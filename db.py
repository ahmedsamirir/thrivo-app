"""
═══════════════════════════════════════════════════════════════════════
  Thrivo — Database Layer (v11)
  ─────────────────────────────────────────────────────────────────────
  JSON-FILE storage only. SQLite and Postgres backends are GONE.

  WHY JSON?  Per the v11 spec: "Save data only as a JSON file on the
  Streamlit cloud and load that JSON as a backup anytime."

  WARNING ABOUT DATA LOSS:  JSON files on Streamlit Cloud's filesystem
  get wiped whenever:
    • You push a new commit (redeploy)
    • The app sleeps 15+ minutes then wakes
    • Streamlit's infrastructure reboots your container

  This module provides MANUAL export/import only — no auto-backup.
  To prevent losing data, the admin must:
    1. Click "Download Backup" in the admin panel BEFORE every code push
    2. Click "Upload Backup" after redeploy to restore
    3. Download backups regularly (the app reminds you on admin login)

  The admin panel also shows the last-modified time of each file and
  warns when data hasn't been backed up recently.

  ─────────────────────────────────────────────────────────────────────
  CONFIGURATION  (env vars or st.secrets)
  ─────────────────────────────────────────────────────────────────────
    THRIVO_DATA_DIR  → where JSON files live (default: current working dir)
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import os
import io
import json
import datetime
import threading
import zipfile


# ──────────────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────────────
def _cfg(key: str, default: str = "") -> str:
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


DATA_DIR = _cfg("THRIVO_DATA_DIR", "").strip() or "."

# Filenames (all relative to DATA_DIR)
USERS_FILE          = "users.json"
SUBSCRIPTIONS_FILE  = "subscriptions.json"
PRICES_FILE         = "public_prices.json"
PRICE_HISTORY_FILE  = "public_price_history.json"

# Module-level state
_io_lock = threading.Lock()


def _path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


# ──────────────────────────────────────────────────────────────────────
#  JSON I/O — atomic writes (write to .tmp then rename)
# ──────────────────────────────────────────────────────────────────────
def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    """Atomic write: write to .tmp then os.replace."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Users
# ──────────────────────────────────────────────────────────────────────
def load_users() -> dict:
    with _io_lock:
        d = _read_json(_path(USERS_FILE), {})
    return d if isinstance(d, dict) else {}


def save_users(users: dict) -> None:
    with _io_lock:
        _write_json(_path(USERS_FILE), users)


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Subscriptions
# ──────────────────────────────────────────────────────────────────────
def load_subscriptions() -> list:
    with _io_lock:
        d = _read_json(_path(SUBSCRIPTIONS_FILE), [])
    return d if isinstance(d, list) else []


def save_subscriptions(subs: list) -> None:
    with _io_lock:
        _write_json(_path(SUBSCRIPTIONS_FILE), subs)


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Per-user data (one file per user)
# ──────────────────────────────────────────────────────────────────────
def _user_data_path(username: str) -> str:
    safe = "".join(c for c in str(username) if c.isalnum() or c in "_-.")
    return _path(f"data_{safe}.json")


def load_user_data(username: str) -> dict | None:
    path = _user_data_path(username)
    if not os.path.exists(path):
        return None
    with _io_lock:
        return _read_json(path, None)


def save_user_data(username: str, data: dict) -> None:
    with _io_lock:
        _write_json(_user_data_path(username), data)


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Public prices (gold/usd/btc/egx snapshots)
# ──────────────────────────────────────────────────────────────────────
def load_prices() -> dict:
    with _io_lock:
        d = _read_json(_path(PRICES_FILE), {})
    return d if isinstance(d, dict) else {}


def save_price(asset: str, snapshot: dict) -> None:
    with _io_lock:
        all_p = _read_json(_path(PRICES_FILE), {})
        if not isinstance(all_p, dict):
            all_p = {}
        snap = dict(snapshot)
        snap["_updated_at"] = datetime.datetime.utcnow().isoformat()
        all_p[asset] = snap
        _write_json(_path(PRICES_FILE), all_p)


def append_price_history(asset: str, date_str: str, value: float, meta: dict | None = None) -> None:
    with _io_lock:
        hist = _read_json(_path(PRICE_HISTORY_FILE), {})
        if not isinstance(hist, dict):
            hist = {}
        hist.setdefault(asset, {})[date_str] = {"value": float(value), "meta": meta or {}}
        _write_json(_path(PRICE_HISTORY_FILE), hist)


