"""
═══════════════════════════════════════════════════════════════════════
  Thrivo — Database Layer  (v10.4)
  ─────────────────────────────────────────────────────────────────────
  Three-tier persistence, auto-selected at startup:

    1. Postgres   → if DATABASE_URL env/secret is set (production)
    2. SQLite     → default, single thrivo.db file (works anywhere)
    3. JSON files → legacy fallback if SQLite import fails (rare)

  SQLite is the default for local dev and for Streamlit Cloud deploys
  without a DATABASE_URL. Same schema, same public API, the rest of the
  app calls the same `load_users / save_users / load_user_data / ...`
  helpers regardless of which backend is active.

  ─────────────────────────────────────────────────────────────────────
  THE EPHEMERAL FILESYSTEM PROBLEM (and how this module handles it)
  ─────────────────────────────────────────────────────────────────────
  Streamlit Cloud's free tier wipes the filesystem on every redeploy
  and on most container restarts. A plain SQLite file would lose data
  every few hours of inactivity.

  Solution: GitHub branch backup. Every save writes the SQLite file to
  disk locally AND (if THRIVO_BACKUP_PAT and THRIVO_BACKUP_REPO are set)
  pushes a copy to a dedicated branch on the user's repo (default:
  `thrivo-data-backup`). On startup, if the local thrivo.db is missing,
  we try to pull the latest backup before initializing fresh.

  Backup is throttled (one push per 60s by default) to avoid spamming
  GitHub. All git operations use a pure-Python implementation via the
  GitHub REST API — NO `git` binary required, which is critical because
  Streamlit Cloud's container doesn't always have git in PATH.

  ─────────────────────────────────────────────────────────────────────
  CONFIGURATION (env vars or st.secrets)
  ─────────────────────────────────────────────────────────────────────
    DATABASE_URL              → use Postgres (skip SQLite entirely)
    THRIVO_DB_PATH            → SQLite file path (default: thrivo.db)
    THRIVO_BACKUP_PAT         → GitHub Personal Access Token (repo scope)
    THRIVO_BACKUP_REPO        → owner/repo (e.g. "egahmedsamir/thrivo")
    THRIVO_BACKUP_BRANCH      → branch name (default: thrivo-data-backup)
    THRIVO_BACKUP_THROTTLE    → seconds between pushes (default: 60)
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import os
import json
import sqlite3
import datetime
import threading
import base64
import time as _time
from typing import Any


# ──────────────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────────────
def _cfg_value(key: str, default: str = "") -> str:
    """Resolve env var → st.secrets → default. Works without Streamlit too."""
    v = os.environ.get(key, "")
    if v:
        return v
    try:
        import streamlit as _st
        if key in _st.secrets:
            return str(_st.secrets[key])
    except Exception:
        pass
    return default


DATABASE_URL    = _cfg_value("DATABASE_URL").strip()
SQLITE_PATH     = _cfg_value("THRIVO_DB_PATH", "thrivo.db").strip()
BACKUP_PAT      = _cfg_value("THRIVO_BACKUP_PAT").strip()
BACKUP_REPO     = _cfg_value("THRIVO_BACKUP_REPO").strip()  # "owner/repo"
BACKUP_BRANCH   = _cfg_value("THRIVO_BACKUP_BRANCH", "thrivo-data-backup").strip()
try:
    BACKUP_THROTTLE = int(_cfg_value("THRIVO_BACKUP_THROTTLE", "60"))
except ValueError:
    BACKUP_THROTTLE = 60


# Module-level state
_pg_lock     = threading.Lock()
_sqlite_lock = threading.Lock()
_backup_lock = threading.Lock()
_last_backup_at = 0.0
_init_error: str = ""
_backend = None
_backup_warned = False


# ──────────────────────────────────────────────────────────────────────
#  GITHUB BACKUP HELPERS  (pure-Python, no `git` binary needed)
# ──────────────────────────────────────────────────────────────────────
def _backup_configured() -> bool:
    return bool(BACKUP_PAT and BACKUP_REPO)


def _gh_request(method: str, path: str, **kwargs):
    """Lightweight wrapper around requests for GitHub API calls."""
    import requests
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {BACKUP_PAT}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "thrivo-backup",
    }
    headers.update(kwargs.pop("headers", {}))
    return requests.request(method, url, headers=headers, timeout=30, **kwargs)


def _backup_push_db(db_file_path: str) -> tuple[bool, str]:
    """
    Push the local SQLite file to the backup branch on GitHub.
    Returns (success, message). Throttled — won't push more than once
    per BACKUP_THROTTLE seconds.
    """
    global _last_backup_at, _backup_warned
    if not _backup_configured():
        return False, "backup not configured"

    if not os.path.exists(db_file_path):
        return False, "db file missing"

    # Size guard — GitHub blob hard limit is 100MB. Warn at 95MB.
    size = os.path.getsize(db_file_path)
    if size > 95 * 1024 * 1024:
        return False, f"db file too large ({size/1024/1024:.1f}MB) — cannot push to GitHub"

    # Throttle
    now = _time.time()
    if now - _last_backup_at < BACKUP_THROTTLE:
        return False, "throttled"

    with _backup_lock:
        # Re-check inside lock
        if _time.time() - _last_backup_at < BACKUP_THROTTLE:
            return False, "throttled"

        try:
            with open(db_file_path, "rb") as f:
                content_bytes = f.read()
            content_b64 = base64.b64encode(content_bytes).decode("ascii")

            # 1. Get current SHA of thrivo.db on backup branch (or 404 if new)
            sha = None
            r = _gh_request(
                "GET",
                f"/repos/{BACKUP_REPO}/contents/thrivo.db",
                params={"ref": BACKUP_BRANCH},
            )
            if r.status_code == 200:
                sha = r.json().get("sha")
            elif r.status_code == 404:
                # Branch may not exist yet — try to create it
                _ensure_backup_branch_exists()
            elif r.status_code in (401, 403):
                if not _backup_warned:
                    print(f"⚠️  Thrivo backup: GitHub auth failed ({r.status_code}). "
                          f"Check THRIVO_BACKUP_PAT permissions.")
                    _backup_warned = True
                return False, f"auth failed ({r.status_code})"

            # 2. Commit the new content
            payload = {
                "message": f"Thrivo data backup {datetime.datetime.utcnow().isoformat()}Z",
                "content": content_b64,
                "branch":  BACKUP_BRANCH,
            }
            if sha:
                payload["sha"] = sha

            r = _gh_request(
                "PUT",
                f"/repos/{BACKUP_REPO}/contents/thrivo.db",
                json=payload,
            )
            if r.status_code in (200, 201):
                _last_backup_at = _time.time()
                return True, "ok"
            return False, f"push failed: {r.status_code} {r.text[:120]}"
        except Exception as e:
            return False, f"backup exception: {e}"


def _ensure_backup_branch_exists():
    """If the backup branch doesn't exist, create it from the default branch."""
    try:
        # 1. Find default branch
        r = _gh_request("GET", f"/repos/{BACKUP_REPO}")
        if r.status_code != 200:
            return
        default_branch = r.json().get("default_branch", "main")

        # 2. Get default branch's HEAD SHA
        r = _gh_request("GET",
                        f"/repos/{BACKUP_REPO}/git/refs/heads/{default_branch}")
        if r.status_code != 200:
            return
        head_sha = r.json()["object"]["sha"]

        # 3. Create the backup branch (idempotent — 422 if it already exists)
        _gh_request(
            "POST",
            f"/repos/{BACKUP_REPO}/git/refs",
            json={"ref": f"refs/heads/{BACKUP_BRANCH}", "sha": head_sha},
        )
    except Exception:
        pass


