"""
═══════════════════════════════════════════════════════════════════════
  Thrivo — Database Layer
  ─────────────────────────────────────────────────────────────────────
  Drop-in replacement for the JSON-file persistence in Thrivo_v9.py.

  Storage backend is chosen automatically:
    1. If env var DATABASE_URL is set → Postgres (production)
    2. Otherwise → JSON files on disk (local dev, single-user mode)

  This means: same code runs locally with zero setup, AND in production
  with a real database, with no code branches scattered through the app.

  Schema (created automatically on first connect):
    users          (username PK, password_hash, email, plan, theme,
                    created_at, last_login)
    subscriptions  (id PK, username, plan, status, payment_phone,
                    payment_amount, requested_at, approved_at)
    user_data      (username PK, data_json, updated_at)
    prices         (asset PK, snapshot_json, updated_at)
    price_history  (asset, date, value, PK(asset, date))

  Usage in Thrivo_v9.py — unchanged. The existing helper functions
  (`_load_users`, `_save_users`, `load_user_data`, `save_data`)
  delegate to this module.
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import os
import json
import datetime
import threading
from typing import Any

# ── Backend selection ─────────────────────────────────────────────────
# We import psycopg2 lazily — local dev doesn't need it installed.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
try:
    import streamlit as _st
    if not DATABASE_URL:
        try:
            DATABASE_URL = (_st.secrets.get("DATABASE_URL", "") or "").strip()
        except Exception:
            pass
except Exception:
    pass

USE_POSTGRES = bool(DATABASE_URL)

_pg_lock = threading.Lock()  # serialize schema init across threads


# ──────────────────────────────────────────────────────────────────────
#  POSTGRES BACKEND
# ──────────────────────────────────────────────────────────────────────
class _PostgresBackend:
    def __init__(self, url: str):
        import psycopg2
        from psycopg2.extras import Json, RealDictCursor
        self._psycopg2 = psycopg2
        self._Json = Json
        self._RealDictCursor = RealDictCursor
        self._url = url
        self._init_schema()

    def _conn(self):
        # Supabase/Neon require sslmode=require — append if missing
        url = self._url
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return self._psycopg2.connect(url, connect_timeout=10)

    def _init_schema(self):
        with _pg_lock:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        username      TEXT PRIMARY KEY,
                        password_hash TEXT NOT NULL,
                        email         TEXT,
                        plan          TEXT DEFAULT 'Free',
                        theme         TEXT DEFAULT 'dark',
                        is_admin      BOOLEAN DEFAULT FALSE,
                        approved      BOOLEAN DEFAULT FALSE,
                        created_at    TIMESTAMPTZ DEFAULT NOW(),
                        last_login    TIMESTAMPTZ
                    );

                    CREATE TABLE IF NOT EXISTS subscriptions (
                        id              SERIAL PRIMARY KEY,
                        username        TEXT NOT NULL,
                        plan            TEXT NOT NULL,
                        status          TEXT NOT NULL DEFAULT 'pending',
                        payment_phone   TEXT,
                        payment_amount  NUMERIC,
                        requested_at    TIMESTAMPTZ DEFAULT NOW(),
                        approved_at     TIMESTAMPTZ
                    );

                    CREATE TABLE IF NOT EXISTS user_data (
                        username   TEXT PRIMARY KEY,
                        data_json  JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS prices (
                        asset         TEXT PRIMARY KEY,
                        snapshot_json JSONB NOT NULL,
                        updated_at    TIMESTAMPTZ DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS price_history (
                        asset TEXT NOT NULL,
                        date  DATE NOT NULL,
                        value NUMERIC NOT NULL,
                        meta_json JSONB,
                        PRIMARY KEY (asset, date)
                    );
                """)
                conn.commit()

    # ── Users ──
    def load_users(self) -> dict:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT * FROM users")
            rows = cur.fetchall()
        return {
            r["username"]: {
                "password_hash": r["password_hash"],
                "email":         r["email"] or "",
                "plan":          r["plan"] or "Free",
                "theme":         r["theme"] or "dark",
                "is_admin":      bool(r["is_admin"]),
                "approved":      bool(r["approved"]),
                "created_at":    r["created_at"].isoformat() if r["created_at"] else "",
                "last_login":    r["last_login"].isoformat() if r["last_login"] else "",
            }
            for r in rows
        }

    def save_users(self, users: dict) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            for uname, u in users.items():
                cur.execute("""
                    INSERT INTO users (username, password_hash, email, plan, theme,
                                       is_admin, approved, created_at, last_login)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()), %s::timestamptz)
                    ON CONFLICT (username) DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        email         = EXCLUDED.email,
                        plan          = EXCLUDED.plan,
                        theme         = EXCLUDED.theme,
                        is_admin      = EXCLUDED.is_admin,
                        approved      = EXCLUDED.approved,
                        last_login    = EXCLUDED.last_login;
                """, (
                    uname,
                    u.get("password_hash", ""),
                    u.get("email", ""),
                    u.get("plan", "Free"),
                    u.get("theme", "dark"),
                    bool(u.get("is_admin", False)),
                    bool(u.get("approved", False)),
                    u.get("created_at") or None,
                    u.get("last_login") or None,
                ))
            conn.commit()

    # ── Subscriptions ──
    def load_subscriptions(self) -> list:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT * FROM subscriptions ORDER BY requested_at DESC")
            rows = cur.fetchall()
        return [
            {
                "id":             r["id"],
                "username":       r["username"],
                "plan":           r["plan"],
                "status":         r["status"],
                "payment_phone":  r["payment_phone"] or "",
                "payment_amount": float(r["payment_amount"]) if r["payment_amount"] is not None else 0.0,
                "requested_at":   r["requested_at"].isoformat() if r["requested_at"] else "",
                "approved_at":    r["approved_at"].isoformat() if r["approved_at"] else "",
            }
            for r in rows
        ]

    def save_subscriptions(self, subs: list) -> None:
        # Save = full replace (simple model, matches old JSON behavior)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions")
            for s in subs:
                cur.execute("""
                    INSERT INTO subscriptions
                        (username, plan, status, payment_phone, payment_amount,
                         requested_at, approved_at)
                    VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
                """, (
                    s.get("username", ""),
                    s.get("plan", ""),
                    s.get("status", "pending"),
                    s.get("payment_phone", ""),
                    float(s.get("payment_amount") or 0),
                    s.get("requested_at") or None,
                    s.get("approved_at") or None,
                ))
            conn.commit()

    # ── User per-user data ──
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
                    data_json  = EXCLUDED.data_json,
                    updated_at = NOW();
            """, (username, json.dumps(data, default=str)))
            conn.commit()

    # ── Public price snapshots ──
    def load_prices(self) -> dict:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("SELECT asset, snapshot_json, updated_at FROM prices")
            rows = cur.fetchall()
        return {
            r["asset"]: {
                **(r["snapshot_json"] or {}),
                "_updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
            }
            for r in rows
        }

    def save_price(self, asset: str, snapshot: dict) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prices (asset, snapshot_json, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (asset) DO UPDATE SET
                    snapshot_json = EXCLUDED.snapshot_json,
                    updated_at    = NOW();
            """, (asset, json.dumps(snapshot, default=str)))
            conn.commit()

    def append_price_history(self, asset: str, date_str: str, value: float, meta: dict | None = None):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO price_history (asset, date, value, meta_json)
                VALUES (%s, %s::date, %s, %s::jsonb)
                ON CONFLICT (asset, date) DO UPDATE SET
                    value     = EXCLUDED.value,
                    meta_json = EXCLUDED.meta_json;
            """, (asset, date_str, float(value), json.dumps(meta or {}, default=str)))
            conn.commit()

    def load_price_history(self, asset: str, days: int = 90) -> list:
        with self._conn() as conn, conn.cursor(cursor_factory=self._RealDictCursor) as cur:
            cur.execute("""
                SELECT date, value, meta_json
                FROM price_history
                WHERE asset = %s
                  AND date >= CURRENT_DATE - %s::int
                ORDER BY date ASC
            """, (asset, days))
            rows = cur.fetchall()
        return [
            {"date": r["date"].isoformat(), "value": float(r["value"]), "meta": r["meta_json"] or {}}
            for r in rows
        ]


# ──────────────────────────────────────────────────────────────────────
#  JSON FILE BACKEND (local dev)
# ──────────────────────────────────────────────────────────────────────
class _JsonBackend:
    USERS_FILE         = "users.json"
    SUBSCRIPTIONS_FILE = "subscriptions.json"
    PRICES_FILE        = "public_prices.json"
    PRICE_HISTORY_FILE = "public_price_history.json"

    @staticmethod
    def _read(path: str, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    @staticmethod
    def _write(path: str, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception:
            pass

    def load_users(self) -> dict:
        return self._read(self.USERS_FILE, {})

    def save_users(self, users: dict) -> None:
        self._write(self.USERS_FILE, users)

    def load_subscriptions(self) -> list:
        d = self._read(self.SUBSCRIPTIONS_FILE, [])
        return d if isinstance(d, list) else []

    def save_subscriptions(self, subs: list) -> None:
        self._write(self.SUBSCRIPTIONS_FILE, subs)

    def load_user_data(self, username: str) -> dict | None:
        path = f"data_{username}.json"
        if not os.path.exists(path):
            return None
        return self._read(path, None)

    def save_user_data(self, username: str, data: dict) -> None:
        self._write(f"data_{username}.json", data)

    def load_prices(self) -> dict:
        return self._read(self.PRICES_FILE, {})

    def save_price(self, asset: str, snapshot: dict) -> None:
        all_prices = self._read(self.PRICES_FILE, {})
        snapshot = dict(snapshot)
        snapshot["_updated_at"] = datetime.datetime.utcnow().isoformat()
        all_prices[asset] = snapshot
        self._write(self.PRICES_FILE, all_prices)

    def append_price_history(self, asset: str, date_str: str, value: float, meta: dict | None = None):
        hist = self._read(self.PRICE_HISTORY_FILE, {})
        hist.setdefault(asset, {})[date_str] = {"value": float(value), "meta": meta or {}}
        self._write(self.PRICE_HISTORY_FILE, hist)

    def load_price_history(self, asset: str, days: int = 90) -> list:
        hist = self._read(self.PRICE_HISTORY_FILE, {}).get(asset, {})
        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        rows = []
        for date_str, payload in sorted(hist.items()):
            try:
                d = datetime.date.fromisoformat(date_str)
                if d >= cutoff:
                    rows.append({
                        "date":  date_str,
                        "value": float(payload.get("value", 0) if isinstance(payload, dict) else payload),
                        "meta":  payload.get("meta", {}) if isinstance(payload, dict) else {},
                    })
            except Exception:
                continue
        return rows


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — what the rest of the app calls
# ──────────────────────────────────────────────────────────────────────
_backend = None
_init_error: str = ""


def _get_backend():
    global _backend, _init_error
    if _backend is not None:
        return _backend
    if USE_POSTGRES:
        try:
            _backend = _PostgresBackend(DATABASE_URL)
            return _backend
        except Exception as e:
            _init_error = f"Postgres connection failed: {e}. Falling back to JSON files."
            _backend = _JsonBackend()
            return _backend
    _backend = _JsonBackend()
    return _backend


def get_init_error() -> str:
    """For the admin panel to surface DB connection problems."""
    _get_backend()  # ensure init attempted
    return _init_error


def get_backend_kind() -> str:
    return "Postgres" if isinstance(_get_backend(), _PostgresBackend) else "JSON files"


# ── Thin wrappers — exact names that match Thrivo's existing helpers ──
def load_users() -> dict:                        return _get_backend().load_users()
def save_users(users: dict) -> None:             _get_backend().save_users(users)
def load_subscriptions() -> list:                return _get_backend().load_subscriptions()
def save_subscriptions(subs: list) -> None:      _get_backend().save_subscriptions(subs)
def load_user_data(username: str) -> dict | None:return _get_backend().load_user_data(username)
def save_user_data(username: str, data: dict):   _get_backend().save_user_data(username, data)
def load_prices() -> dict:                       return _get_backend().load_prices()
def save_price(asset: str, snapshot: dict):      _get_backend().save_price(asset, snapshot)
def append_price_history(asset: str, date_str: str, value: float, meta: dict | None = None):
    _get_backend().append_price_history(asset, date_str, value, meta)
def load_price_history(asset: str, days: int = 90) -> list:
    return _get_backend().load_price_history(asset, days)