def load_price_history(asset: str, days: int = 90) -> list:
    with _io_lock:
        hist = _read_json(_path(PRICE_HISTORY_FILE), {})
    asset_hist = (hist.get(asset) if isinstance(hist, dict) else {}) or {}
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    rows: list[dict] = []
    for ds, p in sorted(asset_hist.items()):
        try:
            d = datetime.date.fromisoformat(ds)
            if d >= cutoff:
                val = p.get("value", 0) if isinstance(p, dict) else p
                meta = p.get("meta", {}) if isinstance(p, dict) else {}
                rows.append({"date": ds, "value": float(val), "meta": meta})
        except Exception:
            continue
    return rows


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Identity (kept for backward-compat with v10 admin panel)
# ──────────────────────────────────────────────────────────────────────
def get_backend_kind() -> str:
    return "JSON files"


def get_init_error() -> str:
    return ""


def get_backup_status() -> dict:
    """Reports the last-modified timestamp of each data file so the admin
    knows whether they need to grab a fresh backup."""
    files = [USERS_FILE, SUBSCRIPTIONS_FILE, PRICES_FILE, PRICE_HISTORY_FILE]
    try:
        for f in os.listdir(DATA_DIR):
            if f.startswith("data_") and f.endswith(".json") and f not in files:
                files.append(f)
    except Exception:
        pass

    file_info = []
    newest_mtime = 0
    for fname in files:
        p = _path(fname)
        if os.path.exists(p):
            mt = os.path.getmtime(p)
            file_info.append({
                "name":         fname,
                "size_bytes":   os.path.getsize(p),
                "modified":     datetime.datetime.fromtimestamp(mt).isoformat(),
                "modified_ts":  mt,
            })
            newest_mtime = max(newest_mtime, mt)

    return {
        "mode":              "manual-only",
        "data_dir":          os.path.abspath(DATA_DIR),
        "files":             file_info,
        "newest_change":     (datetime.datetime.fromtimestamp(newest_mtime).isoformat()
                              if newest_mtime else "never"),
        "newest_change_ts":  newest_mtime,
    }


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API — Manual export & import
# ──────────────────────────────────────────────────────────────────────
def export_all_as_zip() -> bytes:
    """Bundle every JSON file in DATA_DIR into a downloadable ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        files_added = 0
        try:
            for f in sorted(os.listdir(DATA_DIR)):
                if f.endswith(".json"):
                    zf.write(_path(f), arcname=f)
                    files_added += 1
        except Exception:
            pass
        manifest = {
            "exported_at":   datetime.datetime.utcnow().isoformat(),
            "thrivo_version": "v11",
            "files_count":    files_added,
            "format":         "json-bundle-v1",
        }
        zf.writestr("_manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()


def import_from_zip(zip_bytes: bytes) -> tuple[int, str]:
    """Restore JSON files from a ZIP produced by export_all_as_zip.
    Overwrites existing local files. Returns (files_imported, message).
    Only files ending in .json that look like Thrivo data files are accepted.
    """
    KNOWN_PREFIXES = ("users.json", "subscriptions.json", "public_prices.json",
                      "public_price_history.json", "data_")
    try:
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            count = 0
            for name in zf.namelist():
                safe_name = os.path.basename(name)
                if not safe_name.endswith(".json") or safe_name == "_manifest.json":
                    continue
                # Whitelist check — only accept files that look like Thrivo data
                if not any(safe_name == known or safe_name.startswith(known)
                           for known in KNOWN_PREFIXES):
                    continue
                # Sanity-check that it's valid JSON before writing
                try:
                    payload = zf.read(name)
                    json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                with open(_path(safe_name), "wb") as dst:
                    dst.write(payload)
                count += 1
        if count == 0:
            return 0, "no valid Thrivo data files found in the ZIP"
        return count, f"imported {count} files successfully"
    except zipfile.BadZipFile:
        return 0, "the uploaded file is not a valid ZIP"
    except Exception as e:
        return 0, f"import failed: {e}"


# Aliases kept for backward-compat with v10 admin panel code
def export_db_bytes() -> bytes:
    return export_all_as_zip()


def force_backup_now() -> tuple[bool, str]:
    return False, "v11 is manual-only — use the Download Backup button instead"