def _backup_pull_db(db_file_path: str) -> tuple[bool, str]:
    """
    Restore the SQLite file from the backup branch on GitHub.
    Returns (success, message). Called once on startup if the local
    db file is missing.
    """
    if not _backup_configured():
        return False, "backup not configured"
    try:
        r = _gh_request(
            "GET",
            f"/repos/{BACKUP_REPO}/contents/thrivo.db",
            params={"ref": BACKUP_BRANCH},
            headers={"Accept": "application/vnd.github.raw"},
        )
        if r.status_code == 200:
            os.makedirs(os.path.dirname(db_file_path) or ".", exist_ok=True)
            with open(db_file_path, "wb") as f:
                f.write(r.content)
            return True, f"restored {len(r.content)} bytes"
        if r.status_code == 404:
            return False, "no backup yet (first run)"
        return False, f"pull failed: {r.status_code}"
    except Exception as e:
        return False, f"pull exception: {e}"


# ──────────────────────────────────────────────────────────────────────
#  POSTGRES BACKEND (carried over — production option)
# ──────────────────────────────────────────────────────────────────────
class _PostgresBackend:
    KIND = "Postgres"

    def __init__(self, url: str):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        self._psycopg2 = psycopg2
        self._RealDictCursor = RealDictCursor
        self._url = url
        self._init_schema()

    def _conn(self):
        url = self._url
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return self._psycopg2.connect(url, connect_timeout=10)

    def _init_schema(self):
        with _pg_lock, self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    email TEXT, plan TEXT DEFAULT 'Free', theme TEXT DEFAULT 'dark',
                    is_admin BOOLEAN DEFAULT FALSE, approved BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(), last_login TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY, username TEXT NOT NULL,
                    plan TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    payment_phone TEXT, payment_amount NUMERIC,
                    requested_at TIMESTAMPTZ DEFAULT NOW(), approved_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS user_data (
                    username TEXT PRIMARY KEY, data_json JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS prices (
                    asset TEXT PRIMARY KEY, snapshot_json JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS price_history (
                    asset TEXT NOT NULL, date DATE NOT NULL, value NUMERIC NOT NULL,
                    meta_json JSONB, PRIMARY KEY (asset, date)
                );
            """)
            conn.commit()

    def load_users(self) -> dict:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT * FROM users")
            return {r["username"]: {
                "password_hash": r["password_hash"], "email": r["email"] or "",
                "plan": r["plan"] or "Free", "theme": r["theme"] or "dark",
                "is_admin": bool(r["is_admin"]), "approved": bool(r["approved"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                "last_login": r["last_login"].isoformat() if r["last_login"] else "",
            } for r in cur.fetchall()}

    def save_users(self, users: dict) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            for uname, u in users.items():
                cur.execute("""
                    INSERT INTO users (username, password_hash, email, plan, theme,
                                       is_admin, approved, created_at, last_login)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, NOW()),%s::timestamptz)
                    ON CONFLICT (username) DO UPDATE SET
                        password_hash=EXCLUDED.password_hash, email=EXCLUDED.email,
                        plan=EXCLUDED.plan, theme=EXCLUDED.theme,
                        is_admin=EXCLUDED.is_admin, approved=EXCLUDED.approved,
                        last_login=EXCLUDED.last_login;
                """, (uname, u.get("password_hash", ""), u.get("email", ""),
                      u.get("plan", "Free"), u.get("theme", "dark"),
                      bool(u.get("is_admin", False)), bool(u.get("approved", False)),
                      u.get("created_at") or None, u.get("last_login") or None))
            conn.commit()

    def load_subscriptions(self) -> list:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT * FROM subscriptions ORDER BY requested_at DESC")
            return [{
                "id": r["id"], "username": r["username"], "plan": r["plan"],
                "status": r["status"], "payment_phone": r["payment_phone"] or "",
                "payment_amount": float(r["payment_amount"]) if r["payment_amount"] is not None else 0.0,
                "requested_at": r["requested_at"].isoformat() if r["requested_at"] else "",
                "approved_at":  r["approved_at"].isoformat()  if r["approved_at"]  else "",
            } for r in cur.fetchall()]

    def save_subscriptions(self, subs: list) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions")
            for s in subs:
                cur.execute("""
                    INSERT INTO subscriptions (username, plan, status, payment_phone,
                        payment_amount, requested_at, approved_at)
                    VALUES (%s,%s,%s,%s,%s,%s::timestamptz,%s::timestamptz)
                """, (s.get("username", ""), s.get("plan", ""), s.get("status", "pending"),
                      s.get("payment_phone", ""), float(s.get("payment_amount") or 0),
                      s.get("requested_at") or None, s.get("approved_at") or None))
            conn.commit()

    def load_user_data(self, username: str) -> dict | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT data_json FROM user_data WHERE username = %s", (username,))
            row = cur.fetchone()
        return row[0] if row else None

    def save_user_data(self, username: str, data: dict) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_data (username, data_json, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (username) DO UPDATE SET
                    data_json = EXCLUDED.data_json, updated_at = NOW();
            """, (username, json.dumps(data, default=str)))
            conn.commit()

    def load_prices(self) -> dict:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT asset, snapshot_json, updated_at FROM prices")
            return {r["asset"]: {**(r["snapshot_json"] or {}),
                                 "_updated_at": r["updated_at"].isoformat() if r["updated_at"] else ""}
                    for r in cur.fetchall()}

    def save_price(self, asset: str, snapshot: dict) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prices (asset, snapshot_json, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (asset) DO UPDATE SET
                    snapshot_json = EXCLUDED.snapshot_json, updated_at = NOW();
            """, (asset, json.dumps(snapshot, default=str)))
            conn.commit()

    def append_price_history(self, asset: str, date_str: str, value: float, meta: dict | None = None):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO price_history (asset, date, value, meta_json)
                VALUES (%s, %s::date, %s, %s::jsonb)
                ON CONFLICT (asset, date) DO UPDATE SET
                    value = EXCLUDED.value, meta_json = EXCLUDED.meta_json;
            """, (asset, date_str, float(value), json.dumps(meta or {}, default=str)))
            conn.commit()

    def load_price_history(self, asset: str, days: int = 90) -> list:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("""
                SELECT date, value, meta_json FROM price_history
                WHERE asset = %s AND date >= CURRENT_DATE - %s::int
                ORDER BY date ASC
            """, (asset, days))
            return [{"date": r["date"].isoformat(), "value": float(r["value"]),
                     "meta": r["meta_json"] or {}} for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────────────
#  SQLITE BACKEND  (default — single-file, portable, no server)
# ──────────────────────────────────────────────────────────────────────
class _SQLiteBackend:
    KIND = "SQLite"

    def __init__(self, db_path: str):
        self._path = db_path
        self._restored_from_backup = False

        # If the local file is missing, try to restore from GitHub backup
        if not os.path.exists(self._path) and _backup_configured():
            ok, msg = _backup_pull_db(self._path)
            if ok:
                self._restored_from_backup = True
                print(f"✓ Thrivo: {msg} from {BACKUP_REPO}@{BACKUP_BRANCH}")
            else:
                print(f"  Thrivo: no backup restored ({msg}) — starting fresh")

        self._init_schema()

    def _conn(self):
        # check_same_thread=False because Streamlit may dispatch across threads.
        # We serialize via _sqlite_lock to keep it safe.
        c = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")  # better concurrency
        c.execute("PRAGMA synchronous=NORMAL;")  # faster, still safe
        c.execute("PRAGMA foreign_keys=ON;")
        return c

    def _init_schema(self):
        with _sqlite_lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username       TEXT PRIMARY KEY,
                    password_hash  TEXT NOT NULL,
                    email          TEXT,
                    plan           TEXT DEFAULT 'Free',
                    theme          TEXT DEFAULT 'dark',
                    is_admin       INTEGER DEFAULT 0,
                    approved       INTEGER DEFAULT 0,
                    created_at     TEXT,
                    last_login     TEXT
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    username        TEXT NOT NULL,
                    plan            TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    payment_phone   TEXT,
                    payment_amount  REAL,
                    requested_at    TEXT,
                    approved_at     TEXT
                );
                CREATE TABLE IF NOT EXISTS user_data (
                    username    TEXT PRIMARY KEY,
                    data_json   TEXT NOT NULL,
                    updated_at  TEXT
                );
                CREATE TABLE IF NOT EXISTS prices (
                    asset          TEXT PRIMARY KEY,
                    snapshot_json  TEXT NOT NULL,
                    updated_at     TEXT
                );
                CREATE TABLE IF NOT EXISTS price_history (
                    asset      TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    value      REAL NOT NULL,
                    meta_json  TEXT,
                    PRIMARY KEY (asset, date)
                );
            """)

    def _now(self):
        return datetime.datetime.utcnow().isoformat()

    def _maybe_backup(self):
        """Fire-and-forget backup push. Throttled internally."""
        if not _backup_configured():
            return
        # Run in a thread so save_user_data isn't blocked by network I/O
        def _do():
            try:
                _backup_push_db(self._path)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    # ── Users ──
    def load_users(self) -> dict:
        with _sqlite_lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM users").fetchall()
        return {
            r["username"]: {
                "password_hash": r["password_hash"],
                "email":         r["email"] or "",
                "plan":          r["plan"] or "Free",
                "theme":         r["theme"] or "dark",
                "is_admin":      bool(r["is_admin"]),
                "approved":      bool(r["approved"]),
                "created_at":    r["created_at"] or "",
                "last_login":    r["last_login"] or "",
            }
            for r in rows
        }

    def save_users(self, users: dict) -> None:
        with _sqlite_lock, self._conn() as conn:
            for uname, u in users.items():
                conn.execute("""
                    INSERT INTO users (username, password_hash, email, plan, theme,
                                       is_admin, approved, created_at, last_login)
                    VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, ?), ?)
                    ON CONFLICT(username) DO UPDATE SET
                        password_hash = excluded.password_hash,
                        email         = excluded.email,
                        plan          = excluded.plan,
                        theme         = excluded.theme,
                        is_admin      = excluded.is_admin,
                        approved      = excluded.approved,
                        last_login    = excluded.last_login;
                """, (
                    uname, u.get("password_hash", ""), u.get("email", ""),
                    u.get("plan", "Free"), u.get("theme", "dark"),
                    1 if u.get("is_admin") else 0,
                    1 if u.get("approved") else 0,
                    u.get("created_at"), self._now(),
                    u.get("last_login"),
                ))
            conn.commit()
        self._maybe_backup()

    # ── Subscriptions ──
    def load_subscriptions(self) -> list:
        with _sqlite_lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions ORDER BY requested_at DESC"
            ).fetchall()
        return [
            {
                "id":             r["id"],
                "username":       r["username"],
                "plan":           r["plan"],
                "status":         r["status"],
                "payment_phone":  r["payment_phone"] or "",
                "payment_amount": float(r["payment_amount"] or 0),
                "requested_at":   r["requested_at"] or "",
                "approved_at":    r["approved_at"] or "",
            }
            for r in rows
        ]

    def save_subscriptions(self, subs: list) -> None:
        with _sqlite_lock, self._conn() as conn:
            conn.execute("DELETE FROM subscriptions")
            for s in subs:
                conn.execute("""
                    INSERT INTO subscriptions
                        (username, plan, status, payment_phone, payment_amount,
                         requested_at, approved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    s.get("username", ""), s.get("plan", ""),
                    s.get("status", "pending"), s.get("payment_phone", ""),
                    float(s.get("payment_amount") or 0),
                    s.get("requested_at") or self._now(),
                    s.get("approved_at") or None,
                ))
            conn.commit()
        self._maybe_backup()

    # ── User per-user data ──
    def load_user_data(self, username: str) -> dict | None:
        with _sqlite_lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM user_data WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["data_json"])
        except Exception:
            return None

    def save_user_data(self, username: str, data: dict) -> None:
        payload = json.dumps(data, default=str)
        with _sqlite_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO user_data (username, data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    data_json  = excluded.data_json,
                    updated_at = excluded.updated_at;
            """, (username, payload, self._now()))
            conn.commit()
        self._maybe_backup()

    # ── Public price snapshots ──
    def load_prices(self) -> dict:
        with _sqlite_lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT asset, snapshot_json, updated_at FROM prices"
            ).fetchall()
        out = {}
        for r in rows:
            try:
                snap = json.loads(r["snapshot_json"])
            except Exception:
                snap = {}
            snap["_updated_at"] = r["updated_at"] or ""
            out[r["asset"]] = snap
        return out

    def save_price(self, asset: str, snapshot: dict) -> None:
        payload = json.dumps(snapshot, default=str)
        with _sqlite_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO prices (asset, snapshot_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(asset) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    updated_at    = excluded.updated_at;
            """, (asset, payload, self._now()))
            conn.commit()
        self._maybe_backup()

    def append_price_history(self, asset: str, date_str: str, value: float,
                              meta: dict | None = None):
        meta_payload = json.dumps(meta or {}, default=str)
        with _sqlite_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO price_history (asset, date, value, meta_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset, date) DO UPDATE SET
                    value     = excluded.value,
                    meta_json = excluded.meta_json;
            """, (asset, date_str, float(value), meta_payload))
            conn.commit()
        self._maybe_backup()

    def load_price_history(self, asset: str, days: int = 90) -> list:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        with _sqlite_lock, self._conn() as conn:
            rows = conn.execute("""
                SELECT date, value, meta_json
                FROM price_history
                WHERE asset = ? AND date >= ?
                ORDER BY date ASC
            """, (asset, cutoff)).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            except Exception:
                meta = {}
            out.append({"date": r["date"], "value": float(r["value"]), "meta": meta})
        return out

    # ── Admin helpers (SQLite-only) ──
    def export_db_bytes(self) -> bytes:
        """Return the raw SQLite file bytes — for admin download."""
        if not os.path.exists(self._path):
            return b""
        with open(self._path, "rb") as f:
            return f.read()

    def force_backup(self) -> tuple[bool, str]:
        """Manual backup trigger for admin panel."""
        global _last_backup_at
        # Bypass throttle by resetting the timer
        _last_backup_at = 0.0
        return _backup_push_db(self._path)


# ──────────────────────────────────────────────────────────────────────
#  JSON FILE BACKEND (final fallback if SQLite fails — extremely rare)
# ──────────────────────────────────────────────────────────────────────
class _JsonBackend:
    KIND = "JSON files"
    USERS_FILE          = "users.json"
    SUBSCRIPTIONS_FILE  = "subscriptions.json"
    PRICES_FILE         = "public_prices.json"
    PRICE_HISTORY_FILE  = "public_price_history.json"

    @staticmethod
    def _read(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return default

    @staticmethod
    def _write(path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception: pass

    def load_users(self) -> dict: return self._read(self.USERS_FILE, {})
    def save_users(self, u): self._write(self.USERS_FILE, u)
    def load_subscriptions(self) -> list:
        d = self._read(self.SUBSCRIPTIONS_FILE, [])
        return d if isinstance(d, list) else []
    def save_subscriptions(self, s): self._write(self.SUBSCRIPTIONS_FILE, s)
    def load_user_data(self, username):
        path = f"data_{username}.json"
        return self._read(path, None) if os.path.exists(path) else None
    def save_user_data(self, username, data): self._write(f"data_{username}.json", data)
    def load_prices(self) -> dict: return self._read(self.PRICES_FILE, {})
    def save_price(self, asset, snapshot):
        all_p = self._read(self.PRICES_FILE, {})
        snapshot = dict(snapshot)
        snapshot["_updated_at"] = datetime.datetime.utcnow().isoformat()
        all_p[asset] = snapshot
        self._write(self.PRICES_FILE, all_p)
    def append_price_history(self, asset, date_str, value, meta=None):
        hist = self._read(self.PRICE_HISTORY_FILE, {})
        hist.setdefault(asset, {})[date_str] = {"value": float(value), "meta": meta or {}}
        self._write(self.PRICE_HISTORY_FILE, hist)
    def load_price_history(self, asset, days=90):
        hist = self._read(self.PRICE_HISTORY_FILE, {}).get(asset, {})
        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        rows = []
        for ds, p in sorted(hist.items()):
            try:
                d = datetime.date.fromisoformat(ds)
                if d >= cutoff:
                    rows.append({"date": ds,
                                 "value": float(p.get("value", 0) if isinstance(p, dict) else p),
                                 "meta":  p.get("meta", {}) if isinstance(p, dict) else {}})
            except Exception: continue
        return rows


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ──────────────────────────────────────────────────────────────────────
def _get_backend():
    """Lazy-init the chosen backend on first call."""
    global _backend, _init_error
    if _backend is not None:
        return _backend

    # 1. Postgres (production)
    if DATABASE_URL:
        try:
            _backend = _PostgresBackend(DATABASE_URL)
            return _backend
        except Exception as e:
            _init_error = f"Postgres init failed: {e} — falling back to SQLite."

    # 2. SQLite (default)
    try:
        _backend = _SQLiteBackend(SQLITE_PATH)
        if _init_error:
            _init_error += " (now using SQLite)"
        return _backend
    except Exception as e:
        _init_error = (_init_error + " | " if _init_error else "") + \
                      f"SQLite init failed: {e} — falling back to JSON files."

    # 3. JSON files (last resort)
    _backend = _JsonBackend()
    return _backend


def get_init_error() -> str:
    _get_backend()
    return _init_error


def get_backend_kind() -> str:
    return _get_backend().KIND


def get_backup_status() -> dict:
    """Used by the admin panel to display backup health."""
    return {
        "configured": _backup_configured(),
        "repo":       BACKUP_REPO if _backup_configured() else "",
        "branch":     BACKUP_BRANCH,
        "last_push":  (datetime.datetime.fromtimestamp(_last_backup_at).isoformat()
                       if _last_backup_at else "never"),
        "throttle_s": BACKUP_THROTTLE,
    }


def force_backup_now() -> tuple[bool, str]:
    """Admin panel button — push backup immediately (bypass throttle)."""
    backend = _get_backend()
    if hasattr(backend, "force_backup"):
        return backend.force_backup()
    return False, "current backend doesn't support backup"


def export_db_bytes() -> bytes:
    """Admin panel — download the entire SQLite db. Returns empty bytes
    if the active backend isn't SQLite."""
    backend = _get_backend()
    if hasattr(backend, "export_db_bytes"):
        return backend.export_db_bytes()
    return b""


# ── Thin pass-through wrappers (signatures unchanged from v10.0) ──
def load_users() -> dict:                          return _get_backend().load_users()
def save_users(users: dict) -> None:               _get_backend().save_users(users)
def load_subscriptions() -> list:                  return _get_backend().load_subscriptions()
def save_subscriptions(subs: list) -> None:        _get_backend().save_subscriptions(subs)
def load_user_data(username: str) -> dict | None:  return _get_backend().load_user_data(username)
def save_user_data(username: str, data: dict):     _get_backend().save_user_data(username, data)
def load_prices() -> dict:                         return _get_backend().load_prices()
def save_price(asset: str, snapshot: dict):        _get_backend().save_price(asset, snapshot)
def append_price_history(asset: str, date_str: str, value: float, meta: dict | None = None):
    _get_backend().append_price_history(asset, date_str, value, meta)
def load_price_history(asset: str, days: int = 90) -> list:
    return _get_backend().load_price_history(asset, days)
