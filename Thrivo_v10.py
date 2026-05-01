"""
═══════════════════════════════════════════════════════════════════════
  THRIVO — The Personal Growth Operating System
  ─────────────────────────────────────────────────────────────────────
  A multi-tenant SaaS platform for intentional living:
  daily tracking, finance, gym, stocks, journaling, AI coaching.

  Architecture:
    • Streamlit single-file app (easy deploy)
    • Per-user JSON persistence (file: data_<username>.json)
    • Tier-gated feature access via SUBSCRIPTION_PLANS
    • Admin panel with approval queue for paid accounts
    • Pluggable AI (Google Gemini) & live market data (multi-source)

  Config precedence:  env var  →  secrets.toml  →  hardcoded default
═══════════════════════════════════════════════════════════════════════
"""
import streamlit as st
import pandas as pd
import json
import os
import datetime
import time
import plotly.express as px
import plotly.graph_objects as go
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import io
import hashlib
import re as _re
import requests
from bs4 import BeautifulSoup


# ═══════════════════════════════════════════════════════════════════════
#  APP CONFIGURATION  —  all tunable knobs in one place
# ═══════════════════════════════════════════════════════════════════════
def _cfg(key: str, default):
    """Config resolution: env var → st.secrets → default. Works in any deploy."""
    env_val = os.environ.get(key)
    if env_val is not None and env_val != "":
        return env_val
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return default


class AppConfig:
    # ── Branding ──
    NAME          = "Thrivo"
    TAGLINE       = "Grow with intention."
    ICON          = "🌱"
    VERSION       = "v9.0"

    # ── Storage ──
    DATA_FILE          = "thrivo_shared_data.json"   # legacy shared file (for migration)
    USERS_FILE         = _cfg("THRIVO_USERS_FILE",         "users.json")
    SUBSCRIPTIONS_FILE = _cfg("THRIVO_SUBSCRIPTIONS_FILE", "subscriptions.json")
    USER_DATA_PREFIX   = "data_"                     # per-user file prefix
    SESSION_TIMEOUT    = 300                         # 5 minutes idle timeout

    # ── Admin contact ──
    # Empty default — set via env var or st.secrets in production deploy
    ADMIN_EMAIL   = _cfg("THRIVO_ADMIN_EMAIL", "")
    SUPPORT_URL   = _cfg("THRIVO_SUPPORT_URL", "")

    # ── DEPRECATED legacy single-password gate ──
    # APP_PASSWORD was used by v8 (single-password gate). The current
    # multi-user auth (users.json + per-user password hashes) replaces it.
    # We keep the symbol for back-compat with any user-customized code that
    # might still reference it, but it is NOT used by the auth flow and
    # does NOT need a value. Empty default = no leaked secret.
    APP_PASSWORD  = _cfg("THRIVO_APP_PASSWORD", "")
    SCRAPE_URL    = "https://goldbullioneg.com/%D8%A3%D8%B3%D8%B9%D8%A7%D8%B1-%D8%A7%D9%84%D8%B0%D9%87%D8%A8/"

    # ── Payment phone (stored as SHA-256 hash, never plaintext) ──
    # Set via THRIVO_PAYMENT_PHONE env var or st.secrets. Empty default —
    # the admin panel checks if it's set before showing the manual-payment flow.
    _PAYMENT_PHONE = _cfg("THRIVO_PAYMENT_PHONE", "")
    PAYMENT_PHONE_HASH = (
        hashlib.sha256(str(_PAYMENT_PHONE).encode()).hexdigest()
        if _PAYMENT_PHONE else ""
    )
    PAYMENT_PHONE_MASKED = (
        (str(_PAYMENT_PHONE)[:3] + "****" + str(_PAYMENT_PHONE)[-4:])
        if _PAYMENT_PHONE and len(str(_PAYMENT_PHONE)) >= 7 else ""
    )

    # ── Admin bootstrap password ──
    # On FIRST run with an empty users table, the app creates a default admin.
    # In production, set THRIVO_ADMIN_BOOTSTRAP_PASSWORD via env var/secret.
    # If unset, a cryptographically random password is generated and printed
    # ONCE to the server logs — operator must read it from logs and immediately
    # change it after first login.
    ADMIN_BOOTSTRAP_PASSWORD = _cfg("THRIVO_ADMIN_BOOTSTRAP_PASSWORD", "")

    # ── SMTP (optional — admin notification emails) ──
    SMTP_USER = _cfg("SMTP_USER", "")
    SMTP_PASS = _cfg("SMTP_PASS", "")
    SMTP_HOST = _cfg("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(_cfg("SMTP_PORT", 465))


# Back-compat aliases — so the rest of the codebase doesn't break
DATA_FILE            = AppConfig.DATA_FILE
APP_PASSWORD         = AppConfig.APP_PASSWORD
SCRAPE_URL           = AppConfig.SCRAPE_URL
SESSION_TIMEOUT      = AppConfig.SESSION_TIMEOUT
USERS_FILE           = AppConfig.USERS_FILE
SUBSCRIPTIONS_FILE   = AppConfig.SUBSCRIPTIONS_FILE
PAYMENT_PHONE_HASH   = AppConfig.PAYMENT_PHONE_HASH
PAYMENT_PHONE_MASKED = AppConfig.PAYMENT_PHONE_MASKED
ADMIN_EMAIL          = AppConfig.ADMIN_EMAIL


# ── Database / persistence layer ───────────────────────────────────────
# Auto-selects Postgres (if DATABASE_URL is set) or JSON files (local dev).
# All user data, subscriptions, public prices flow through this module.
import db

# ── Smart Buying Calendar (Pro+ feature) ────────────────────────────────
# Curated Egyptian retail calendar + data-driven analysis of scraped prices.
# All recommendations cite sources so users can verify in-app.
import buy_calendar


# ──────────────────────────────────────────────────────────────────────
#  GLOBAL USD/EGP RATE — used for all income conversion
# ──────────────────────────────────────────────────────────────────────
#  Resolution order (fastest first, falling back gracefully):
#    1. Live scrape from goldbullioneg.com — same source as Finance tab.
#       This is the authoritative path; works from any deploy with internet.
#    2. Public DB price snapshot (populated by GitHub Actions cron).
#    3. The bundled scripts/scrape_prices.py module's investing.com fetcher.
#    4. Hard fallback (50.0) — last resort, only when offline.
#
#  Note: this function does NOT read user data because it runs at module
#  scope (before the user's data is loaded). For per-user history fallback,
#  the page calls `_resolve_usd_rate(data)` which adds that tier.
@st.cache_data(ttl=1800)  # 30 min cache
def get_usd_egp_rate_global():
    # 1. Live scrape from goldbullioneg.com (same as Finance tab uses)
    try:
        r = requests.get(SCRAPE_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.content, 'html.parser')
        cells = soup.find_all(['td', 'th'])
        for i, c in enumerate(cells):
            txt = c.get_text(strip=True)
            if ("الدولار" in txt or "أمريكي" in txt) and i + 1 < len(cells):
                try:
                    rate = float(cells[i + 1].get_text(strip=True)
                                                  .replace(',', '').replace('EGP', '').strip())
                    if 5 < rate < 1000:  # sanity check
                        return rate
                except Exception:
                    pass
    except Exception:
        pass

    # 2. DB price snapshot (cron-populated)
    try:
        prices = db.load_prices()
        if prices:
            usd = prices.get("usd_egp") or {}
            rate = usd.get("rate")
            if rate and 5 < float(rate) < 1000:
                return float(rate)
    except Exception:
        pass

    # 3. Bundled scrape_prices.py investing.com fetcher
    try:
        import sys as _sys, os as _os
        scripts_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "scripts")
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)
        import importlib
        sp = importlib.import_module("scrape_prices")
        result = sp.fetch_usd_egp()
        if result and result.get("rate"):
            r = float(result["rate"])
            if 5 < r < 1000:
                try:
                    db.save_price("usd_egp", result)
                except Exception:
                    pass
                return r
    except Exception:
        pass

    # 4. Hard fallback — only hit when offline
    return 50.0


def _resolve_usd_rate(user_data: dict | None = None) -> float:
    """Return the best available USD/EGP rate. Use this at PAGE level —
    it consults user_data['price_history'] as an extra fallback before
    landing on the global 50.0 default."""
    rate = get_usd_egp_rate_global()
    if rate != 50.0:
        return rate
    # Per-user price_history fallback (Finance tab populates this)
    if user_data and user_data.get("price_history"):
        try:
            ph = user_data["price_history"]
            latest = sorted(ph.keys())[-1]
            saved = ph[latest].get("usd")
            if saved and 5 < float(saved) < 1000:
                return float(saved)
        except Exception:
            pass
    return 50.0


def amount_to_egp(amount, currency, usd_rate=None):
    """Convert any amount to EGP. Use this everywhere instead of summing
    raw amounts. Defaults to global USD rate but accepts an override."""
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        return 0.0
    cur = (currency or "EGP").upper()
    if cur == "USD":
        rate = usd_rate if usd_rate else get_usd_egp_rate_global()
        return amt * float(rate)
    return amt

def _send_admin_email(subject: str, body: str) -> bool:
    """
    Send notification email to admin via SMTP.
    Reads SMTP credentials from env vars (SMTP_USER, SMTP_PASS) or st.secrets.
    Fails silently if not configured — admin still sees notifications in the dashboard.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    smtp_user = AppConfig.SMTP_USER
    smtp_pass = AppConfig.SMTP_PASS
    if not smtp_user or not smtp_pass:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = AppConfig.ADMIN_EMAIL
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL(AppConfig.SMTP_HOST, AppConfig.SMTP_PORT, timeout=10) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, AppConfig.ADMIN_EMAIL, msg.as_string())
        return True
    except Exception:
        return False

PAYMENT_METHODS = {
    "instapay": {
        "label": "InstaPay",
        "icon": "📱",
        "instructions": f"Send to InstaPay number: **{PAYMENT_PHONE_MASKED}**",
        "note": "After sending, paste your transaction reference number below."
    },
    "wallet": {
        "label": "Mobile Wallet (Vodafone / Orange / Etisalat)",
        "icon": "👛",
        "instructions": f"Send to wallet number: **{PAYMENT_PHONE_MASKED}**",
        "note": "After sending, paste your transaction reference number below."
    },
    "visa": {
        "label": "Visa / Credit Card",
        "icon": "💳",
        "instructions": "Bank transfer details will be sent to your email after submission.",
        "note": "Paste your bank transfer reference number below."
    },
}

# ── Gemini API helper ──
def get_gemini_key():
    """Retrieve Gemini API key: session → user profile → env variable"""
    import os
    # 1. Session state (fastest)
    key = st.session_state.get("gemini_api_key", "")
    if key:
        return key
    # 2. User profile (persisted across sessions)
    username = st.session_state.get("auth_user", "")
    if username:
        try:
            users = _load_users() if "_users_cache" not in st.session_state else st.session_state["_users_cache"]
            key = users.get(username, {}).get("gemini_api_key", "")
            if key:
                st.session_state["gemini_api_key"] = key  # cache in session
                return key
        except Exception:
            pass
    # 3. Environment variable
    return os.environ.get("GEMINI_API_KEY", "")

def call_gemini(prompt_text, max_tokens=8192, temperature=0.7, model=None):
    """
    Call Google Gemini API with exponential backoff retry.

    Returns the FULL response text by joining all `parts` returned by the
    model. Earlier versions only read parts[0] which truncated multi-part
    responses to a single line. Default max_tokens raised from 1200 → 8192
    (the model's actual output cap) so long answers aren't cut short.

    Available models (set GEMINI_MODEL in sidebar or pass model= directly):
      - gemini-2.0-flash        → Fast, free tier, latest Flash (default)
      - gemini-2.5-flash-preview-05-20 → Most capable Flash, free tier
      - gemini-1.5-pro          → Pro quality, paid tier (higher quota)
      - gemini-2.5-pro-preview-06-05   → Most capable Pro, paid tier
    """
    api_key = get_gemini_key()
    if not api_key:
        return ("⚠️ No Gemini API key. Add it in the sidebar under 🔑 AI Settings. "
                "Get a free key at https://aistudio.google.com/app/apikey")

    if model is None:
        model = st.session_state.get("gemini_model", "gemini-2.5-flash")

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature":     temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    for attempt in range(4):
        try:
            resp = requests.post(url, json=payload,
                                 headers={"Content-Type": "application/json"}, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    # No candidates returned — usually a safety block
                    block = data.get("promptFeedback", {}).get("blockReason", "")
                    if block:
                        return f"⚠️ Gemini blocked the prompt: {block}. Try rephrasing."
                    return "⚠️ Gemini returned no response. Try rephrasing or simplifying your question."

                cand = candidates[0]
                # ── Collect ALL parts, not just parts[0] (the original bug) ──
                parts = (cand.get("content", {}) or {}).get("parts", []) or []
                text_chunks = [p.get("text", "") for p in parts if isinstance(p, dict)]
                full_text = "".join(c for c in text_chunks if c)

                finish = cand.get("finishReason", "")
                if not full_text:
                    # No text at all — explain why so users know
                    if finish == "SAFETY":
                        return ("⚠️ Gemini's safety filters blocked this response. "
                                "Try rephrasing your question without sensitive keywords.")
                    if finish == "RECITATION":
                        return "⚠️ Gemini blocked the response (recitation). Try rephrasing."
                    if finish == "MAX_TOKENS":
                        return ("⚠️ Gemini hit the token limit before producing output. "
                                "Try a shorter prompt.")
                    return f"⚠️ Gemini returned an empty response (finish reason: {finish or 'unknown'})."

                # Got text — note if truncated so the user knows there's more
                if finish == "MAX_TOKENS":
                    full_text += ("\n\n_…response was truncated at the token limit. "
                                  "Ask for a shorter or more focused answer to see the full reply._")
                return full_text

            if resp.status_code == 503:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                return "⚠️ Gemini rate limit reached. Wait a moment and try again (or switch to a different model)."
            if resp.status_code == 400:
                err = resp.json().get("error", {}).get("message", resp.text[:200])
                return f"⚠️ Bad request: {err}. Check your API key at aistudio.google.com."
            if resp.status_code == 404:
                return (f"⚠️ Model '{model}' not found. Go to the sidebar → 🔑 AI Settings and "
                        f"select a different model, or check https://ai.google.dev/gemini-api/docs/models")
            return f"⚠️ Gemini error {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            if attempt == 3:
                return f"⚠️ Connection error: {str(e)}"
            time.sleep(2 ** attempt)
    return "⚠️ Gemini service unavailable after retries. Try again later."

st.set_page_config(
    page_title=f"{AppConfig.NAME} — {AppConfig.TAGLINE}",
    page_icon=AppConfig.ICON,
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": f"**{AppConfig.NAME}** {AppConfig.VERSION} — {AppConfig.TAGLINE}",
    }
)

# ── PWA Support (optional — iOS home-screen install) ──
# If pwa_support.py is not present, this fails silently and the app
# continues to work as a normal web app.
try:
    from pwa_support import inject_pwa
    inject_pwa()
except ImportError:
    pass

# ==========================================
# USER AUTH & SUBSCRIPTION SYSTEM
# ==========================================
# (hashlib and re already imported at top)

# ── All available tabs in the app ──
ALL_TABS = [
    ("📊", "Daily Tracker",    "Daily Tracker"),
    ("💎", "Finance Hub Pro",  "FinanceDash"),   # NEW — unified financial dashboard
    ("💰", "Gold & Dollar",    "Gold"),
    ("💸", "Finance Hub",      "Finance"),
    ("💳", "Credit Tracker",   "Credit"),
    ("🏋️", "Gym Tracker",      "Gym"),
    ("📈", "EGX Stocks",       "Stocks"),
    ("🛒", "Smart Buying",     "BuyTime"),       # NEW v10 — Egyptian buying calendar (Pro+)
    ("🎯", "Goal OS",          "GoalOS"),        # NEW — OKR-style goals
    ("✅", "Habit Tracker",    "Habits"),        # NEW — 21-day habit grid
    ("⏱️", "Focus Timer",      "Pomodoro"),      # NEW — pomodoro
    ("🏪", "Business Tracker", "Business"),
    ("🗂️", "Agile Board",      "Agile"),
    ("🤖", "AI Adviser",       "AIAdviser"),
    ("📚", "Library",          "Library"),
    ("📝", "Notes",            "Notes"),
    ("✍️", "AI Journal",       "Journal"),
    ("🔥", "Streak Arena",     "Streaks"),
    ("🍽️", "AI Chef",          "Chef"),
    ("🌍", "Language Lab",     "Language"),
    ("🏆", "Reports",          "Reports"),
]


# ──────────────────────────────────────────────────────────────────────
#  SIDEBAR GROUPING — logical categories so 20+ tabs don't overwhelm
# ──────────────────────────────────────────────────────────────────────
# Order of groups = display order in sidebar. Tabs not in any group fall
# into "Other" automatically. Each group has an icon + display title.
TAB_GROUPS = [
    ("🏠 Today",          ["Daily Tracker", "Reports"]),
    ("💰 Money",          ["FinanceDash", "Finance", "Gold", "Credit",
                           "Stocks", "BuyTime"]),
    ("🚀 Growth",         ["GoalOS", "Habits", "Pomodoro", "Streaks"]),
    ("❤️ Wellbeing",      ["Gym", "Chef", "Language", "Journal"]),
    ("🛠️ Workspace",      ["Business", "Agile", "Library", "Notes"]),
    ("🤖 AI",             ["AIAdviser"]),
]

SUBSCRIPTION_PLANS = {
    "Free": {
        "price": 0,
        "tabs": ["Daily Tracker", "Habits", "Pomodoro", "Journal", "Streaks", "Chef", "Language", "Notes"],
        "description": "Daily tracker, habits, focus timer & notes",
        "color": "#64748b"
    },
    "Starter": {
        "price": 49,
        "tabs": ["Daily Tracker", "Habits", "Pomodoro", "Gold", "Finance", "Library", "Journal", "Streaks", "Chef", "Language", "Notes", "Reports"],
        "description": "Finance + Gold + Habits + Focus Timer",
        "color": "#3b82f6"
    },
    "Pro": {
        "price": 149,
        "tabs": ["Daily Tracker", "FinanceDash", "Habits", "Pomodoro", "GoalOS", "Gold", "Finance", "Credit", "Gym", "Stocks", "BuyTime", "Library", "Journal", "Streaks", "Chef", "Language", "Notes", "Reports"],
        "description": "Full personal finance + stocks + Goal OS + Smart Buying",
        "color": "#a78bfa"
    },
    "Business": {
        "price": 299,
        "tabs": [t[2] for t in ALL_TABS],  # all tabs — Business & Admin get everything
        "description": "Everything — business + agile + AI adviser",
        "color": "#22c55e"
    },
    "Admin": {
        "price": 0,
        "tabs": [t[2] for t in ALL_TABS],
        "description": "Admin — full access + user management",
        "color": "#f97316"
    }
}

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def _sanitize(s: str) -> str:
    """Strip any dangerous characters to prevent injection"""
    return _re.sub(r'[^\w\s@._\-]', '', str(s))[:100]

def _generate_admin_bootstrap_password() -> str:
    """Resolve the admin's first-run password.
    Priority:
      1. THRIVO_ADMIN_BOOTSTRAP_PASSWORD env var / st.secrets (production-set)
      2. A cryptographically-random 16-char password generated at first run
         and printed to server logs ONCE — operator must log in and change it.
    Never hardcoded in source.
    """
    if AppConfig.ADMIN_BOOTSTRAP_PASSWORD:
        return str(AppConfig.ADMIN_BOOTSTRAP_PASSWORD)
    # Generate a random one — printed to logs once
    import secrets, string
    alphabet = string.ascii_letters + string.digits
    pw = "".join(secrets.choice(alphabet) for _ in range(16))
    # Print to server stdout (Streamlit Cloud / Render show this in deploy logs)
    print("=" * 70)
    print("🔐 THRIVO FIRST-RUN ADMIN PASSWORD (write this down NOW):")
    print(f"   username: admin")
    print(f"   password: {pw}")
    print("   Log in immediately and change it. This password will not be shown again.")
    print("   To set a fixed bootstrap password, set THRIVO_ADMIN_BOOTSTRAP_PASSWORD env var.")
    print("=" * 70)
    return pw

def _load_users():
    """Load all users from the active backend (Postgres or JSON files).
    On first run with an empty users table, bootstraps a default admin account
    with a password from THRIVO_ADMIN_BOOTSTRAP_PASSWORD or a random one."""
    users = db.load_users()
    if not users:
        bootstrap_pw = _generate_admin_bootstrap_password()
        admin = {
            "admin": {
                "password_hash": _hash_password(bootstrap_pw),
                "plan":         "Admin",
                "email":        AppConfig.ADMIN_EMAIL or "admin@thrivo.app",
                "approved":     True,
                "is_admin":     True,
                "created_at":   datetime.datetime.utcnow().isoformat(),
                "custom_tabs":  None,
                "display_name": "Admin",
                "must_change_password": True,  # forces change on first login
            }
        }
        db.save_users(admin)
        return admin
    # Forward-compat: ensure required keys exist on every user
    for uname, u in users.items():
        u.setdefault("custom_tabs", None)
        u.setdefault("display_name", uname.capitalize())
    return users

def _save_users(users: dict):
    """Persist users dict via the active backend."""
    db.save_users(users)

def _get_user_tabs(username: str, users: dict) -> list:
    user = users.get(username, {})
    custom = user.get("custom_tabs")
    if custom is not None:
        return custom
    plan = user.get("plan", "Free")
    return SUBSCRIPTION_PLANS.get(plan, SUBSCRIPTION_PLANS["Free"])["tabs"]


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC PRICES — visible to anyone, no login needed
# ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=21600)  # 6 hours — refresh covers cron schedule + buffer
def _fetch_public_prices_cached() -> dict:
    """
    Strategy:
      1. Try Postgres (db.load_prices) — populated by cron job.
      2. Fall back to public_prices.json — committed by GitHub Actions.
      3. Last resort — run the scraper live (slow first call, but always works).
    """
    out: dict = {}

    # Source 1: DB
    try:
        db_prices = db.load_prices()
        if db_prices:
            out.update(db_prices)
            out["_source"] = f"database ({db.get_backend_kind()})"
            return out
    except Exception:
        pass

    # Source 2: JSON snapshot in repo (written by cron)
    try:
        if os.path.exists("public_prices.json"):
            with open("public_prices.json", "r", encoding="utf-8") as f:
                snap = json.load(f)
            out.update(snap)
            out["_source"] = "snapshot file"
            return out
    except Exception:
        pass

    # Source 3: Live scrape — only if both above failed (rare)
    try:
        import importlib, sys as _sys, os as _os
        scripts_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "scripts")
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)
        sp = importlib.import_module("scrape_prices")
        out["gold"]    = sp.fetch_gold()
        out["usd_egp"] = sp.fetch_usd_egp()
        out["btc"]     = sp.fetch_btc()
        out["egx"]     = sp.fetch_egx()
        out["_source"] = "live scrape (cron unavailable)"
    except Exception:
        out["_source"] = "unavailable"

    return out


def _render_public_prices_card():
    """Render the live-prices ticker shown on the public landing page."""
    prices = _fetch_public_prices_cached()
    if not prices or all(v in (None, {}, []) for k, v in prices.items() if not k.startswith("_")):
        return  # don't render anything if all sources are dry

    st.markdown(
        "<div style='max-width:880px;margin:0 auto;padding:0 16px;'>"
        "<div style='color:#64748b;font-size:0.78rem;text-transform:uppercase;"
        "letter-spacing:0.12em;margin-bottom:8px;text-align:center;'>"
        "🟢 Live Markets · No Login Required</div></div>",
        unsafe_allow_html=True,
    )

    cols = st.columns(4)

    # Gold 21k (primary indicator for Egypt audience)
    with cols[0]:
        gold = prices.get("gold") or {}
        v21 = (gold.get("values", {}) or {}).get("k21")
        _price_tile("🥇 Gold 21k", f"{v21:,.0f} EGP" if v21 else "—",
                    "per gram", "#f59e0b" if v21 else "#475569")

    # USD/EGP
    with cols[1]:
        usd = prices.get("usd_egp") or {}
        rate = usd.get("rate")
        chg = usd.get("change_pct", 0) or 0
        _price_tile("💵 USD/EGP", f"{rate:.4f}" if rate else "—",
                    f"{chg:+.2f}% vs prev" if rate else "—",
                    "#22c55e" if chg >= 0 else "#ef4444")

    # BTC
    with cols[2]:
        btc = prices.get("btc") or {}
        usd_v = btc.get("usd")
        chg_b = btc.get("change_pct_24h", 0) or 0
        _price_tile("₿ Bitcoin", f"${usd_v:,.0f}" if usd_v else "—",
                    f"{chg_b:+.2f}% 24h" if usd_v else "—",
                    "#22c55e" if chg_b >= 0 else "#ef4444")

    # EGX top stock (CIB — most-traded)
    with cols[3]:
        egx_list = prices.get("egx") or []
        cib = next((s for s in egx_list if s.get("ticker") == "COMI"), None) \
              if isinstance(egx_list, list) else None
        if cib:
            _price_tile("🏦 EGX · COMI", f"{cib['price']:,.2f} EGP",
                        f"{cib['change_pct']:+.2f}%",
                        "#22c55e" if cib["change_pct"] >= 0 else "#ef4444")
        else:
            _price_tile("🏦 EGX", "—", "Sun-Thu 10:00", "#475569")

    # Footer with source + last update
    src      = prices.get("_source", "—")
    run_at   = prices.get("_run_at", "")
    upd_text = ""
    if run_at:
        try:
            t = datetime.datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            hours_ago = (datetime.datetime.utcnow() - t.replace(tzinfo=None)).total_seconds() / 3600
            upd_text = f" · updated {hours_ago:.0f}h ago" if hours_ago < 48 else f" · {t.strftime('%Y-%m-%d')}"
        except Exception:
            pass
    st.markdown(
        f"<div style='text-align:center;color:#475569;font-size:0.72rem;margin:8px 0 24px;'>"
        f"📡 source: {src}{upd_text}</div>",
        unsafe_allow_html=True,
    )


def _price_tile(title: str, value: str, sub: str, color: str):
    st.markdown(
        f"""<div style='background:#0d1b2a;border:1px solid #1e3a5f;border-radius:12px;
            padding:14px 12px;text-align:center;margin-bottom:6px;'>
            <div style='color:#64748b;font-size:0.7rem;text-transform:uppercase;
                letter-spacing:0.08em;'>{title}</div>
            <div style='color:#e2e8f0;font-family:"JetBrains Mono",monospace;
                font-size:1.25rem;font-weight:700;margin:4px 0;'>{value}</div>
            <div style='color:{color};font-size:0.72rem;font-weight:600;'>{sub}</div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_auth():
    """Render login / register page. Returns True if authenticated."""
    if st.session_state.get("auth_user"):
        return True

    st.markdown(f"""
    <div style='text-align:center; padding:48px 0 10px;'>
        <div style='font-size:3.8rem; line-height:1;'>{AppConfig.ICON}</div>
        <h1 style='color:#e2e8f0; font-size:2.4rem; margin:14px 0 4px;
                   background: linear-gradient(135deg, #22c55e 0%, #3b82f6 100%);
                   -webkit-background-clip: text; -webkit-text-fill-color: transparent;
                   background-clip: text; font-weight:800; letter-spacing:-0.02em;'>
            {AppConfig.NAME}
        </h1>
        <p style='color:#64748b; font-size:0.98rem; margin-top:2px;'>{AppConfig.TAGLINE}</p>
        <p style='color:#334155; font-size:0.75rem; margin-top:4px; letter-spacing:0.15em;'>
            THE PERSONAL GROWTH OPERATING SYSTEM
        </p>
    </div>
    """, unsafe_allow_html=True)

    # ── Public price ticker — visible WITHOUT login ──
    _render_public_prices_card()

    col1, col2, col3 = st.columns([1, 1.4, 1])
    with col2:
        auth_tab, reg_tab = st.tabs(["🔑 Sign In", "📝 Sign Up"])

        users = _load_users()

        with auth_tab:
            st.markdown("<br>", unsafe_allow_html=True)
            username_in = st.text_input("Username", key="login_user", placeholder="your username")
            password_in = st.text_input("Password", type="password", key="login_pass", placeholder="••••••••")

            if st.button("Sign In →", use_container_width=True, type="primary", key="signin_btn"):
                un = _sanitize(username_in).strip().lower()
                if not un or not password_in:
                    st.error("Please enter both username and password.")
                elif un not in users:
                    st.error("Username not found.")
                elif users[un]["password_hash"] != _hash_password(password_in):
                    st.error("Incorrect password.")
                else:
                    st.session_state["auth_user"] = un
                    st.session_state["auth_plan"] = users[un].get("plan","Free")
                    st.session_state["auth_is_admin"] = users[un].get("is_admin", False)
                    st.session_state["auth_tabs"] = _get_user_tabs(un, users)
                    st.session_state["page"] = st.session_state["auth_tabs"][0] if st.session_state["auth_tabs"] else "Daily Tracker"
                    # Auto-load saved Gemini key and model
                    saved_key = users[un].get("gemini_api_key", "")
                    if saved_key:
                        st.session_state["gemini_api_key"] = saved_key
                    saved_model = users[un].get("gemini_model", "gemini-2.0-flash")
                    st.session_state["gemini_model"] = saved_model
                    st.rerun()

        with reg_tab:
            step = st.session_state.get("signup_step", 1)

            # ── STEP 1: Account details ──
            if step == 1:
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("**Step 1 of 3 — Account Details**")
                new_user  = st.text_input("Username", key="reg_user",  placeholder="letters, numbers, underscore")
                new_email = st.text_input("Email",    key="reg_email", placeholder="you@example.com")
                new_pass  = st.text_input("Password (min 8 chars)", type="password", key="reg_pass")
                new_pass2 = st.text_input("Confirm Password",       type="password", key="reg_pass2")

                if st.button("Next: Choose Plan →", use_container_width=True, type="primary", key="step1_btn"):
                    un = _sanitize(new_user).strip().lower()
                    em = _sanitize(new_email).strip()
                    errors = []
                    if not _re.match(r'^[a-z0-9_]{3,30}$', un):
                        errors.append("Username: 3-30 chars, letters/numbers/underscore only.")
                    if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', em):
                        errors.append("Invalid email address.")
                    if len(new_pass) < 8:
                        errors.append("Password must be at least 8 characters.")
                    if new_pass != new_pass2:
                        errors.append("Passwords do not match.")
                    if un in users:
                        errors.append("Username already taken.")
                    if errors:
                        for e in errors: st.error(e)
                    else:
                        st.session_state["reg_data"] = {"username": un, "email": em,
                                                         "password_hash": _hash_password(new_pass)}
                        st.session_state["signup_step"] = 2
                        st.rerun()

            # ── STEP 2: Plan selection ──
            elif step == 2:
                st.markdown("**Step 2 of 3 — Choose Your Plan**")
                reg_data = st.session_state.get("reg_data", {})
                st.markdown(f"<div style='color:#60a5fa;font-size:0.82rem;margin-bottom:8px;'>Account: <b>{reg_data.get('username','?')}</b></div>", unsafe_allow_html=True)

                # Plan cards
                plan_choices = [p for p in SUBSCRIPTION_PLANS if p not in ("Admin", "Free")]
                # Free plan also available
                plan_choices = ["Free"] + plan_choices
                chosen_plan = st.radio("Select a plan", plan_choices,
                    format_func=lambda p: f"{p}  —  {SUBSCRIPTION_PLANS[p]['description']}  ({SUBSCRIPTION_PLANS[p]['price']} EGP/mo)",
                    key="reg_plan_radio")

                # Show plan features
                plan_info = SUBSCRIPTION_PLANS.get(chosen_plan, {})
                pcolor = plan_info.get("color", "#64748b")
                st.markdown(
                    f"<div style='background:#0d1b2a;border:1px solid {pcolor}44;border-radius:10px;padding:12px 16px;margin:8px 0;'>"
                    f"<div style='color:{pcolor};font-weight:700;'>{chosen_plan} — {plan_info.get('price',0)} EGP/mo</div>"
                    f"<div style='color:#64748b;font-size:0.8rem;margin-top:4px;'>"
                    f"Includes: {', '.join(plan_info.get('tabs',[])[:6])}"
                    f"{'...' if len(plan_info.get('tabs',[]))>6 else ''}</div></div>",
                    unsafe_allow_html=True)

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("← Back", key="step2_back", use_container_width=True):
                        st.session_state["signup_step"] = 1
                        st.rerun()
                with c2:
                    btn_label = "Next: Payment →" if SUBSCRIPTION_PLANS.get(chosen_plan,{}).get("price",0) > 0 else "Create Account (Free) →"
                    if st.button(btn_label, use_container_width=True, type="primary", key="step2_btn"):
                        st.session_state["reg_data"]["plan"] = chosen_plan
                        if SUBSCRIPTION_PLANS.get(chosen_plan,{}).get("price",0) == 0:
                            st.session_state["signup_step"] = 4  # skip payment for free
                        else:
                            st.session_state["signup_step"] = 3
                        st.rerun()

            # ── STEP 3: Payment ──
            elif step == 3:
                reg_data  = st.session_state.get("reg_data", {})
                plan_name = reg_data.get("plan", "Starter")
                price     = SUBSCRIPTION_PLANS.get(plan_name, {}).get("price", 0)
                pcolor    = SUBSCRIPTION_PLANS.get(plan_name, {}).get("color", "#3b82f6")

                st.markdown(f"**Step 3 of 3 — Payment for {plan_name} ({price} EGP/mo)**")
                st.markdown(
                    f"<div style='background:#0d1b2a;border:1px solid {pcolor};border-radius:10px;padding:14px;margin-bottom:10px;'>"
                    f"<span style='color:{pcolor};font-weight:700;'>Amount due: {price} EGP/month</span>"
                    f"</div>", unsafe_allow_html=True)

                pay_method = st.radio("Payment Method", list(PAYMENT_METHODS.keys()),
                    format_func=lambda k: f"{PAYMENT_METHODS[k]['icon']} {PAYMENT_METHODS[k]['label']}",
                    key="pay_method_radio", horizontal=True)

                pm = PAYMENT_METHODS[pay_method]
                st.markdown(
                    f"<div style='background:#0d1117;border-radius:8px;padding:14px;margin:10px 0;'>"
                    f"<div style='color:#60a5fa;font-size:0.85rem;font-weight:600;'>{pm['icon']} {pm['label']}</div>"
                    f"<div style='color:#e2e8f0;margin:8px 0;'>{pm['instructions']}</div>"
                    f"<div style='color:#475569;font-size:0.78rem;'>{pm['note']}</div>"
                    f"</div>", unsafe_allow_html=True)

                # Security note about phone
                st.markdown(
                    "<div class='insight-card' style='font-size:0.75rem;'>"
                    "🔒 The payment number above is shown in masked form for security. "
                    "Your payment reference is stored securely and never shared."
                    "</div>", unsafe_allow_html=True)

                tx_ref = st.text_input("Transaction / Reference Number *",
                    placeholder="Paste your payment reference or transaction ID here",
                    key="tx_ref_input")
                extra_note = st.text_area("Additional Notes (optional)",
                    placeholder="e.g. I paid on 15 April at 3pm via Vodafone Cash",
                    height=70, key="pay_note")

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("← Back", key="step3_back", use_container_width=True):
                        st.session_state["signup_step"] = 2
                        st.rerun()
                with c2:
                    if st.button("✅ Submit & Request Approval", use_container_width=True,
                                 type="primary", key="step3_btn"):
                        if not tx_ref.strip():
                            st.error("Please enter your transaction reference number.")
                        else:
                            st.session_state["reg_data"]["payment_method"] = pay_method
                            st.session_state["reg_data"]["tx_ref"]         = tx_ref.strip()
                            st.session_state["reg_data"]["pay_note"]        = extra_note.strip()
                            st.session_state["signup_step"] = 4
                            st.rerun()

            # ── STEP 4: Save account ──
            elif step == 4:
                reg_data  = st.session_state.get("reg_data", {})
                plan_name = reg_data.get("plan", "Free")
                price     = SUBSCRIPTION_PLANS.get(plan_name, {}).get("price", 0)
                is_free   = price == 0
                # Free accounts are auto-approved; paid accounts need admin approval
                status    = "approved" if is_free else "pending"

                new_user_record = {
                    "password_hash": reg_data["password_hash"],
                    "plan":          plan_name,
                    "email":         reg_data["email"],
                    "created":       datetime.date.today().isoformat(),
                    "is_admin":      False,
                    "custom_tabs":   None,
                    "display_name":  reg_data["username"],
                    "status":        status,
                    "payment": {
                        "method":    reg_data.get("payment_method", ""),
                        "tx_ref":    reg_data.get("tx_ref", ""),
                        "note":      reg_data.get("pay_note", ""),
                        # Store hash of phone for audit — NOT the phone itself
                        "phone_hash": PAYMENT_PHONE_HASH,
                        "amount":    price,
                        "currency":  "EGP",
                        "submitted_at": datetime.datetime.now().isoformat()
                    } if not is_free else {}
                }
                users[reg_data["username"]] = new_user_record
                _save_users(users)

                # Send email notification to admin
                email_body = f"""
<html><body style="font-family:sans-serif;background:#0d1117;color:#e2e8f0;padding:20px;">
<h2 style="color:#22c55e;">🌱 New Thrivo Signup</h2>
<table style="border-collapse:collapse;width:100%;">
  <tr><td style="padding:6px;color:#94a3b8;width:160px;">Username</td><td style="padding:6px;color:#e2e8f0;"><b>{reg_data["username"]}</b></td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Email</td><td style="padding:6px;color:#e2e8f0;">{reg_data["email"]}</td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Plan</td><td style="padding:6px;color:#a78bfa;"><b>{plan_name} — {price} EGP/mo</b></td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Payment Method</td><td style="padding:6px;color:#e2e8f0;">{reg_data.get("payment_method","N/A (Free)")}</td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Transaction Ref</td><td style="padding:6px;color:#22c55e;"><b>{reg_data.get("tx_ref","—")}</b></td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Note</td><td style="padding:6px;color:#e2e8f0;">{reg_data.get("pay_note","—")}</td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Date</td><td style="padding:6px;color:#e2e8f0;">{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</td></tr>
  <tr><td style="padding:6px;color:#94a3b8;">Status</td><td style="padding:6px;color:{"#22c55e" if is_free else "#f97316"};"><b>{"✅ Auto-approved (free)" if is_free else "⏳ Awaiting your approval"}</b></td></tr>
</table>
{"<p style='color:#f97316;margin-top:16px;'>👉 <b>Action required:</b> Log in to the Admin Panel to approve or reject this account.</p>" if not is_free else ""}
<hr style="border-color:#1e293b;margin-top:20px;">
<p style="color:#475569;font-size:12px;">Thrivo · Automated notification</p>
</body></html>"""

                email_sent = _send_admin_email(
                    subject=f"🆕 New {'Paid' if not is_free else 'Free'} Signup — {reg_data['username']} ({plan_name})",
                    body=email_body
                )

                # Clear signup session state
                for k in ["signup_step", "reg_data"]:
                    st.session_state.pop(k, None)

                if is_free:
                    st.success(f"✅ Free account created! You can sign in now.")
                else:
                    st.markdown(f"""
                    <div style='background:#1c1000;border:1px solid #f97316;border-radius:12px;padding:20px;text-align:center;margin-top:8px;'>
                        <div style='font-size:2rem;'>⏳</div>
                        <div style='color:#fb923c;font-weight:700;font-size:1.1rem;margin:8px 0;'>Account Submitted!</div>
                        <div style='color:#94a3b8;font-size:0.87rem;'>
                            Your <b style='color:#a78bfa;'>{plan_name}</b> account is under review.<br>
                            We'll contact you at <b>{reg_data["email"]}</b> once approved.<br><br>
                            <span style='color:#475569;font-size:0.78rem;'>
                            {"📧 Admin notified by email." if email_sent else "📋 Admin notified in dashboard."}
                            </span>
                        </div>
                    </div>""", unsafe_allow_html=True)

        # ── Plan overview at bottom ──
        st.divider()
        st.markdown("<h4 style='color:#94a3b8; text-align:center; font-size:0.9rem;'>📦 Subscription Plans</h4>", unsafe_allow_html=True)
        plan_cols = st.columns(4)
        for i, (pname, pdata) in enumerate([p for p in SUBSCRIPTION_PLANS.items() if p[0] != "Admin"]):
            with plan_cols[i]:
                st.markdown(f"""
                <div style='background:#0d1b2a; border:1px solid {pdata["color"]}44; border-radius:10px;
                     padding:12px; text-align:center;'>
                    <div style='color:{pdata["color"]}; font-weight:700; font-size:0.9rem;'>{pname}</div>
                    <div style='color:#e2e8f0; font-size:1.2rem; font-weight:700; margin:4px 0;'>{pdata["price"]} EGP<span style='font-size:0.7rem; color:#475569;'>/mo</span></div>
                    <div style='color:#64748b; font-size:0.72rem;'>{pdata["description"]}</div>
                </div>""", unsafe_allow_html=True)

    return False


def render_admin_panel(users: dict):
    """Admin user management panel with approval queue."""
    st.title("⚙️ Admin Panel — Thrivo")

    # ── DATABASE & BACKUP STATUS ──
    backend_kind   = db.get_backend_kind()
    init_err       = db.get_init_error()
    backup_status  = db.get_backup_status()

    backend_color  = ("#22c55e" if backend_kind == "Postgres"
                      else "#3b82f6" if backend_kind == "SQLite"
                      else "#f59e0b")

    with st.expander(f"🗄️ Database & Backup ({backend_kind})", expanded=False):
        col_a, col_b = st.columns(2)
        with col_a:
            if backend_kind == "Postgres":
                _backend_caption = "Postgres URL is set — production mode."
            elif backend_kind == "SQLite":
                _backend_caption = "Single SQLite file (thrivo.db). Add DATABASE_URL env to switch to Postgres."
            else:
                _backend_caption = "Fallback JSON storage. SQLite & Postgres both unavailable."
            st.markdown(
                f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                f"border-left:3px solid {backend_color};border-radius:10px;padding:12px 16px;'>"
                f"<div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;"
                f"letter-spacing:0.1em;margin-bottom:4px;'>Active Backend</div>"
                f"<div style='color:{backend_color};font-size:1.2rem;font-weight:700;'>{backend_kind}</div>"
                f"<div style='color:var(--text-muted);font-size:0.78rem;margin-top:4px;'>"
                f"{_backend_caption}"
                f"</div></div>",
                unsafe_allow_html=True,
            )
            if init_err:
                st.warning(f"⚠️ {init_err}")

        with col_b:
            if backend_kind == "SQLite":
                bup_color = "#22c55e" if backup_status["configured"] else "#64748b"
                bup_text  = ("Configured" if backup_status["configured"]
                             else "NOT configured — data is at risk on Streamlit Cloud!")
                if backup_status["configured"]:
                    _bup_detail = (f"Repo: {backup_status['repo']}<br>"
                                   f"Branch: {backup_status['branch']}<br>"
                                   f"Last push: {backup_status['last_push']}")
                else:
                    _bup_detail = "Set THRIVO_BACKUP_PAT and THRIVO_BACKUP_REPO env vars / secrets."
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                    f"border-left:3px solid {bup_color};border-radius:10px;padding:12px 16px;'>"
                    f"<div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;"
                    f"letter-spacing:0.1em;margin-bottom:4px;'>GitHub Backup</div>"
                    f"<div style='color:{bup_color};font-size:1rem;font-weight:600;'>{bup_text}</div>"
                    f"<div style='color:var(--text-muted);font-size:0.78rem;margin-top:4px;'>"
                    f"{_bup_detail}"
                    f"</div></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.info(f"Backup mechanism is SQLite-only. {backend_kind} has its own persistence.")

        st.markdown("")
        col_x, col_y = st.columns(2)
        with col_x:
            if backend_kind == "SQLite" and backup_status["configured"]:
                if st.button("🔄 Force backup now", use_container_width=True):
                    ok, msg = db.force_backup_now()
                    if ok:
                        st.success(f"✓ Backed up: {msg}")
                    else:
                        st.error(f"✗ Failed: {msg}")
        with col_y:
            if backend_kind == "SQLite":
                db_bytes = db.export_db_bytes()
                if db_bytes:
                    st.download_button(
                        f"📥 Download thrivo.db ({len(db_bytes)/1024:.1f} KB)",
                        data=db_bytes,
                        file_name=f"thrivo_{datetime.date.today().isoformat()}.db",
                        mime="application/octet-stream",
                        use_container_width=True,
                    )

    # ── PENDING APPROVALS (shown first if any) ──
    pending = {u: d for u, d in users.items() if d.get("status") == "pending"}
    if pending:
        st.markdown(f"""
        <div style='background:#1c0e00;border:2px solid #f97316;border-radius:12px;padding:16px 20px;margin-bottom:16px;'>
            <div style='color:#fb923c;font-weight:700;font-size:1rem;'>⏳ {len(pending)} Account(s) Awaiting Approval</div>
        </div>""", unsafe_allow_html=True)

        for uname, udata in pending.items():
            pay = udata.get("payment", {})
            plan_name = udata.get("plan","?")
            price = SUBSCRIPTION_PLANS.get(plan_name,{}).get("price",0)
            pm_label = PAYMENT_METHODS.get(pay.get("method",""),{}).get("label", pay.get("method","—"))

            with st.expander(f"👤 {uname}  —  {plan_name} ({price} EGP/mo)  —  {udata.get('email','')}"):
                pc1, pc2 = st.columns(2)
                with pc1:
                    st.markdown(f"""
                    <div style='background:#0d1b2a;border-radius:8px;padding:14px;font-size:0.85rem;'>
                        <div style='color:#60a5fa;font-weight:600;margin-bottom:8px;'>Account Info</div>
                        <div><span style='color:#475569;'>Username:</span> <span style='color:#e2e8f0;'>{uname}</span></div>
                        <div><span style='color:#475569;'>Email:</span> <span style='color:#e2e8f0;'>{udata.get("email","")}</span></div>
                        <div><span style='color:#475569;'>Plan:</span> <span style='color:#a78bfa;'>{plan_name} — {price} EGP/mo</span></div>
                        <div><span style='color:#475569;'>Registered:</span> <span style='color:#94a3b8;'>{udata.get("created","")}</span></div>
                    </div>""", unsafe_allow_html=True)
                with pc2:
                    st.markdown(f"""
                    <div style='background:#0d1b2a;border-radius:8px;padding:14px;font-size:0.85rem;'>
                        <div style='color:#22c55e;font-weight:600;margin-bottom:8px;'>Payment Details</div>
                        <div><span style='color:#475569;'>Method:</span> <span style='color:#e2e8f0;'>{pm_label}</span></div>
                        <div><span style='color:#475569;'>Amount:</span> <span style='color:#22c55e;font-weight:700;'>{pay.get("amount","?")} EGP</span></div>
                        <div><span style='color:#475569;'>Reference:</span> <span style='color:#facc15;font-family:JetBrains Mono,monospace;'>{pay.get("tx_ref","—")}</span></div>
                        <div><span style='color:#475569;'>Submitted:</span> <span style='color:#94a3b8;'>{pay.get("submitted_at","")[:16]}</span></div>
                        <div style='margin-top:6px;color:#94a3b8;font-size:0.78rem;'>{pay.get("note","")}</div>
                        <div style='margin-top:6px;color:#334155;font-size:0.72rem;'>
                            🔒 Phone hash (audit): {pay.get("phone_hash","")[:16]}...
                        </div>
                    </div>""", unsafe_allow_html=True)

                ap_col, rj_col = st.columns(2)
                with ap_col:
                    if st.button(f"✅ Approve {uname}", key=f"approve_{uname}",
                                 use_container_width=True, type="primary"):
                        users[uname]["status"] = "approved"
                        _save_users(users)
                        # Notify user by email
                        body = f"""<html><body style="font-family:sans-serif;background:#0d1117;color:#e2e8f0;padding:20px;">
<h2 style="color:#22c55e;">✅ Your Thrivo Account is Approved!</h2>
<p>Hi <b>{uname}</b>,</p>
<p>Your <b style="color:#a78bfa;">{plan_name}</b> account has been approved. You can now sign in at your app URL.</p>
<p style="color:#475569;font-size:12px;">Thrivo Team</p></body></html>"""
                        _send_admin_email(
                            subject=f"✅ Account Approved — Thrivo ({uname})",
                            body=body)
                        st.success(f"✅ {uname} approved!")
                        st.rerun()
                with rj_col:
                    if st.button(f"❌ Reject {uname}", key=f"reject_{uname}",
                                 use_container_width=True):
                        users[uname]["status"] = "rejected"
                        _save_users(users)
                        st.warning(f"❌ {uname} rejected.")
                        st.rerun()
        st.divider()

    # ── USER MANAGEMENT ──
    st.subheader("👥 Manage Users")
    user_list = [u for u in users.keys()]
    selected_user = st.selectbox("Select user", user_list, key="admin_sel_user")
    u = users[selected_user]
    status_badge = {"approved":"✅ Active","pending":"⏳ Pending","rejected":"❌ Rejected"}.get(u.get("status","approved"),"✅")

    ac1, ac2 = st.columns(2)
    with ac1:
        plan_name_u = u.get("plan","Free")
        price_u = SUBSCRIPTION_PLANS.get(plan_name_u,{}).get("price",0)
        st.markdown(f"""
        <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:10px; padding:16px; margin-bottom:12px;'>
            <div style='color:#60a5fa; font-weight:700;'>{selected_user} <span style='font-size:0.8rem;color:#94a3b8;'>({status_badge})</span></div>
            <div style='color:#64748b; font-size:0.8rem;'>📧 {u.get("email","")}</div>
            <div style='color:#64748b; font-size:0.8rem;'>📅 Joined {u.get("created","")}</div>
            <div style='color:#a78bfa; font-size:0.85rem; margin-top:4px;'>{plan_name_u} — {price_u} EGP/mo</div>
        </div>""", unsafe_allow_html=True)

        new_plan = st.selectbox("Change Plan", list(SUBSCRIPTION_PLANS.keys()),
            index=list(SUBSCRIPTION_PLANS.keys()).index(u.get("plan","Free")),
            key=f"admin_plan_{selected_user}")
        new_status = st.selectbox("Account Status", ["approved","pending","rejected"],
            index=["approved","pending","rejected"].index(u.get("status","approved")),
            key=f"admin_status_{selected_user}")

    with ac2:
        current_custom = u.get("custom_tabs", None)
        all_tab_keys = [t[2] for t in ALL_TABS]
        default_sel = current_custom if current_custom else SUBSCRIPTION_PLANS.get(u.get("plan","Free"),{}).get("tabs",[])
        custom_tabs_sel = st.multiselect("Custom Tab Access",
            options=all_tab_keys,
            default=[t for t in default_sel if t in all_tab_keys],
            key=f"admin_tabs_{selected_user}")
        use_custom = st.checkbox("Override plan with custom tabs",
            value=current_custom is not None, key=f"admin_use_custom_{selected_user}")

        # Show payment info if exists
        pay_u = u.get("payment", {})
        if pay_u:
            pm_label = PAYMENT_METHODS.get(pay_u.get("method",""),{}).get("label","—")
            st.markdown(f"""
            <div style='background:#052e16;border:1px solid #16a34a;border-radius:8px;padding:10px;font-size:0.78rem;margin-top:8px;'>
                <div style='color:#22c55e;font-weight:600;'>💳 Payment on File</div>
                <div style='color:#94a3b8;'>Method: {pm_label}</div>
                <div style='color:#facc15;font-family:JetBrains Mono,monospace;'>Ref: {pay_u.get("tx_ref","—")}</div>
                <div style='color:#475569;'>{pay_u.get("amount","?")} EGP · {pay_u.get("submitted_at","")[:10]}</div>
            </div>""", unsafe_allow_html=True)

    col_save, col_del = st.columns(2)
    with col_save:
        if st.button("💾 Save Changes", key=f"admin_save_{selected_user}",
                     use_container_width=True, type="primary"):
            users[selected_user]["plan"]        = new_plan
            users[selected_user]["status"]      = new_status
            users[selected_user]["custom_tabs"] = custom_tabs_sel if use_custom else None
            _save_users(users)
            st.success(f"✅ {selected_user} updated!")
    with col_del:
        if selected_user != "admin" and st.button("🗑️ Delete User",
                key=f"admin_del_{selected_user}", use_container_width=True):
            del users[selected_user]
            _save_users(users)
            st.success(f"Deleted {selected_user}.")
            st.rerun()

    st.divider()
    st.subheader("📋 All Users")
    user_rows = []
    for uname, udata in users.items():
        tabs_count = len(_get_user_tabs(uname, users))
        user_rows.append({
            "Username": uname,
            "Status": {"approved":"✅","pending":"⏳","rejected":"❌"}.get(udata.get("status","approved"),"✅"),
            "Plan": udata.get("plan","Free"),
            "Price": f"{SUBSCRIPTION_PLANS.get(udata.get('plan','Free'),{}).get('price',0)} EGP/mo",
            "Email": udata.get("email",""),
            "Joined": udata.get("created",""),
            "Tabs": tabs_count,
            "Admin": "✅" if udata.get("is_admin") else ""
        })
    st.dataframe(pd.DataFrame(user_rows), use_container_width=True, hide_index=True)

# ==========================================
# GLOBAL CUSTOM CSS - Dark Sleek Theme
# ==========================================
# ==========================================
# THEME SYSTEM — CSS variables + user preference
# ==========================================
# Theme is resolved once at page load and written as CSS variables, so every
# component on every page automatically follows it. Users can switch via the
# sidebar, and their preference is persisted to their user record.

def _get_user_theme() -> str:
    """Return 'dark' or 'light' — reads from session, then user profile, then default."""
    # 1. Session state (current session)
    theme = st.session_state.get("theme")
    if theme in ("dark", "light"):
        return theme
    # 2. User profile (persisted)
    username = st.session_state.get("auth_user", "")
    if username:
        try:
            users = _load_users()
            saved = users.get(username, {}).get("theme", "")
            if saved in ("dark", "light"):
                st.session_state["theme"] = saved
                return saved
        except Exception:
            pass
    # 3. Default
    st.session_state["theme"] = "dark"
    return "dark"


# ── Palette definitions ──
# Every color used anywhere in the app MUST be defined here for themes to work
# consistently. Components reference them via CSS custom properties.
THEMES = {
    "dark": {
        # Backgrounds
        "--bg-app":       "#080c14",
        "--bg-sidebar":   "#0d1117",
        "--bg-surface":   "#0d1b2a",
        "--bg-surface-2": "#0f1e30",
        "--bg-inset":     "#0a1422",
        # Borders
        "--border":       "#1e2d3d",
        "--border-2":     "#1e3a5f",
        "--border-soft":  "#152238",
        # Text
        "--text":         "#e2e8f0",
        "--text-muted":   "#94a3b8",
        "--text-dim":     "#64748b",
        "--text-faint":   "#475569",
        "--text-heading": "#e2e8f0",
        "--text-subhead": "#93c5fd",
        # Accents (semantic)
        "--accent":       "#22c55e",   # brand green (Thrivo)
        "--accent-soft":  "#16a34a",
        "--info":         "#3b82f6",
        "--info-soft":    "#60a5fa",
        "--warn":         "#f59e0b",
        "--warn-soft":    "#fbbf24",
        "--danger":       "#ef4444",
        "--danger-soft":  "#f87171",
        # Success/Info/Warning tinted fills
        "--fill-success": "#052e16",
        "--fill-info":    "#0c1a2e",
        "--fill-warn":    "#1c1000",
        "--fill-danger":  "#1c0606",
        # Button gradient
        "--btn-grad-1":   "#1e3a5f",
        "--btn-grad-2":   "#0d2137",
        "--btn-text":     "#93c5fd",
        "--btn-border":   "#2d5a8e",
    },
    "light": {
        "--bg-app":       "#f8fafc",
        "--bg-sidebar":   "#ffffff",
        "--bg-surface":   "#ffffff",
        "--bg-surface-2": "#f1f5f9",
        "--bg-inset":     "#f8fafc",
        "--border":       "#e2e8f0",
        "--border-2":     "#cbd5e1",
        "--border-soft":  "#e5e7eb",
        "--text":         "#0f172a",
        "--text-muted":   "#475569",
        "--text-dim":     "#64748b",
        "--text-faint":   "#94a3b8",
        "--text-heading": "#0f172a",
        "--text-subhead": "#1d4ed8",
        "--accent":       "#16a34a",
        "--accent-soft":  "#22c55e",
        "--info":         "#2563eb",
        "--info-soft":    "#3b82f6",
        "--warn":         "#d97706",
        "--warn-soft":    "#f59e0b",
        "--danger":       "#dc2626",
        "--danger-soft":  "#ef4444",
        "--fill-success": "#f0fdf4",
        "--fill-info":    "#eff6ff",
        "--fill-warn":    "#fffbeb",
        "--fill-danger":  "#fef2f2",
        "--btn-grad-1":   "#eff6ff",
        "--btn-grad-2":   "#dbeafe",
        "--btn-text":     "#1d4ed8",
        "--btn-border":   "#bfdbfe",
    },
}


def _theme_css() -> str:
    """Generate the full CSS block for the active theme."""
    theme_name = _get_user_theme()
    vars_css = "\n".join(f"    {k}: {v};" for k, v in THEMES[theme_name].items())

    return f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;700&family=Sora:wght@300;400;600;800&display=swap');

    :root {{
{vars_css}
    }}

    html, body, [class*="css"] {{
        font-family: 'Inter', 'Sora', -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    /* App shell */
    .stApp {{
        background: var(--bg-app);
        color: var(--text);
    }}

    /* Sidebar */
    section[data-testid="stSidebar"] {{
        background: var(--bg-sidebar) !important;
        border-right: 1px solid var(--border);
    }}
    section[data-testid="stSidebar"] * {{ color: var(--text) !important; }}
    section[data-testid="stSidebar"] .stMarkdown p,
    section[data-testid="stSidebar"] label {{ color: var(--text-muted) !important; }}

    /* Metric cards */
    [data-testid="stMetric"] {{
        background: var(--bg-surface);
        border: 1px solid var(--border-2);
        border-radius: 14px;
        padding: 18px 22px;
        transition: all 0.2s ease;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }}
    [data-testid="stMetric"]:hover {{
        border-color: var(--info);
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
    }}
    [data-testid="stMetricLabel"] {{
        color: var(--text-dim) !important;
        font-size: 0.72rem !important;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        font-weight: 600;
    }}
    [data-testid="stMetricValue"] {{
        color: var(--text) !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 1.7rem !important;
        font-weight: 700;
    }}
    [data-testid="stMetricDelta"] {{ font-size: 0.85rem !important; }}

    /* Tabs — friendlier, rounded pills */
    .stTabs [data-baseweb="tab-list"] {{
        background: var(--bg-sidebar);
        border-bottom: 1px solid var(--border);
        gap: 6px;
        padding: 4px 4px 0 4px;
        border-radius: 10px 10px 0 0;
    }}
    .stTabs [data-baseweb="tab"] {{
        color: var(--text-dim);
        border-radius: 8px 8px 0 0;
        font-size: 0.88rem;
        font-weight: 500;
        padding: 10px 18px;
        transition: all 0.15s;
    }}
    .stTabs [data-baseweb="tab"]:hover {{
        background: var(--bg-surface-2);
        color: var(--text);
    }}
    .stTabs [aria-selected="true"] {{
        background: var(--bg-surface) !important;
        color: var(--accent) !important;
        border-bottom: 2px solid var(--accent);
        font-weight: 600;
    }}

    /* Buttons — more inviting, better touch targets */
    .stButton button {{
        background: linear-gradient(135deg, var(--btn-grad-1) 0%, var(--btn-grad-2) 100%);
        border: 1px solid var(--btn-border);
        border-radius: 10px;
        color: var(--btn-text);
        font-family: inherit;
        font-weight: 600;
        font-size: 0.88rem;
        letter-spacing: 0.01em;
        padding: 8px 18px;
        min-height: 40px;
        transition: all 0.18s ease;
    }}
    .stButton button:hover {{
        filter: brightness(1.08);
        border-color: var(--info);
        transform: translateY(-1px);
        box-shadow: 0 4px 10px rgba(0,0,0,0.1);
    }}
    .stButton button:active {{ transform: translateY(0); }}

    /* Primary buttons — accent-colored */
    .stButton [data-testid="baseButton-primary"] {{
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-soft) 100%) !important;
        color: white !important;
        border: none !important;
        font-weight: 700;
    }}

    /* Expanders */
    .streamlit-expanderHeader {{
        background: var(--bg-surface);
        border-radius: 10px;
        color: var(--text-subhead) !important;
        border: 1px solid var(--border-2);
    }}

    /* Data frames */
    [data-testid="stDataFrame"] {{
        border: 1px solid var(--border-2);
        border-radius: 10px;
        overflow: hidden;
    }}

    /* Dividers */
    hr {{ border-color: var(--border); margin: 1.2rem 0; }}

    /* Checkboxes */
    [data-testid="stCheckbox"] label {{ color: var(--text) !important; font-size: 0.9rem; }}

    /* Headings */
    h1 {{
        color: var(--text-heading) !important;
        font-weight: 800 !important;
        font-size: 2rem !important;
        letter-spacing: -0.02em;
    }}
    h2 {{ color: var(--text-heading) !important; font-weight: 700 !important; letter-spacing: -0.015em; }}
    h3 {{ color: var(--text-subhead) !important; font-weight: 600 !important; }}

    /* Inputs — friendlier, bigger */
    .stSelectbox > div > div, .stNumberInput > div > div,
    .stTextInput > div > div, .stTextArea textarea {{
        background: var(--bg-surface) !important;
        border-color: var(--border-2) !important;
        color: var(--text) !important;
        border-radius: 10px !important;
    }}
    .stTextInput > div > div:focus-within,
    .stSelectbox > div > div:focus-within {{
        border-color: var(--info) !important;
        box-shadow: 0 0 0 3px rgba(59,130,246,0.1) !important;
    }}

    /* Alerts */
    .stSuccess {{ background: var(--fill-success) !important; border-color: var(--accent) !important; color: var(--text) !important; }}
    .stInfo    {{ background: var(--fill-info)    !important; border-color: var(--info)   !important; color: var(--text) !important; }}
    .stWarning {{ background: var(--fill-warn)    !important; border-color: var(--warn)   !important; color: var(--text) !important; }}
    .stError   {{ background: var(--fill-danger)  !important; border-color: var(--danger) !important; color: var(--text) !important; }}

    /* Custom components */
    .grade-badge {{
        display: inline-block;
        font-family: 'JetBrains Mono', monospace;
        font-size: 2.5rem;
        font-weight: 700;
        padding: 8px 24px;
        border-radius: 12px;
        border: 2px solid;
        text-align: center;
    }}
    .streak-card {{
        background: linear-gradient(135deg, #c2410c, #f97316);
        border: 1px solid #ea580c;
        border-radius: 14px;
        padding: 18px 22px;
        text-align: center;
        color: white;
    }}
    .insight-card {{
        background: var(--bg-surface);
        border-left: 3px solid var(--info);
        border-radius: 0 10px 10px 0;
        padding: 14px 18px;
        margin: 8px 0;
        font-size: 0.9rem;
        color: var(--text);
    }}
    .kpi-pill {{
        display: inline-flex;
        align-items: center;
        background: var(--bg-surface);
        border: 1px solid var(--border-2);
        border-radius: 999px;
        padding: 5px 14px;
        font-size: 0.8rem;
        color: var(--text-muted);
        gap: 6px;
        margin: 3px;
    }}

    /* Scrollbar */
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg-app); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border-2); border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--info); }}

    /* Radio / selectbox items */
    [data-testid="stRadio"] label, [data-testid="stMultiSelect"] label {{ color: var(--text) !important; }}

    /* Caption */
    [data-testid="stCaptionContainer"], .stMarkdown p em {{ color: var(--text-dim) !important; }}
</style>
"""


st.markdown(_theme_css(), unsafe_allow_html=True)

# --- AUTHENTICATION ---
# Old single-password auth removed — replaced by user auth system above

# --- DATA MANAGER ---
def load_data():
    default = {
        "history": {},
        "goals": {"daily": [], "monthly": [], "yearly": []},
        "library": {"books": [], "courses": [], "skills": [], "media": []},
        "finance": {"income": [], "expenses_monthly": [], "expenses_extra": [], "assets": [], "goals": []},
        "price_history": {},
        "notes": {},
        "credit": {
            "transactions": [], "installment_plans": [],
            "limits": {"QNB": 0, "EGBank": 0},
            "balances": {"QNB": 0, "EGBank": 0}
        },
        "gym": {"sessions": [], "workouts": [], "habits": []},
        "stocks": {"watchlist": [], "price_history": {}},
        "business": {
            "coffee_shop": {"income": [], "expenses": [], "inventory": [], "metrics": []},
            "fashion_store": {"income": [], "expenses": [], "inventory": [], "metrics": []}
        },
        "agile": {"tasks": [], "sprints": []},
        "journal": {},
        "custom_streaks": [],
        "user_prefs": {"weekends": ["Friday","Saturday"], "holidays": []}
    }
    if not os.path.exists(DATA_FILE):
        return default
    with open(DATA_FILE, "r") as f:
        d = json.load(f)
    # Backward-compat patches
    for k, v in default.items():
        if k not in d:
            d[k] = v
    if "gym" not in d: d["gym"] = {"sessions": [], "workouts": [], "habits": []}
    if "stocks" not in d: d["stocks"] = {"watchlist": [], "price_history": {}}
    if "business" not in d: d["business"] = default["business"]
    if "agile" not in d: d["agile"] = {"tasks": [], "sprints": []}
    if "journal" not in d: d["journal"] = {}
    if "custom_streaks" not in d: d["custom_streaks"] = []
    if "user_prefs" not in d: d["user_prefs"] = {"weekends": ["Friday","Saturday"], "holidays": []}
    # Credit upgrade: add limits/balances if missing
    if "limits" not in d["credit"]: d["credit"]["limits"] = {"QNB": 0, "EGBank": 0}
    if "balances" not in d["credit"]: d["credit"]["balances"] = {"QNB": 0, "EGBank": 0}
    return d

def save_data(d):
    """Legacy alias — pre-auth code path. After auth, the per-user save_data
    (defined later) takes over via Python's late-binding."""
    pass  # no-op: pre-auth code never persists data

# Note: no pre-auth `data = load_data()` call — auth runs first, then per-user data loads below.

# ── Authentication Gate ──
if not render_auth():
    st.stop()

# ── Load user context ──
_current_username = st.session_state.get("auth_user", "admin")
# Re-verify status on every page load (catches post-login status changes)
_all_users_check = _load_users()
_user_status = _all_users_check.get(_current_username, {}).get("status", "approved")
if _user_status == "pending":
    st.session_state.pop("auth_user", None)
    st.error("⏳ Your account is still pending approval. Check back soon.")
    st.stop()
elif _user_status == "rejected":
    st.session_state.pop("auth_user", None)
    st.error("❌ Account not approved. Contact: " + ADMIN_EMAIL)
    st.stop()
_is_admin = st.session_state.get("auth_is_admin", False)
_user_tabs = st.session_state.get("auth_tabs", [t[2] for t in ALL_TABS])
_users_db = _load_users()

# Per-user data: each user gets their own JSON file
USER_DATA_FILE = f"data_{_current_username}.json"

def _get_default_user_data():
    """Return a completely fresh empty data structure — no cross-user contamination."""
    return {
        "history": {},
        "goals": {"daily": [], "monthly": [], "yearly": []},
        "library": {"books": [], "courses": [], "skills": [], "media": []},
        "finance": {"income": [], "expenses_monthly": [], "expenses_extra": [], "assets": [], "goals": []},
        "price_history": {},
        "notes": {},
        "credit": {
            "transactions": [], "installment_plans": [],
            "limits": {"QNB": 0, "EGBank": 0},        # legacy — kept for backward compat
            "balances": {"QNB": 0, "EGBank": 0},      # legacy — kept for backward compat
            "accounts": []                             # v10.2+ — flexible: cards & installment programs
        },
        "gym": {"sessions": [], "workouts": [], "habits": []},
        "stocks": {"watchlist": [], "price_history": {}},
        "business": {
            "coffee_shop":  {"income": [], "expenses": [], "inventory": [], "metrics": []},
            "fashion_store": {"income": [], "expenses": [], "inventory": [], "metrics": []}
        },
        "agile": {"tasks": [], "sprints": []},
        "journal": {},
        "custom_streaks": [],
        "user_prefs": {"weekends": ["Friday", "Saturday"], "holidays": []},
        "meals": {"log": [], "favorites": [], "custom_meals": []},
        "language": {"sessions": [], "vocab_learned": [], "quiz_history": [], "settings": {}},
        # ── NEW (v9) feature data ──
        "habits":   {"list": [], "log": {}},                     # habits: [{id,name,icon,target_days,created}], log: {YYYY-MM-DD: [habit_id,...]}
        "pomodoro": {"sessions": [], "settings": {"focus_min": 25, "break_min": 5, "long_break_min": 15, "long_every": 4}},
        "okr":      {"objectives": [], "checkins": []},          # quarterly OKRs + weekly check-ins
        "buytime":  {"watchlist": [], "savings_log": []},        # v10.1: planned purchases + savings tracker
    }

def load_user_data():
    """Load this user's personal data via the active backend, or create empty defaults."""
    defaults = _get_default_user_data()
    d = db.load_user_data(_current_username)
    if d is None:
        # Brand new user — initialize their data record
        db.save_user_data(_current_username, defaults)
        return defaults
    # Forward-compat: add any new keys introduced in newer versions
    for k, v in defaults.items():
        if k not in d:
            d[k] = v
    # Patch credit sub-keys (legacy)
    if "limits"   not in d["credit"]: d["credit"]["limits"]   = {"QNB": 0, "EGBank": 0}
    if "balances" not in d["credit"]: d["credit"]["balances"] = {"QNB": 0, "EGBank": 0}
    if "accounts" not in d["credit"]: d["credit"]["accounts"] = []   # v10.2+ — flexible accounts
    return d

def save_data(d):
    """Persist this user's data via the active backend."""
    db.save_user_data(_current_username, d)

data = load_user_data()


# ── Prayer times helper (module scope — available to all pages) ──
# Must be defined BEFORE it's used in the Daily Tracker page.
# Previously this was nested inside the weekend/holiday branch, causing
# NameError on regular weekdays.
@st.cache_data(ttl=3600 * 12)
def get_prayer_times(city: str = "Cairo", country: str = "Egypt", method: int = 5) -> dict:
    """
    Fetch prayer times from the Aladhan API.
    Falls back to sensible Cairo defaults if the request fails.
    Cached for 12 hours — prayer times don't shift much day-to-day.
    """
    try:
        r = requests.get(
            f"http://api.aladhan.com/v1/timingsByCity"
            f"?city={city}&country={country}&method={method}",
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()["data"]["timings"]
    except Exception:
        pass
    return {"Fajr": "05:00", "Dhuhr": "12:00", "Asr": "15:00",
            "Maghrib": "18:00", "Isha": "19:30"}


# --- SIDEBAR NAVIGATION ---
with st.sidebar:
    plan_color = SUBSCRIPTION_PLANS.get(st.session_state.get("auth_plan","Free"),{}).get("color","#64748b")
    plan_name  = st.session_state.get("auth_plan","Free")
    st.markdown(
        f"<div style='padding:14px 0 4px 0;display:flex;align-items:center;gap:10px;'>"
        f"<span style='font-size:1.5rem;'>{AppConfig.ICON}</span>"
        f"<span style='font-family:Sora,sans-serif;font-size:1.15rem;color:#22c55e;"
        f"font-weight:800;letter-spacing:-0.01em;'>{AppConfig.NAME}</span>"
        f"<span style='font-size:0.62rem;color:#334155;margin-left:auto;"
        f"font-family:JetBrains Mono,monospace;'>{AppConfig.VERSION}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.73rem;margin-bottom:6px;'>"
        f"👤 {_current_username} · <span style='color:{plan_color};'>{plan_name}</span></div>",
        unsafe_allow_html=True)

    st.subheader("📅 Date")
    selected_date = st.date_input("", datetime.date.today(), label_visibility="collapsed")
    current_day_str = selected_date.strftime("%Y-%m-%d")
    today = datetime.date.today()
    st.divider()

    if _is_admin:
        if st.button("⚙️ Admin Panel", use_container_width=True, key="nav_adminpanel"):
            st.session_state["page"] = "__admin__"
            st.rerun()

    allowed_pages = [t for t in ALL_TABS if t[2] in _user_tabs]

    # ── Search box — instantly filters across all groups ──
    nav_query = st.text_input(
        "Find tab",
        key="nav_search",
        placeholder="🔎 Search tabs...",
        label_visibility="collapsed",
    ).strip().lower()

    def _matches_search(label: str, key: str) -> bool:
        if not nav_query:
            return True
        return (nav_query in label.lower()) or (nav_query in key.lower())

    # Build a key→(icon, label) lookup once
    tab_lookup = {key: (icon, label) for icon, label, key in allowed_pages}
    allowed_keys = set(tab_lookup.keys())
    grouped_keys = set()  # track tabs assigned to a group so "Other" gets the rest

    # Active page — for "What I clicked last" indicator
    active_page = st.session_state.get("page")

    # ── Render each group (only if it has at least one allowed+matching tab) ──
    for group_title, group_keys in TAB_GROUPS:
        in_group = [k for k in group_keys if k in allowed_keys]
        grouped_keys.update(group_keys)
        # Filter by search
        visible = [k for k in in_group if _matches_search(tab_lookup[k][1], k)]
        if not visible:
            continue

        # Group header (small, muted, with count)
        st.markdown(
            f"<div style='margin:14px 0 4px;color:var(--text-faint);"
            f"font-size:0.68rem;text-transform:uppercase;letter-spacing:0.12em;"
            f"font-weight:600;display:flex;justify-content:space-between;'>"
            f"<span>{group_title}</span>"
            f"<span style='color:var(--text-faint);'>{len(visible)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        for k in visible:
            icon, label = tab_lookup[k]
            is_active = active_page == k
            btn_label = ("▶ " if is_active else "") + icon + " " + label
            if st.button(btn_label, use_container_width=True, key="nav_" + k,
                         type="primary" if is_active else "secondary"):
                st.session_state["page"] = k
                st.rerun()

    # ── Catch-all: any tabs not in any group ──
    other = [k for k in allowed_keys if k not in grouped_keys]
    other_visible = [k for k in other if _matches_search(tab_lookup[k][1], k)]
    if other_visible:
        st.markdown(
            "<div style='margin:14px 0 4px;color:var(--text-faint);"
            "font-size:0.68rem;text-transform:uppercase;letter-spacing:0.12em;"
            "font-weight:600;'>📦 Other</div>",
            unsafe_allow_html=True,
        )
        for k in other_visible:
            icon, label = tab_lookup[k]
            is_active = active_page == k
            btn_label = ("▶ " if is_active else "") + icon + " " + label
            if st.button(btn_label, use_container_width=True, key="nav_" + k,
                         type="primary" if is_active else "secondary"):
                st.session_state["page"] = k
                st.rerun()

    # If search filtered everything out, show a hint
    if nav_query:
        any_visible = any(
            _matches_search(tab_lookup[k][1], k) for k in allowed_keys
        )
        if not any_visible:
            st.markdown(
                f"<div style='color:var(--text-faint);font-size:0.78rem;"
                f"text-align:center;padding:14px 0;'>"
                f"No tabs match '{nav_query}'.</div>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("<p style='font-size:0.7rem;color:#475569;letter-spacing:0.1em;text-transform:uppercase;'>This Week</p>", unsafe_allow_html=True)
    fixed_tasks_sidebar = ["t1_ds", "t2_de", "t3_gym", "t4_life"]
    week_cols = st.columns(7)
    for i, col in enumerate(week_cols):
        day_offset = today - datetime.timedelta(days=6 - i)
        d_str = day_offset.strftime("%Y-%m-%d")
        day_data_s = data["history"].get(d_str, {})
        done_count = sum(1 for k in fixed_tasks_sidebar if day_data_s.get(k, False))
        wcolor = "#22c55e" if done_count == 4 else "#eab308" if done_count >= 2 else "#1e3a5f" if done_count > 0 else "#0d1117"
        day_letter = day_offset.strftime("%a")[0]
        col.markdown(
            f"<div style='background:{wcolor};border-radius:4px;width:100%;aspect-ratio:1;'></div>"
            f"<div style='font-size:0.6rem;color:#475569;text-align:center;'>{day_letter}</div>",
            unsafe_allow_html=True)

    st.divider()
    with st.expander("🔑 AI Settings (Gemini)"):
        current_gemini = get_gemini_key()
        new_gemini_key = st.text_input("Gemini API Key", value=current_gemini, type="password",
                                       placeholder="AIza...", help="Free key at aistudio.google.com/app/apikey")

        # ── Dynamic model list fetched live from the API ──
        @st.cache_data(ttl=3600)
        def fetch_gemini_models(api_key: str):
            """Fetch available generateContent-capable models from the Gemini API."""
            if not api_key:
                return []
            try:
                r = requests.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                    timeout=8
                )
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    # Keep only models that support generateContent (text gen)
                    valid = []
                    for m in models:
                        name = m.get("name", "").replace("models/", "")
                        methods = m.get("supportedGenerationMethods", [])
                        display = m.get("displayName", name)
                        # Filter: text-gen models only, skip embedding/vision/TTS/image-only
                        if "generateContent" in methods and name:
                            # Prioritise readable names
                            valid.append((name, display))
                    # Sort: put stable models first, previews after
                    valid.sort(key=lambda x: (1 if "preview" in x[0] or "exp" in x[0] else 0, x[0]))
                    return valid
            except Exception:
                pass
            return []

        key_for_fetch = new_gemini_key or current_gemini
        live_models = fetch_gemini_models(key_for_fetch) if key_for_fetch else []

        # Fallback hardcoded list (confirmed working as of April 2025)
        # Uses stable names without date suffixes — Google keeps these updated automatically
        FALLBACK_MODELS = [
            ("gemini-2.0-flash",      "⚡ Gemini 2.0 Flash — Fast, free tier"),
            ("gemini-2.5-flash",      "🚀 Gemini 2.5 Flash — Best reasoning, free tier"),
            ("gemini-2.5-flash-lite", "🪶 Gemini 2.5 Flash-Lite — Fastest & cheapest"),
            ("gemini-2.5-pro",        "👑 Gemini 2.5 Pro — Most capable (paid)"),
        ]

        if live_models:
            model_options = [m[0] for m in live_models]
            model_labels  = {m[0]: m[1] for m in live_models}
            st.caption(f"✅ {len(live_models)} models fetched live from your API key.")
        else:
            model_options = [m[0] for m in FALLBACK_MODELS]
            model_labels  = {m[0]: m[1] for m in FALLBACK_MODELS}
            if key_for_fetch:
                st.caption("⚠️ Could not fetch live model list — using known stable models.")
            else:
                st.caption("Add your API key above to see available models.")

        current_model = st.session_state.get("gemini_model", "gemini-2.5-flash")
        # Ensure current_model is in the list (may have been saved as an old name)
        if current_model not in model_options and model_options:
            current_model = model_options[0]

        selected_model = st.selectbox(
            "Model",
            options=model_options,
            format_func=lambda m: model_labels.get(m, m),
            index=model_options.index(current_model) if current_model in model_options else 0,
            key="gemini_model_select",
            help="Free tier: Flash models. Paid tier: Pro models (requires billing on Google Cloud)."
        )

        if st.button("💾 Save Settings", key="save_gemini_key"):
            st.session_state["gemini_api_key"] = new_gemini_key
            st.session_state["gemini_model"] = selected_model
            if _current_username:
                _users_db[_current_username]["gemini_api_key"] = new_gemini_key
                _users_db[_current_username]["gemini_model"] = selected_model
                _save_users(_users_db)
            # Clear model fetch cache so it re-fetches with new key
            fetch_gemini_models.clear()
            st.success("✅ Saved permanently!")
            st.rerun()

        status_color = "#22c55e" if current_gemini else "#ef4444"
        status_text = f"✅ Key active ({current_gemini[:8]}...)" if current_gemini else "⚠️ No key — AI disabled"
        st.markdown(f"<span style='color:{status_color};font-size:0.72rem;'>{status_text}</span>", unsafe_allow_html=True)
        if current_gemini:
            active_label = model_labels.get(selected_model, selected_model)
            st.markdown(f"<span style='color:#475569;font-size:0.7rem;'>Model: {active_label}</span>", unsafe_allow_html=True)
        st.markdown("<span style='color:#334155;font-size:0.68rem;'>Get key: aistudio.google.com/app/apikey</span>", unsafe_allow_html=True)

    st.divider()

    # ── Theme switcher ──
    current_theme = _get_user_theme()
    theme_col_l, theme_col_d = st.columns(2)
    with theme_col_l:
        if st.button(
            ("✓ " if current_theme == "light" else "") + "☀️ Light",
            key="theme_light_btn",
            use_container_width=True,
            type="primary" if current_theme == "light" else "secondary",
        ):
            st.session_state["theme"] = "light"
            if _current_username and _current_username in _users_db:
                _users_db[_current_username]["theme"] = "light"
                _save_users(_users_db)
            st.rerun()
    with theme_col_d:
        if st.button(
            ("✓ " if current_theme == "dark" else "") + "🌙 Dark",
            key="theme_dark_btn",
            use_container_width=True,
            type="primary" if current_theme == "dark" else "secondary",
        ):
            st.session_state["theme"] = "dark"
            if _current_username and _current_username in _users_db:
                _users_db[_current_username]["theme"] = "dark"
                _save_users(_users_db)
            st.rerun()

    if st.button("🔒 Logout", type="primary", use_container_width=True):
        for k in ["auth_user","auth_plan","auth_is_admin","auth_tabs","page"]:
            st.session_state.pop(k, None)
        st.rerun()



# Ensure Data Structure — only creates if date not yet in history (never overwrites)
if current_day_str not in data["history"]:
    data["history"][current_day_str] = {
        "t1_ds": False, "t2_de": False, "t3_gym": False, "t4_life": False,
        "daily_goals_completed": [],
        "prayers": {"Fajr": False, "Dhuhr": False, "Asr": False, "Maghrib": False, "Isha": False},
        "mood": 3,
        "energy": 3,
        "note": ""
    }
    save_data(data)  # persist the new day immediately

# Patch missing sub-keys on existing days (backward compat)
if "prayers" not in data["history"][current_day_str]:
    data["history"][current_day_str]["prayers"] = {"Fajr": False, "Dhuhr": False, "Asr": False, "Maghrib": False, "Isha": False}
if "mood" not in data["history"][current_day_str]:
    data["history"][current_day_str]["mood"] = 3
if "energy" not in data["history"][current_day_str]:
    data["history"][current_day_str]["energy"] = 3

# ── Date-change detection: clear widget state when user navigates to a different date ──
# This ensures checkboxes/sliders load from disk data not stale session_state
if st.session_state.get("_last_viewed_date") != current_day_str:
    # Purge all date-specific widget keys from session_state
    keys_to_clear = [k for k in st.session_state if k.startswith("dt_")]
    for k in keys_to_clear:
        del st.session_state[k]
    st.session_state["_last_viewed_date"] = current_day_str

# ==========================================
# PAGE 1: DAILY TRACKER
# ==========================================
# ==========================================
# ADMIN PANEL ROUTE
# ==========================================
if st.session_state.get("page") == "__admin__":
    if _is_admin:
        render_admin_panel(_users_db)
    else:
        st.error("Access denied.")
    st.stop()

if st.session_state['page'] == 'Daily Tracker':
    # ── Weekend / Holiday Detection ──
    user_prefs = data.get("user_prefs", {})
    user_weekends = user_prefs.get("weekends", ["Friday", "Saturday"])
    user_holidays = user_prefs.get("holidays", [])
    day_name = selected_date.strftime("%A")
    is_weekend = day_name in user_weekends
    is_holiday = selected_date.strftime("%Y-%m-%d") in user_holidays

    if not is_weekend and not is_holiday:
        # Allow manual marking of today as a holiday
        if selected_date == datetime.date.today():
            mark_col, _ = st.columns([2, 5])
            with mark_col:
                if st.button("🏖️ Mark Today as Holiday", key="mark_holiday"):
                    if "user_prefs" not in data:
                        data["user_prefs"] = {}
                    if "holidays" not in data["user_prefs"]:
                        data["user_prefs"]["holidays"] = []
                    data["user_prefs"]["holidays"].append(current_day_str)
                    save_data(data)
                    st.rerun()


    # ── Weekend / Holiday Mode ──
    if is_weekend or is_holiday:
        mode_label = "🏖️ Holiday" if is_holiday else "🌴 Weekend"
        mode_color = "#f97316" if is_holiday else "#a78bfa"
        st.markdown(
            f"<div style='background:#1c0e00; border:2px solid {mode_color}; border-radius:14px; "
            f"padding:16px 22px; margin-bottom:16px; display:flex; align-items:center; gap:12px;'>"
            f"<span style='font-size:2rem;'>{mode_label.split()[0]}</span>"
            f"<div><div style='color:{mode_color}; font-weight:700; font-size:1.1rem;'>{mode_label} — {selected_date.strftime("%A, %B %d")}</div>"
            f"<div style='color:#94a3b8; font-size:0.82rem;'>Regular protocol paused. Set your own day plan below.</div></div>"
            f"</div>", unsafe_allow_html=True)

        # --- PRAYER TIMES ---
        # (get_prayer_times is defined at module scope — available everywhere)

        # Prayer times still shown on weekends
        prayer_times = get_prayer_times()
        day_data = data["history"][current_day_str]
        prayers_done = sum(1 for v in day_data.get("prayers",{}).values() if v)

        wk_c1, wk_c2 = st.columns([2, 1])
        with wk_c1:
            st.markdown("### 📝 Today's Free Protocol")
            free_protocol = day_data.get("free_protocol", "")
            new_fp = st.text_area("What's your plan for today?", value=free_protocol, height=120,
                key="free_protocol_input",
                placeholder="e.g. Morning walk, family brunch, movie night, reading 30 pages...")

            gemini_key_wk = get_gemini_key()
            if gemini_key_wk:
                if st.button("✨ Get AI Day Suggestions", key="ai_weekend_plan"):
                    wk_prompt = f"""I have a {mode_label} today ({selected_date.strftime('%A, %B %d')}).
Suggest a beautiful, balanced day protocol for me. Include:
- Morning routine (1 item)
- Social/family activity (1-2 items)
- Personal development (1 item)
- Rest/fun (1 item)
- Evening wind-down (1 item)
Be specific, inspiring, and concise. Format as a simple bulleted list."""
                    with st.spinner("✨ Getting suggestions..."):
                        suggestions = call_gemini(wk_prompt, max_tokens=1500)
                    st.markdown(f"""
                    <div style='background:#0d1b2a; border:1px solid {mode_color}; border-radius:10px; padding:14px;'>
                        <div style='color:{mode_color}; font-size:0.8rem; margin-bottom:8px;'>✨ AI Day Suggestions</div>
                        <div style='color:#e2e8f0; font-size:0.87rem; white-space:pre-wrap;'>{suggestions}</div>
                    </div>""", unsafe_allow_html=True)

            if st.button("💾 Save Day Plan", key="save_free_protocol"):
                data["history"][current_day_str]["free_protocol"] = new_fp
                save_data(data)
                st.success("✅ Saved!")

        with wk_c2:
            st.markdown("### 🕌 Prayers")
            prayer_icons = {"Fajr":"🌅","Dhuhr":"☀️","Asr":"🌤️","Maghrib":"🌅","Isha":"🌙"}
            for p in ["Fajr","Dhuhr","Asr","Maghrib","Isha"]:
                t = prayer_times.get(p,"")
                is_done = day_data.get("prayers",{}).get(p, False)
                pc_col, pi_col = st.columns([1,5])
                with pc_col:
                    nv = st.checkbox("", value=is_done, key=f"wk_p_{p}_{current_day_str}", label_visibility="collapsed")
                    if nv != is_done:
                        data["history"][current_day_str]["prayers"][p] = nv
                        save_data(data); st.rerun()
                with pi_col:
                    bc = "#052e16" if is_done else "#0d1b2a"
                    bc2 = "#16a34a" if is_done else "#1e3a5f"
                    st.markdown(
                        f"<div style='background:{bc};border:1px solid {bc2};border-radius:8px;"
                        f"padding:6px 12px;margin-bottom:4px;display:flex;justify-content:space-between;'>"
                        f"<span style='color:#e2e8f0;font-size:0.88rem;'>{prayer_icons[p]} {p}</span>"
                        f"<span style='color:#475569;font-family:JetBrains Mono,monospace;font-size:0.78rem;'>{t}</span></div>",
                        unsafe_allow_html=True)
            st.metric("Prayers", f"{prayers_done}/5")

            if is_holiday:
                if st.button("❌ Remove Holiday Mark", key="remove_holiday"):
                    data["user_prefs"]["holidays"] = [h for h in data["user_prefs"].get("holidays",[]) if h != current_day_str]
                    save_data(data); st.rerun()

    else:
        # Header
        st.markdown(f"""
        <div style='display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;'>
            <div>
                <h1 style='margin:0; padding:0;'>📊 {selected_date.strftime('%A')}</h1>
                <p style='color:#475569; margin:0; font-size:0.9rem; font-family: JetBrains Mono, monospace;'>{selected_date.strftime('%B %d, %Y')}</p>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if selected_date > datetime.date.today():
            st.warning("⚠️ You are planning for the future.")
        elif selected_date < datetime.date.today():
            st.info("🕒 Editing past date.")

    prayer_times = get_prayer_times()
    day_data = data["history"][current_day_str]

    # --- CALCULATE METRICS ---
    fixed_tasks = ["t1_ds", "t2_de", "t3_gym", "t4_life"]
    completed_fixed = sum(1 for k in fixed_tasks if day_data.get(k, False))

    # FIXED: Only show daily extras for the SELECTED date
    daily_goals_for_day = [g for g in data["goals"]["daily"] if g.get("date") == current_day_str]
    completed_custom_ids = set(day_data.get("daily_goals_completed", []))
    valid_completed_custom = [gid for gid in completed_custom_ids if any(g['id'] == gid for g in daily_goals_for_day)]
    completed_custom = len(valid_completed_custom)

    total_tasks = 4 + len(daily_goals_for_day)
    progress = (completed_fixed + completed_custom) / total_tasks if total_tasks > 0 else 0

    # Grade
    if progress >= 1.0:
        grade, grade_color, grade_bg = "S+", "#22c55e", "#052e16"
    elif progress >= 0.8:
        grade, grade_color, grade_bg = "A", "#60a5fa", "#0c1a2e"
    elif progress >= 0.6:
        grade, grade_color, grade_bg = "B", "#facc15", "#1c1a00"
    elif progress >= 0.4:
        grade, grade_color, grade_bg = "C", "#fb923c", "#1c0e00"
    else:
        grade, grade_color, grade_bg = "F", "#ef4444", "#1c0000"

    prayers_done = sum(1 for v in day_data["prayers"].values() if v)

    # Streak
    streak = 0
    today_iso = datetime.date.today().strftime("%Y-%m-%d")
    sorted_dates = sorted(data["history"].keys(), reverse=True)
    for d_key in sorted_dates:
        if d_key > today_iso:
            continue
        d_done = sum(1 for k in fixed_tasks if data["history"][d_key].get(k, False))
        if d_done > 0:
            streak += 1
        else:
            if d_key != today_iso:
                break

    # ── METRICS ROW ──
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(f"""
        <div style='background:{grade_bg}; border:1px solid {grade_color}; border-radius:12px; padding:16px 20px; text-align:center;'>
            <div style='color:#64748b; font-size:0.72rem; letter-spacing:0.1em; text-transform:uppercase; margin-bottom:4px;'>DAY GRADE</div>
            <div style='font-family:JetBrains Mono,monospace; font-size:2.2rem; font-weight:700; color:{grade_color};'>{grade}</div>
        </div>
        """, unsafe_allow_html=True)
    c2.metric("Consistency", f"{int(progress * 100)}%")
    c3.metric("Prayers", f"{prayers_done}/5")
    with c4:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg,#431407,#7c2d12); border:1px solid #ea580c; border-radius:12px; padding:16px 20px; text-align:center;'>
            <div style='color:#fed7aa; font-size:0.72rem; letter-spacing:0.1em; text-transform:uppercase; margin-bottom:4px;'>STREAK</div>
            <div style='font-family:JetBrains Mono,monospace; font-size:2.2rem; font-weight:700; color:#fb923c;'>{streak} 🔥</div>
        </div>
        """, unsafe_allow_html=True)
    c5.metric("Tasks Done", f"{completed_fixed + completed_custom}/{total_tasks}")

    st.markdown("<div style='margin: 10px 0 2px 0;'></div>", unsafe_allow_html=True)

    # ── ANIMATED PROGRESS BAR ──
    bar_segments = ""
    for i, tk in enumerate(fixed_tasks):
        done = day_data.get(tk, False)
        c = grade_color if done else "#1e293b"
        bar_segments += f"<div style='flex:1; height:8px; background:{c}; border-radius:2px; transition:background 0.4s;'></div>"

    # Custom task indicators
    for g in daily_goals_for_day:
        done = g['id'] in completed_custom_ids
        c = "#a78bfa" if done else "#1e293b"
        bar_segments += f"<div style='flex:1; height:8px; background:{c}; border-radius:2px; max-width:30px; transition:background 0.4s;'></div>"

    st.markdown(f"<div style='display:flex; gap:4px; margin-bottom:20px;'>{bar_segments}</div>", unsafe_allow_html=True)

    # ── MAIN CONTENT COLUMNS ──
    col_left, col_right = st.columns([3, 2])

    with col_left:
        # PROTOCOL SECTIONS
        st.markdown("""<h3 style='margin-bottom:12px;'>⚡ Today's Protocol</h3>""", unsafe_allow_html=True)

        tab_work, tab_evening, tab_extra = st.tabs(["☀️ Work Block", "🌙 Evening Block", "➕ Extras"])

        with tab_work:
            st.markdown("<div style='padding: 8px 0;'></div>", unsafe_allow_html=True)
            chk1 = st.checkbox("🧠 Deep Work (10:00 AM)", value=day_data["t1_ds"], key=f"dt_t1_{current_day_str}")
            if chk1 != day_data["t1_ds"]:
                day_data["t1_ds"] = chk1
                save_data(data)
                st.rerun()

            chk2 = st.checkbox("🇩🇪 German Study (01:00 PM)", value=day_data["t2_de"], key=f"dt_t2_{current_day_str}")
            if chk2 != day_data["t2_de"]:
                day_data["t2_de"] = chk2
                save_data(data)
                st.rerun()

            st.markdown("---")
            # Mood + Energy sliders
            mood_val = day_data.get("mood", 3)
            energy_val = day_data.get("energy", 3)
            mood_emojis = ["😞", "😕", "😐", "😊", "🔥"]
            energy_emojis = ["💀", "😴", "⚡", "🚀", "🌟"]

            m_col, e_col = st.columns(2)
            with m_col:
                new_mood = st.select_slider("Mood", options=[1, 2, 3, 4, 5], value=mood_val,
                    format_func=lambda x: mood_emojis[x - 1], key=f"dt_mood_{current_day_str}")
                if new_mood != mood_val:
                    day_data["mood"] = new_mood
                    save_data(data)
            with e_col:
                new_energy = st.select_slider("Energy", options=[1, 2, 3, 4, 5], value=energy_val,
                    format_func=lambda x: energy_emojis[x - 1], key=f"dt_energy_{current_day_str}")
                if new_energy != energy_val:
                    day_data["energy"] = new_energy
                    save_data(data)

        with tab_evening:
            st.markdown("<div style='padding: 8px 0;'></div>", unsafe_allow_html=True)
            chk3 = st.checkbox("🏋️‍♂️ Gym (05:30 PM)", value=day_data["t3_gym"], key=f"dt_t3_{current_day_str}")
            if chk3 != day_data["t3_gym"]:
                day_data["t3_gym"] = chk3
                save_data(data)
                st.rerun()

            chk4 = st.checkbox("❤️ Family Time (07:00 PM)", value=day_data["t4_life"], key=f"dt_t4_{current_day_str}")
            if chk4 != day_data["t4_life"]:
                day_data["t4_life"] = chk4
                save_data(data)
                st.rerun()

            # Quick daily note
            st.markdown("---")
            st.markdown("<p style='color:#64748b; font-size:0.8rem;'>Quick Note</p>", unsafe_allow_html=True)
            current_note = day_data.get("note", "")
            new_note = st.text_area("", value=current_note, key=f"dt_note_{current_day_str}", height=80,
                                    placeholder="Anything notable about today...", label_visibility="collapsed")
            if new_note != current_note:
                day_data["note"] = new_note
                save_data(data)

        with tab_extra:
            st.markdown(f"<p style='color:#a78bfa; font-size:0.82rem; margin-bottom:8px;'>Extra tasks for <b>{selected_date.strftime('%b %d')}</b> only:</p>", unsafe_allow_html=True)

            # ── FIXED DAILY EXTRAS LOGIC ──
            # Show tasks specific to this date only
            current_daily_goals = [g for g in data["goals"]["daily"] if g.get("date") == current_day_str]

            editor_data = []
            for g in current_daily_goals:
                editor_data.append({
                    "✅ Done": g["id"] in day_data.get("daily_goals_completed", []),
                    "Task": g["text"],
                    "_id": g["id"]
                })

            df_editor = pd.DataFrame(editor_data) if editor_data else pd.DataFrame(columns=["✅ Done", "Task", "_id"])

            edited_df = st.data_editor(
                df_editor,
                num_rows="dynamic",
                column_config={
                    "✅ Done": st.column_config.CheckboxColumn("Done", width="small"),
                    "Task": st.column_config.TextColumn("Task", width="large"),
                    "_id": None  # Hidden
                },
                key="daily_extras_editor",
                use_container_width=True
            )

            if st.button("💾 Save Extra Tasks", key="save_extras"):
                new_completed_ids = []
                new_daily_goals_list = []

                # Keep goals from OTHER days intact
                other_days_goals = [g for g in data["goals"]["daily"] if g.get("date") != current_day_str]

                for index, row in edited_df.iterrows():
                    row_id = row.get("_id")
                    if pd.isna(row_id) if isinstance(row_id, float) else not row_id:
                        row_id = str(int(time.time() * 1000)) + str(index)

                    task_text = row.get("Task", "")
                    if task_text and str(task_text).strip():
                        new_daily_goals_list.append({
                            "id": str(row_id),
                            "text": str(task_text).strip(),
                            "status": "active",
                            "date": current_day_str
                        })
                        if row.get("✅ Done", False):
                            new_completed_ids.append(str(row_id))

                data["goals"]["daily"] = other_days_goals + new_daily_goals_list
                data["history"][current_day_str]["daily_goals_completed"] = new_completed_ids
                save_data(data)
                st.success("✅ Saved!")
                st.rerun()

    with col_right:
        # PRAYERS PANEL
        st.markdown("""<h3 style='margin-bottom:12px;'>🕌 Prayers</h3>""", unsafe_allow_html=True)

        prayer_icons = {"Fajr": "🌅", "Dhuhr": "☀️", "Asr": "🌤️", "Maghrib": "🌅", "Isha": "🌙"}
        for p in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            t = prayer_times.get(p, "")
            is_done = day_data["prayers"].get(p, False)
            bg = "#052e16" if is_done else "#0d1b2a"
            border = "#16a34a" if is_done else "#1e3a5f"

            col_check, col_info = st.columns([1, 5])
            with col_check:
                new_val = st.checkbox("", value=is_done, key=f"dt_p_{p}_{current_day_str}", label_visibility="collapsed")
                if new_val != is_done:
                    day_data["prayers"][p] = new_val
                    save_data(data)
                    st.rerun()
            with col_info:
                st.markdown(f"""
                <div style='background:{bg}; border:1px solid {border}; border-radius:8px; padding:8px 12px; margin-bottom:4px; display:flex; justify-content:space-between; align-items:center;'>
                    <span style='color:#e2e8f0; font-size:0.9rem;'>{prayer_icons[p]} {p}</span>
                    <span style='color:#475569; font-family:JetBrains Mono,monospace; font-size:0.8rem;'>{t}</span>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── QUICK INSIGHTS PANEL (NEW) ──
        st.markdown("""<h3 style='margin-bottom:8px;'>💡 Insights</h3>""", unsafe_allow_html=True)

        # Calculate 7-day stats
        last_7 = []
        for i in range(7):
            d_key = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            d_hist = data["history"].get(d_key, {})
            tasks_done = sum(1 for k in fixed_tasks if d_hist.get(k, False))
            prayers = sum(1 for v in d_hist.get("prayers", {}).values() if v)
            last_7.append({"tasks": tasks_done, "prayers": prayers})

        avg_tasks = sum(d["tasks"] for d in last_7) / 7
        avg_prayers = sum(d["prayers"] for d in last_7) / 7
        best_day = max(last_7, key=lambda x: x["tasks"])

        def insight_card(text):
            st.markdown(f"<div class='insight-card'>{text}</div>", unsafe_allow_html=True)

        if avg_tasks >= 3:
            insight_card(f"🔥 Averaging <b>{avg_tasks:.1f}/4</b> tasks this week — strong consistency!")
        elif avg_tasks >= 2:
            insight_card(f"📈 Averaging <b>{avg_tasks:.1f}/4</b> tasks — room to push harder.")
        else:
            insight_card(f"⚠️ Only <b>{avg_tasks:.1f}/4</b> avg tasks this week — time to refocus.")

        if avg_prayers >= 4:
            insight_card(f"🕌 Prayer avg: <b>{avg_prayers:.1f}/5</b> — excellent spiritual consistency.")
        else:
            insight_card(f"🕌 Prayer avg: <b>{avg_prayers:.1f}/5</b> — keep building the habit.")

        if streak >= 7:
            insight_card(f"🌟 <b>{streak}-day streak!</b> You're in a real momentum zone.")
        elif streak >= 3:
            insight_card(f"🔥 <b>{streak}-day streak</b> — don't break it now!")

    # ── ANALYTICS SECTION ──
    st.divider()
    st.markdown("### 📈 Analytics")

    chart_tab1, chart_tab2, chart_tab3 = st.tabs(["🔥 Consistency", "🕌 Prayers", "😊 Mood & Energy"])

    with chart_tab1:
        heatmap_data = []
        for date_key, values in data["history"].items():
            done = sum(1 for k in fixed_tasks if values.get(k, False))
            heatmap_data.append({"Date": date_key, "Tasks": done})
        df_heat = pd.DataFrame(heatmap_data)
        if not df_heat.empty:
            df_heat["Date"] = pd.to_datetime(df_heat["Date"])
            df_heat = df_heat.sort_values("Date")
            fig = go.Figure(go.Bar(
                x=df_heat["Date"],
                y=df_heat["Tasks"],
                marker=dict(
                    color=df_heat["Tasks"],
                    colorscale=[[0, "#0d1b2a"], [0.25, "#1e3a5f"], [0.6, "#1d4ed8"], [1.0, "#22c55e"]],
                    cmin=0, cmax=4
                )
            ))
            fig.update_layout(
                height=200, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b", range=[0, 4]),
                margin=dict(l=0, r=0, t=10, b=0)
            )
            st.plotly_chart(fig, use_container_width=True)

    with chart_tab2:
        pray_hist = []
        for d_key, v in data["history"].items():
            cnt = sum(1 for p in v.get("prayers", {}).values() if p)
            pray_hist.append({"Date": d_key, "Count": cnt})
        df_pray = pd.DataFrame(pray_hist)
        if not df_pray.empty:
            df_pray["Date"] = pd.to_datetime(df_pray["Date"])
            df_pray = df_pray.sort_values("Date")
            fig_p = go.Figure(go.Scatter(
                x=df_pray["Date"], y=df_pray["Count"],
                mode="lines+markers",
                line=dict(color="#22c55e", width=2),
                marker=dict(color="#22c55e", size=6),
                fill="tozeroy",
                fillcolor="rgba(34,197,94,0.1)"
            ))
            fig_p.update_layout(
                height=200, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b", range=[0, 6]),
                margin=dict(l=0, r=0, t=10, b=0)
            )
            st.plotly_chart(fig_p, use_container_width=True)

    with chart_tab3:
        mood_data = []
        for d_key, v in data["history"].items():
            if v.get("mood") or v.get("energy"):
                mood_data.append({
                    "Date": d_key,
                    "Mood": v.get("mood", 3),
                    "Energy": v.get("energy", 3)
                })
        if mood_data:
            df_mood = pd.DataFrame(mood_data)
            df_mood["Date"] = pd.to_datetime(df_mood["Date"])
            df_mood = df_mood.sort_values("Date")
            fig_m = go.Figure()
            fig_m.add_trace(go.Scatter(
                x=df_mood["Date"], y=df_mood["Mood"],
                name="Mood", mode="lines+markers",
                line=dict(color="#a78bfa", width=2),
                marker=dict(color="#a78bfa", size=6)
            ))
            fig_m.add_trace(go.Scatter(
                x=df_mood["Date"], y=df_mood["Energy"],
                name="Energy", mode="lines+markers",
                line=dict(color="#fb923c", width=2),
                marker=dict(color="#fb923c", size=6)
            ))
            fig_m.update_layout(
                height=200, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b", range=[0, 6]),
                legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
                margin=dict(l=0, r=0, t=10, b=0)
            )
            st.plotly_chart(fig_m, use_container_width=True)
        else:
            st.info("Start logging mood & energy from the Work Block tab to see trends.")

    # ── GOALS MANAGER (Monthly + Yearly) ──
    st.divider()
    st.markdown("### 🎯 Goals")
    tab_m, tab_y = st.tabs(["Monthly", "Yearly"])

    with tab_m:
        df_m = pd.DataFrame(data["goals"]["monthly"])
        if not df_m.empty:
            edited_m = st.data_editor(
                df_m, num_rows="dynamic",
                column_config={
                    "status": st.column_config.SelectboxColumn("Status", options=["Created", "In Progress", "On Hold", "Done"]),
                    "text": "Goal"
                },
                key="editor_monthly", use_container_width=True
            )
            if not df_m.equals(edited_m):
                data["goals"]["monthly"] = edited_m.to_dict("records")
                save_data(data)
                st.rerun()
        else:
            if st.button("＋ Add Monthly Goal"):
                data["goals"]["monthly"] = [{"text": "Example goal", "status": "Created"}]
                save_data(data)
                st.rerun()

    with tab_y:
        df_y = pd.DataFrame(data["goals"]["yearly"])
        if not df_y.empty:
            edited_y = st.data_editor(
                df_y, num_rows="dynamic",
                column_config={
                    "status": st.column_config.SelectboxColumn("Status", options=["Created", "In Progress", "On Hold", "Done"]),
                    "text": "Goal"
                },
                key="editor_yearly", use_container_width=True
            )
            if not df_y.equals(edited_y):
                data["goals"]["yearly"] = edited_y.to_dict("records")
                save_data(data)
                st.rerun()
        else:
            if st.button("＋ Add Yearly Goal"):
                data["goals"]["yearly"] = [{"text": "Example goal", "status": "Created"}]
                save_data(data)
                st.rerun()


        # ── Weekend & Holiday Preferences ──
        st.divider()
        with st.expander("⚙️ Weekend & Holiday Settings"):
            user_prefs = data.get("user_prefs", {})
            all_days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
            cur_weekends = user_prefs.get("weekends", ["Friday","Saturday"])
            cur_holidays = user_prefs.get("holidays", [])

            st.caption("Set which days are your weekends. Weekends use a relaxed day protocol.")
            new_weekends = st.multiselect("My Weekend Days", all_days, default=cur_weekends, key="pref_weekends")
            
            st.caption("Manual holidays (beyond regular weekends):")
            holiday_input = st.text_input("Add holiday date (YYYY-MM-DD)", placeholder="2025-06-15", key="holiday_input")
            hol_c1, hol_c2 = st.columns(2)
            with hol_c1:
                if st.button("➕ Add Holiday", key="add_holiday_btn") and holiday_input:
                    if holiday_input not in cur_holidays:
                        cur_holidays.append(holiday_input)
            with hol_c2:
                if cur_holidays:
                    remove_hol = st.selectbox("Remove holiday", ["—"] + sorted(cur_holidays), key="remove_hol_sel")
                    if st.button("❌ Remove", key="remove_hol_btn") and remove_hol != "—":
                        cur_holidays = [h for h in cur_holidays if h != remove_hol]

            if cur_holidays:
                st.markdown("**Saved holidays:** " + ", ".join(sorted(cur_holidays)))

            if st.button("💾 Save Preferences", key="save_wk_prefs"):
                if "user_prefs" not in data: data["user_prefs"] = {}
                data["user_prefs"]["weekends"] = new_weekends
                data["user_prefs"]["holidays"] = cur_holidays
                save_data(data)
                st.success("✅ Preferences saved!")
                st.rerun()
    # end of weekday else block



# ==========================================
# PAGE 2: GOLD & DOLLAR
# ==========================================
elif st.session_state['page'] == 'Gold':
    st.title("💰 Egyptian Market")

    @st.cache_data(ttl=300)
    def get_prices():
        try:
            r = requests.get(SCRAPE_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(r.content, 'html.parser')
            p = {}
            cells = soup.find_all(['td', 'th'])
            for i, c in enumerate(cells):
                txt = c.get_text(strip=True)
                val = 0
                if i + 1 < len(cells):
                    try:
                        val = float(cells[i + 1].get_text(strip=True).replace(',', '').replace('EGP', '').strip())
                    except:
                        pass
                if "عيار 21" in txt: p["g21"] = val
                if "عيار 24" in txt: p["g24"] = val
                if "الدولار" in txt or "أمريكي" in txt: p["usd"] = val
            return p
        except:
            return {}

    @st.cache_data(ttl=300)
    def get_bitcoin_price():
        """Fetch BTC/USD from CoinGecko public API (no key needed)"""
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
                timeout=8
            )
            data_btc = r.json()
            return {
                "usd": data_btc["bitcoin"]["usd"],
                "change_24h": data_btc["bitcoin"]["usd_24h_change"]
            }
        except:
            return {"usd": 0, "change_24h": 0}

    col_refresh, _ = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 Refresh Prices"):
            st.cache_data.clear()
            st.rerun()

    prices = get_prices()
    btc = get_bitcoin_price()
    usd_rate_gold = prices.get('usd', 50)
    btc_egp = btc["usd"] * usd_rate_gold if usd_rate_gold > 0 else 0

    today_iso = datetime.date.today().strftime("%Y-%m-%d")
    if prices.get("g21", 0) > 0:
        saved_entry = {**prices}
        if btc["usd"] > 0:
            saved_entry["btc_usd"] = btc["usd"]
            saved_entry["btc_egp"] = btc_egp
        data["price_history"][today_iso] = saved_entry
        save_data(data)

    # ── GOLD & USD CARDS ──
    st.markdown("<h3 style='margin-bottom:8px;'>🥇 Gold & Dollar</h3>", unsafe_allow_html=True)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("21k Buy", f"{prices.get('g21', 0):,.0f} EGP")
    c2.metric("24k Buy", f"{prices.get('g24', 0):,.0f} EGP")
    c3.metric("USD Buy", f"{prices.get('usd', 0):,.2f} EGP")
    c4.metric("21k Sell", f"{prices.get('g21', 0) - 20:,.0f} EGP")
    c5.metric("24k Sell", f"{prices.get('g24', 0) - 20:,.0f} EGP")
    c6.metric("USD Sell", f"{prices.get('usd', 0) - 0.1:,.2f} EGP")

    # ── BITCOIN CARD ──
    st.markdown("<h3 style='margin-top:20px; margin-bottom:8px;'>₿ Bitcoin</h3>", unsafe_allow_html=True)
    btc_change = btc.get("change_24h", 0)
    btc_change_color = "#22c55e" if btc_change >= 0 else "#ef4444"
    btc_arrow = "▲" if btc_change >= 0 else "▼"
    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("BTC / USD", f"${btc['usd']:,.0f}", delta=f"{btc_change:+.2f}% 24h")
    bc2.metric("BTC / EGP", f"{btc_egp:,.0f} EGP")
    bc3.metric("24h Change", f"{btc_arrow} {abs(btc_change):.2f}%")
    # 0.001 BTC value
    bc4.metric("0.001 BTC Value", f"${btc['usd']*0.001:,.2f} / {btc_egp*0.001:,.0f} EGP")

    st.divider()

    # ── CALCULATOR ──
    calc_col, btc_calc_col = st.columns(2)

    with calc_col:
        st.subheader("🧮 Gold & USD Calculator")
        item = st.selectbox("Asset", ["Gold 21k", "Gold 24k", "USD"], key="gold_calc_item")
        action = st.radio("Action", ["Buy (Pay EGP)", "Sell (Get EGP)"], key="gold_calc_action")
        amt_gold = st.number_input("Amount", min_value=0.0, step=0.5, key="gold_calc_amt")
        if item == "Gold 21k":
            price_gold = prices.get("g21", 0) - (20 if "Sell" in action else 0)
        elif item == "Gold 24k":
            price_gold = prices.get("g24", 0) - (20 if "Sell" in action else 0)
        else:
            price_gold = prices.get("usd", 0) - (0.1 if "Sell" in action else 0)
        total_gold = amt_gold * price_gold
        st.markdown(f"""
        <div style='background:#0d1b2a; border:1px solid #facc15; border-radius:12px; padding:18px; text-align:center; margin-top:10px;'>
            <div style='color:#64748b; font-size:0.75rem; text-transform:uppercase;'>Total Value</div>
            <div style='font-family:JetBrains Mono,monospace; font-size:2rem; font-weight:700; color:#facc15;'>{total_gold:,.2f}</div>
            <div style='color:#475569; font-size:0.8rem;'>Egyptian Pounds</div>
        </div>""", unsafe_allow_html=True)

    with btc_calc_col:
        st.subheader("₿ Bitcoin Calculator")
        btc_action = st.radio("Action", ["Buy BTC (Pay EGP)", "Sell BTC (Get EGP)"], key="btc_action")
        btc_calc_mode = st.radio("Calculate by", ["Amount (BTC)", "Budget (EGP)"], horizontal=True, key="btc_mode")
        if btc_calc_mode == "Amount (BTC)":
            btc_amt = st.number_input("BTC Amount", min_value=0.0, step=0.0001, format="%.6f", key="btc_amt_input")
            btc_result_egp = btc_amt * btc_egp
            btc_result_usd = btc_amt * btc.get("usd", 0)
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #f97316; border-radius:12px; padding:18px; text-align:center; margin-top:4px;'>
                <div style='color:#64748b; font-size:0.75rem; text-transform:uppercase;'>{"You Pay" if "Buy" in btc_action else "You Receive"}</div>
                <div style='font-family:JetBrains Mono,monospace; font-size:1.6rem; font-weight:700; color:#f97316;'>{btc_result_egp:,.2f} EGP</div>
                <div style='color:#64748b; font-size:0.82rem; font-family:JetBrains Mono,monospace;'>${btc_result_usd:,.2f} USD</div>
            </div>""", unsafe_allow_html=True)
        else:
            egp_budget = st.number_input("EGP Budget", min_value=0.0, step=100.0, key="btc_egp_input")
            btc_can_buy = (egp_budget / btc_egp) if btc_egp > 0 else 0
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #f97316; border-radius:12px; padding:18px; text-align:center; margin-top:4px;'>
                <div style='color:#64748b; font-size:0.75rem; text-transform:uppercase;'>BTC You {"Buy" if "Buy" in btc_action else "Need to Sell"}</div>
                <div style='font-family:JetBrains Mono,monospace; font-size:1.6rem; font-weight:700; color:#f97316;'>{btc_can_buy:.6f} BTC</div>
                <div style='color:#64748b; font-size:0.82rem; font-family:JetBrains Mono,monospace;'>${(egp_budget/usd_rate_gold if usd_rate_gold else 0):,.2f} USD equivalent</div>
            </div>""", unsafe_allow_html=True)

    st.divider()

    # ── PRICE HISTORY — SPLIT CHARTS ──
    st.markdown("### 📈 Price History")

    hist_data = []
    for d_key, v in data["price_history"].items():
        hist_data.append({
            "Date": d_key,
            "Gold 21k (EGP)": v.get("g21", 0),
            "Gold 24k (EGP)": v.get("g24", 0),
            "USD Rate (EGP)": v.get("usd", 0),
            "BTC (USD)": v.get("btc_usd", 0),
            "BTC (EGP)": v.get("btc_egp", 0),
        })
    df_hist = pd.DataFrame(hist_data)
    has_history = not df_hist.empty and len(df_hist) > 1

    if has_history:
        df_hist["Date"] = pd.to_datetime(df_hist["Date"])
        df_hist = df_hist.sort_values("Date").tail(60)

        chart_tab_g21, chart_tab_g24, chart_tab_usd, chart_tab_btc = st.tabs(
            ["🥇 Gold 21k", "🥇 Gold 24k", "💵 USD Rate", "₿ Bitcoin"])

        def make_line_chart(df, col, color, title, y_title):
            df_f = df[df[col] > 0].copy()
            if df_f.empty:
                return None
            # Add 7-day moving average
            df_f["MA7"] = df_f[col].rolling(7, min_periods=1).mean()
            fig = go.Figure()
            
            # --- THE FIX IS HERE ---
            # Properly converts 'rgba(R, G, B, 1)' to 'rgba(R, G, B, 0.15)' for the fill
            transparent_fill = color.replace(", 1)", ", 0.15)") 

            fig.add_trace(go.Scatter(x=df_f["Date"], y=df_f[col],
                name=title, mode="lines+markers",
                line=dict(color=color, width=2.5),
                marker=dict(size=5),
                fill="tozeroy", fillcolor=transparent_fill)) # Updated this variable
            
            fig.add_trace(go.Scatter(x=df_f["Date"], y=df_f["MA7"],
                name="7-day MA", mode="lines",
                line=dict(color="#94a3b8", width=1.5, dash="dot")))
            fig.update_layout(
                height=260, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color=color, showgrid=True, gridcolor="#1e293b", title=y_title),
                legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
                margin=dict(l=0, r=0, t=10, b=0))
            return fig

        with chart_tab_g21:
            fig_g21 = make_line_chart(df_hist, "Gold 21k (EGP)", "rgba(250, 204, 21, 1)", "Gold 21k", "EGP / gram")
            if fig_g21: st.plotly_chart(fig_g21, use_container_width=True)
            else: st.info("Not enough Gold 21k history yet.")

        with chart_tab_g24:
            fig_g24 = make_line_chart(df_hist, "Gold 24k (EGP)", "rgba(245, 158, 11, 1)", "Gold 24k", "EGP / gram")
            if fig_g24: st.plotly_chart(fig_g24, use_container_width=True)
            else: st.info("Not enough Gold 24k history yet.")

        with chart_tab_usd:
            fig_usd = make_line_chart(df_hist, "USD Rate (EGP)", "rgba(96, 165, 250, 1)", "USD Rate", "EGP per 1 USD")
            if fig_usd: st.plotly_chart(fig_usd, use_container_width=True)
            else: st.info("Not enough USD history yet.")

        with chart_tab_btc:
            fig_btc = make_line_chart(df_hist, "BTC (USD)", "rgba(249, 115, 22, 1)", "Bitcoin", "USD per BTC")
            if fig_btc: st.plotly_chart(fig_btc, use_container_width=True)
            else: st.info("Bitcoin history will grow each day you open this page.")
    else:
        st.info("Price history builds automatically each day you open this page. Check back tomorrow for charts.")

    st.divider()

    # ── MARKET INTELLIGENCE & SIGNALS ──
    st.markdown("### 🧠 Market Intelligence & Signals")
    st.caption("Rule-based signals derived from your historical price data. Not financial advice — use as one input among many.")

    def compute_signals(df_hist, prices, btc):
        signals = []
        usd_now = prices.get("usd", 0)
        g21_now = prices.get("g21", 0)
        g24_now = prices.get("g24", 0)
        btc_now = btc.get("usd", 0)
        btc_24h = btc.get("change_24h", 0)

        if len(df_hist) >= 7:
            recent7  = df_hist.tail(7)
            recent30 = df_hist.tail(30) if len(df_hist) >= 30 else df_hist

            # ── Gold 21k signals ──
            g21_7d_avg  = recent7["Gold 21k (EGP)"][recent7["Gold 21k (EGP)"] > 0].mean()
            g21_30d_avg = recent30["Gold 21k (EGP)"][recent30["Gold 21k (EGP)"] > 0].mean()
            g21_30d_min = recent30["Gold 21k (EGP)"][recent30["Gold 21k (EGP)"] > 0].min()
            g21_30d_max = recent30["Gold 21k (EGP)"][recent30["Gold 21k (EGP)"] > 0].max()

            if g21_now > 0 and g21_7d_avg > 0:
                g21_vs_7d = (g21_now - g21_7d_avg) / g21_7d_avg * 100
                if g21_now <= g21_30d_min * 1.02:
                    signals.append(("🥇 Gold 21k", "🟢 BUY ZONE", f"Price near 30-day low ({g21_30d_min:,.0f} EGP). Historically good entry point.", "#22c55e"))
                elif g21_now >= g21_30d_max * 0.98:
                    signals.append(("🥇 Gold 21k", "🔴 CAUTION", f"Price near 30-day high ({g21_30d_max:,.0f} EGP). Consider waiting for a pullback.", "#ef4444"))
                elif g21_vs_7d < -1.5:
                    signals.append(("🥇 Gold 21k", "🟡 WATCH", f"Down {abs(g21_vs_7d):.1f}% vs 7-day avg. Short-term dip — potential opportunity.", "#facc15"))
                elif g21_vs_7d > 2:
                    signals.append(("🥇 Gold 21k", "🟠 HOLD", f"Up {g21_vs_7d:.1f}% vs 7-day avg. Momentum positive but avoid chasing highs.", "#fb923c"))
                else:
                    signals.append(("🥇 Gold 21k", "⚪ NEUTRAL", f"Price stable near 7-day avg ({g21_7d_avg:,.0f} EGP). No strong signal.", "#64748b"))

            # ── USD signals ──
            usd_7d_avg  = recent7["USD Rate (EGP)"][recent7["USD Rate (EGP)"] > 0].mean()
            usd_30d_avg = recent30["USD Rate (EGP)"][recent30["USD Rate (EGP)"] > 0].mean()
            usd_30d_min = recent30["USD Rate (EGP)"][recent30["USD Rate (EGP)"] > 0].min()
            usd_30d_max = recent30["USD Rate (EGP)"][recent30["USD Rate (EGP)"] > 0].max()

            if usd_now > 0 and usd_7d_avg > 0:
                usd_vs_7d = (usd_now - usd_7d_avg) / usd_7d_avg * 100
                usd_trend_slope = (recent7["USD Rate (EGP)"].iloc[-1] - recent7["USD Rate (EGP)"].iloc[0]) if len(recent7) > 1 else 0
                if usd_now <= usd_30d_min * 1.01:
                    signals.append(("💵 USD", "🟢 BUY ZONE", f"USD near 30-day low ({usd_30d_min:.2f} EGP). Good time to buy/hold USD.", "#22c55e"))
                elif usd_trend_slope > 0 and usd_vs_7d > 0.5:
                    signals.append(("💵 USD", "🟠 RISING", f"USD up {usd_vs_7d:.2f}% this week — EGP weakening. Consider holding USD.", "#fb923c"))
                elif usd_trend_slope < 0 and usd_vs_7d < -0.5:
                    signals.append(("💵 USD", "🟡 FALLING", f"USD easing vs EGP. If you hold USD, this may be a good time to convert.", "#facc15"))
                else:
                    signals.append(("💵 USD", "⚪ NEUTRAL", f"USD stable around {usd_7d_avg:.2f} EGP. No strong entry/exit signal.", "#64748b"))

            # ── BTC signals ──
            if btc_now > 0:
                if btc_24h <= -5:
                    signals.append(("₿ Bitcoin", "🟢 DIP ALERT", f"BTC dropped {abs(btc_24h):.1f}% in 24h. Potential short-term buying opportunity for risk-tolerant investors.", "#22c55e"))
                elif btc_24h >= 5:
                    signals.append(("₿ Bitcoin", "🔴 CAUTION", f"BTC surged {btc_24h:.1f}% in 24h. Avoid FOMO buys — wait for consolidation.", "#ef4444"))
                elif btc_24h >= 2:
                    signals.append(("₿ Bitcoin", "🟠 MOMENTUM", f"BTC up {btc_24h:.1f}% today. Bullish short-term signal.", "#fb923c"))
                elif btc_24h <= -2:
                    signals.append(("₿ Bitcoin", "🟡 WATCH", f"BTC down {abs(btc_24h):.1f}% today. Monitor for continued decline or bounce.", "#facc15"))
                else:
                    signals.append(("₿ Bitcoin", "⚪ NEUTRAL", f"BTC relatively stable today ({btc_24h:+.2f}%). No strong intraday signal.", "#64748b"))

                # EGP perspective
                btc_egp_now = btc_now * usd_now if usd_now > 0 else 0
                if btc_egp_now > 0:
                    signals.append(("₿ BTC/EGP", "ℹ️ INFO", f"1 BTC = {btc_egp_now:,.0f} EGP today. Each 1% BTC move = ~{btc_egp_now*0.01:,.0f} EGP.", "#60a5fa"))

        # ── Portfolio diversification tip ──
        signals.append(("💡 General", "📊 STRATEGY", "Egypt's high inflation (~30%) makes gold & USD valuable hedges. BTC adds growth potential but high volatility. Suggested allocation: 50-60% EGP cash/savings, 25-30% Gold/USD, 5-10% BTC if risk-tolerant.", "#a78bfa"))

        return signals

    signals_list = compute_signals(df_hist if has_history else pd.DataFrame(), prices, btc)

    sig_grid = st.columns(2)
    for i, (asset, signal_type, message, color) in enumerate(signals_list):
        with sig_grid[i % 2]:
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-left:3px solid {color};
                 border-radius:10px; padding:14px 16px; margin-bottom:10px;'>
                <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;'>
                    <span style='color:#e2e8f0; font-weight:600; font-size:0.88rem;'>{asset}</span>
                    <span style='color:{color}; font-weight:700; font-size:0.8rem; background:{color}15;
                         padding:2px 10px; border-radius:999px;'>{signal_type}</span>
                </div>
                <div style='color:#94a3b8; font-size:0.82rem; line-height:1.5;'>{message}</div>
            </div>
            """, unsafe_allow_html=True)

    # ── AI-Powered Market Analysis ──
    st.divider()
    st.markdown("### 🤖 AI Market Analysis")
    api_key_check = get_gemini_key()
    if not api_key_check:
        st.info("🔑 Add your Gemini API key in the sidebar (🔑 AI Settings) to unlock AI-powered market analysis. Free key at aistudio.google.com/app/apikey")
    else:
        if st.button("🤖 Get AI Market Opinion", key="ai_market_btn", type="primary"):
            usd_n  = prices.get("usd", 0)
            g21_n  = prices.get("g21", 0)
            btc_n  = btc.get("usd", 0)
            btc_chg = btc.get("change_24h", 0)

            # Build history summary
            hist_summary = ""
            if has_history:
                df_last14 = df_hist.tail(14)
                hist_summary = f"""
Price History (last 14 days):
- Gold 21k range: {df_last14['Gold 21k (EGP)'][df_last14['Gold 21k (EGP)']>0].min():,.0f} – {df_last14['Gold 21k (EGP)'].max():,.0f} EGP
- USD range: {df_last14['USD Rate (EGP)'][df_last14['USD Rate (EGP)']>0].min():.2f} – {df_last14['USD Rate (EGP)'].max():.2f} EGP
- BTC range: ${df_last14['BTC (USD)'][df_last14['BTC (USD)']>0].min():,.0f} – ${df_last14['BTC (USD)'].max():,.0f}"""

            ai_prompt = f"""You are a financial analyst specialising in Egyptian markets and crypto assets.
Current market data (Egypt, today):
- Gold 21k: {g21_n:,.0f} EGP/gram
- Gold 24k: {prices.get('g24',0):,.0f} EGP/gram
- USD/EGP rate: {usd_n:.2f} EGP
- Bitcoin: ${btc_n:,.0f} USD ({btc_chg:+.2f}% 24h)
- BTC in EGP: {btc_n * usd_n:,.0f} EGP
{hist_summary}

Provide a concise market analysis covering:
1. GOLD outlook (2-3 sentences — buy, sell, or hold?)
2. USD/EGP outlook (2-3 sentences — is EGP stable or at risk?)
3. BITCOIN outlook (2-3 sentences — short-term and medium-term view)
4. PORTFOLIO SUGGESTION (specific allocation advice for an Egyptian investor with moderate risk tolerance)
5. KEY RISK to watch this month

Keep it practical, specific to Egyptian context, and under 400 words."""

            with st.spinner("🤖 Analysing markets..."):
                ai_market_text = call_gemini(ai_prompt, max_tokens=2500)
                today_str_ai = datetime.date.today().strftime('%B %d, %Y')
                st.markdown(
                    f"<div style='background:#0d1b2a; border:1px solid #3b82f6; border-radius:12px; padding:20px; margin-top:8px;'>"
                    f"<div style='color:#60a5fa; font-size:0.8rem; margin-bottom:10px; font-weight:600;'>🤖 AI Market Analysis — {today_str_ai}</div>"
                    f"<div style='color:#e2e8f0; font-size:0.87rem; white-space:pre-wrap; line-height:1.7;'>{ai_market_text}</div>"
                    f"</div>", unsafe_allow_html=True)


# ==========================================
# PAGE 3: FINANCE HUB
# ==========================================
elif st.session_state['page'] == 'Finance':
    st.title("💸 Finance Hub")
    fin = data["finance"]

    def safe_float(val):
        try:
            return float(val) if val is not None else 0.0
        except:
            return 0.0

    # ── Get live USD rate (reuse cached prices from Gold page helper) ──
    @st.cache_data(ttl=3600)
    def get_usd_rate():
        try:
            r = requests.get(SCRAPE_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(r.content, 'html.parser')
            cells = soup.find_all(['td', 'th'])
            for i, c in enumerate(cells):
                txt = c.get_text(strip=True)
                if ("الدولار" in txt or "أمريكي" in txt) and i + 1 < len(cells):
                    try:
                        return float(cells[i + 1].get_text(strip=True).replace(',', '').replace('EGP', '').strip())
                    except:
                        pass
        except:
            pass
        # fallback: use latest saved price_history
        if data["price_history"]:
            latest = sorted(data["price_history"].keys())[-1]
            return data["price_history"][latest].get("usd", 50.0)
        return 50.0

    usd_rate = get_usd_rate()

    # ── Convert income to EGP ──
    def income_to_egp(item):
        amt = safe_float(item.get('amount', 0))
        currency = item.get('currency', 'EGP')
        if currency == 'USD':
            return amt * usd_rate
        return amt

    inc_egp = sum(income_to_egp(x) for x in fin['income'])
    exp = sum(safe_float(x.get('amount')) for x in fin['expenses_monthly']) + \
          sum(safe_float(x.get('amount')) for x in fin['expenses_extra'])
    assets = sum(safe_float(x.get('value')) for x in fin['assets'])
    savings_rate = ((inc_egp - exp) / inc_egp * 100) if inc_egp > 0 else 0

    # USD rate banner
    st.markdown(f"""
    <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:10px; padding:10px 18px; margin-bottom:16px; display:flex; align-items:center; gap:12px;'>
        <span style='color:#64748b; font-size:0.8rem;'>💱 Live USD Rate:</span>
        <span style='font-family:JetBrains Mono,monospace; color:#60a5fa; font-weight:700; font-size:1.1rem;'>{usd_rate:,.2f} EGP</span>
        <span style='color:#334155; font-size:0.75rem;'>— used to convert USD income automatically</span>
    </div>
    """, unsafe_allow_html=True)

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Monthly Income", f"{inc_egp:,.0f} EGP")
    c2.metric("Total Expenses", f"{exp:,.0f} EGP", delta=f"{inc_egp - exp:,.0f} EGP net")
    c3.metric("Net Worth", f"{assets:,.0f} EGP")
    c4.metric("Savings Rate", f"{savings_rate:.1f}%")

    # Donut chart for expenses breakdown
    if fin['expenses_monthly'] or fin['expenses_extra']:
        st.divider()
        exp_col, bar_col = st.columns(2)
        with exp_col:
            st.subheader("💸 Expense Breakdown")
            all_exp = [(x.get('item', 'Fixed'), safe_float(x.get('amount'))) for x in fin['expenses_monthly']] + \
                      [(x.get('item', 'Extra'), safe_float(x.get('amount'))) for x in fin['expenses_extra']]
            if all_exp:
                labels, values = zip(*[(l, v) for l, v in all_exp if v > 0]) if any(v > 0 for _, v in all_exp) else ([], [])
                if labels:
                    fig_donut = go.Figure(go.Pie(
                        labels=labels, values=values, hole=0.6,
                        marker=dict(colors=px.colors.sequential.Blues_r)
                    ))
                    fig_donut.update_layout(
                        height=280, paper_bgcolor="#080c14",
                        font=dict(color="#94a3b8"),
                        showlegend=True,
                        legend=dict(bgcolor="rgba(0,0,0,0)"),
                        margin=dict(l=10, r=10, t=10, b=10)
                    )
                    st.plotly_chart(fig_donut, use_container_width=True)

        with bar_col:
            st.subheader("🎯 Financial Goals")
            for g in fin.get('goals', []):
                target = safe_float(g.get('target', 0))
                current = safe_float(g.get('current', 0))
                pct = (current / target * 100) if target > 0 else 0
                st.markdown(f"""
                <div style='margin-bottom:12px;'>
                    <div style='display:flex; justify-content:space-between; color:#94a3b8; font-size:0.82rem; margin-bottom:4px;'>
                        <span>{g.get('goal','Goal')}</span>
                        <span style='font-family:JetBrains Mono,monospace;'>{pct:.0f}%</span>
                    </div>
                    <div style='background:#1e293b; border-radius:4px; height:6px;'>
                        <div style='background:#3b82f6; width:{min(pct,100):.0f}%; height:6px; border-radius:4px;'></div>
                    </div>
                    <div style='color:#475569; font-size:0.75rem; margin-top:2px;'>Target: {target:,.0f} EGP</div>
                </div>
                """, unsafe_allow_html=True)

    st.divider()
    t1, t2, t3 = st.tabs(["💵 Income & Fixed", "📉 Extra Expenses", "🏠 Assets & Goals"])

    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Income Sources")
            st.caption("Set currency per source — USD income is auto-converted to EGP at live rate.")

            # Ensure all income rows have a currency field
            for row in fin['income']:
                if 'currency' not in row:
                    row['currency'] = 'EGP'

            df_inc = pd.DataFrame(fin['income']) if fin['income'] else pd.DataFrame([{"source": "Salary", "amount": 0, "currency": "EGP"}])
            if 'currency' not in df_inc.columns:
                df_inc['currency'] = 'EGP'

            ed_inc = st.data_editor(
                df_inc, num_rows="dynamic",
                column_config={
                    "source": st.column_config.TextColumn("Source", width="medium"),
                    "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
                    "currency": st.column_config.SelectboxColumn("Currency", options=["EGP", "USD"], width="small"),
                },
                key="ed_income", use_container_width=True
            )

            # Show converted breakdown
            if not ed_inc.empty:
                st.markdown("<div style='margin-top:8px;'>", unsafe_allow_html=True)
                total_shown = 0
                for _, row in ed_inc.iterrows():
                    amt = safe_float(row.get('amount', 0))
                    curr = row.get('currency', 'EGP')
                    egp_val = amt * usd_rate if curr == 'USD' else amt
                    total_shown += egp_val
                    if amt > 0:
                        tag = f"💵 ${amt:,.2f} × {usd_rate:.2f} = {egp_val:,.0f} EGP" if curr == 'USD' else f"💷 {amt:,.0f} EGP"
                        st.markdown(f"<div class='insight-card'><b>{row.get('source','?')}</b>: {tag}</div>", unsafe_allow_html=True)
                st.markdown(f"<div style='color:#22c55e; font-family:JetBrains Mono,monospace; font-size:0.9rem; margin-top:6px;'>Total: {total_shown:,.0f} EGP/month</div>", unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)

            if not df_inc.equals(ed_inc):
                data["finance"]["income"] = ed_inc.to_dict("records")
                save_data(data)
                st.rerun()

        with c2:
            st.subheader("Monthly Fixed Expenses")
            df_exp = pd.DataFrame(fin['expenses_monthly']) if fin['expenses_monthly'] else pd.DataFrame([{"item": "Rent", "amount": 0}])
            ed_exp = st.data_editor(df_exp, num_rows="dynamic",
                column_config={"amount": st.column_config.NumberColumn("Amount (EGP)", format="%.0f")},
                key="ed_exp_monthly", use_container_width=True)
            if not df_exp.equals(ed_exp):
                data["finance"]["expenses_monthly"] = ed_exp.to_dict("records")
                save_data(data)
                st.rerun()

    with t2:
        # ── Monthly scope: show only this month's extras ──
        cur_month_str = datetime.date.today().strftime("%Y-%m")
        all_extras = fin.get("expenses_extra", [])
        # Filter to current month (by date field)
        month_extras = [e for e in all_extras if str(e.get("date","")).startswith(cur_month_str)]
        other_extras = [e for e in all_extras if not str(e.get("date","")).startswith(cur_month_str)]

        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>"
            f"<h3 style='margin:0;'>📉 Extra Expenses</h3>"
            f"<span style='color:#475569;font-size:0.8rem;'>Showing: {datetime.date.today().strftime('%B %Y')} only</span>"
            f"</div>", unsafe_allow_html=True)
        st.caption("Expenses reset each month. Past months are archived.")

        df_extra = pd.DataFrame(month_extras) if month_extras else pd.DataFrame(
            columns=["item","category","amount","date","notes"])
        for col_n, dflt in [("item",""),("category","Other"),("amount",0),
                             ("date", datetime.date.today().strftime("%Y-%m-%d")),("notes","")]:
            if col_n not in df_extra.columns:
                df_extra[col_n] = dflt

        ed_extra = st.data_editor(
            df_extra.assign(date=pd.to_datetime(df_extra["date"], errors="coerce")),
            num_rows="dynamic",
            column_config={
                "item": st.column_config.TextColumn("Item", width="medium"),
                "category": st.column_config.SelectboxColumn("Category",
                    options=["Food","Transport","Entertainment","Health","Shopping","Utilities",
                             "Education","Subscriptions","Other"]),
                "amount": st.column_config.NumberColumn("Amount (EGP)", format="%.0f", min_value=0),
                "date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "notes": st.column_config.TextColumn("Notes"),
            },
            key="ed_exp_extra", use_container_width=True
        )
        ed_extra["date"] = ed_extra["date"].astype(str)

        if st.button("💾 Save Extra Expenses", key="save_extra_exp"):
            records = ed_extra.to_dict("records")
            # Ensure date field is string
            for r in records:
                if "date" in r and not isinstance(r["date"], str):
                    r["date"] = str(r["date"])
            data["finance"]["expenses_extra"] = other_extras + records
            save_data(data)
            st.success("✅ Saved!")
            st.rerun()

        # Month summary
        if not ed_extra.empty:
            month_total = sum(safe_float(r.get("amount",0)) for r in ed_extra.to_dict("records"))
            st.markdown(
                f"<div style='color:#fb923c;font-family:JetBrains Mono,monospace;font-size:0.9rem;margin-top:4px;'>"
                f"This month total: {month_total:,.0f} EGP</div>", unsafe_allow_html=True)

        # ── Month-over-month health indicator + PNG export (v10.3+) ──
        # Compares THIS month's extras to LAST month's. Shows a colored health
        # banner and a button to download a styled PNG report of last month.
        st.markdown("---")
        st.markdown("### 📊 Month-over-Month Comparison & PNG Report")

        # Compute last calendar month
        _today_extras = datetime.date.today()
        _first_of_this = _today_extras.replace(day=1)
        _last_month_end = _first_of_this - datetime.timedelta(days=1)
        last_month_str = _last_month_end.strftime("%Y-%m")
        last_month_label = _last_month_end.strftime("%B %Y")
        this_month_label = _today_extras.strftime("%B %Y")

        last_month_extras = [e for e in all_extras if str(e.get("date","")).startswith(last_month_str)]
        this_month_total = sum(safe_float(r.get("amount", 0)) for r in month_extras)
        last_month_total = sum(safe_float(r.get("amount", 0)) for r in last_month_extras)

        # Health verdict
        if last_month_total <= 0:
            health_color = "#3b82f6"
            health_emoji = "📊"
            health_title = "No prior month data"
            health_msg   = f"You spent {this_month_total:,.0f} EGP on extras in {this_month_label}. Once you have last month's data, we'll show you the trend."
            health_pct   = 0
        else:
            delta_pct = (this_month_total - last_month_total) / last_month_total * 100
            health_pct = delta_pct
            if delta_pct < -10:
                health_color, health_emoji = "#22c55e", "🟢"
                health_title = f"Spending DOWN {abs(delta_pct):.1f}%"
                health_msg   = (f"This month: {this_month_total:,.0f} EGP. Last month: {last_month_total:,.0f} EGP. "
                                f"You saved {last_month_total - this_month_total:,.0f} EGP — keep it up!")
            elif delta_pct < 10:
                health_color, health_emoji = "#3b82f6", "↔️"
                health_title = f"Spending stable ({delta_pct:+.1f}%)"
                health_msg   = (f"This month: {this_month_total:,.0f} EGP. Last month: {last_month_total:,.0f} EGP. "
                                f"Within ±10% of last month — steady.")
            elif delta_pct < 30:
                health_color, health_emoji = "#f59e0b", "🟡"
                health_title = f"Spending UP {delta_pct:.1f}%"
                health_msg   = (f"This month: {this_month_total:,.0f} EGP. Last month: {last_month_total:,.0f} EGP. "
                                f"You spent {this_month_total - last_month_total:,.0f} EGP more this month. "
                                f"Worth a look — see the report below.")
            else:
                health_color, health_emoji = "#ef4444", "🔴"
                health_title = f"Spending UP {delta_pct:.1f}%"
                health_msg   = (f"This month: {this_month_total:,.0f} EGP. Last month: {last_month_total:,.0f} EGP. "
                                f"That's {this_month_total - last_month_total:,.0f} EGP more — review your categories below.")

        st.markdown(
            f"<div style='background:var(--bg-surface);border:1px solid {health_color};"
            f"border-left:4px solid {health_color};border-radius:12px;padding:14px 18px;margin-bottom:14px;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
            f"<div><h4 style='margin:0;color:var(--text-heading);font-size:1.05rem;'>{health_emoji} {health_title}</h4>"
            f"<div style='color:var(--text);font-size:0.88rem;margin-top:6px;line-height:1.5;'>{health_msg}</div></div>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

        # Quick metrics row
        mom_c1, mom_c2, mom_c3 = st.columns(3)
        mom_c1.metric(this_month_label, f"{this_month_total:,.0f} EGP")
        mom_c2.metric(last_month_label, f"{last_month_total:,.0f} EGP")
        if last_month_total > 0:
            delta_egp = this_month_total - last_month_total
            mom_c3.metric("Difference", f"{delta_egp:+,.0f} EGP", f"{health_pct:+.1f}%",
                          delta_color="inverse")

        # ── Generate PNG report of LAST MONTH'S extras ──
        if not last_month_extras:
            st.info(f"📭 No extra expenses logged for {last_month_label} — nothing to export yet.")
        else:
            # Group by category
            cat_totals = {}
            for e in last_month_extras:
                cat = e.get("category", "Other") or "Other"
                cat_totals[cat] = cat_totals.get(cat, 0) + safe_float(e.get("amount", 0))
            cat_sorted = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)

            # Quick summary preview
            with st.expander(f"👀 Preview {last_month_label} breakdown", expanded=False):
                preview_html = "<div style='display:grid;grid-template-columns:1fr 1fr;gap:6px;'>"
                for cat, total in cat_sorted:
                    pct = (total / last_month_total * 100) if last_month_total else 0
                    preview_html += (f"<div style='display:flex;justify-content:space-between;"
                                     f"background:var(--bg-surface);border:1px solid var(--border-2);"
                                     f"border-radius:8px;padding:8px 12px;'>"
                                     f"<span><b>{cat}</b></span>"
                                     f"<span style='font-family:JetBrains Mono,monospace;'>"
                                     f"{total:,.0f} EGP <span style='color:var(--text-dim);'>"
                                     f"({pct:.0f}%)</span></span></div>")
                preview_html += "</div>"
                st.markdown(preview_html, unsafe_allow_html=True)

            # ── PNG generation ──
            def _build_extras_png(month_label, extras_list, total, cat_breakdown,
                                  prev_total, prev_label, delta_pct, health_color_hex):
                """Render a clean PNG report card for sharing."""
                fig, axes = plt.subplots(2, 1, figsize=(8.5, 11),
                                         gridspec_kw={'height_ratios': [1, 2.2]},
                                         dpi=150)
                fig.patch.set_facecolor("#0d1b2a")

                # ── Top: hero header with totals ──
                ax_h = axes[0]
                ax_h.set_facecolor("#0d1b2a")
                ax_h.axis("off")
                ax_h.set_xlim(0, 10); ax_h.set_ylim(0, 10)

                # Brand — text-only since matplotlib's DejaVu Sans lacks emoji glyphs
                ax_h.text(0.3, 9.0, "THRIVO", fontsize=22, color="#22c55e",
                          fontweight="bold", family="sans-serif")
                ax_h.text(0.3, 8.1, f"Extra Expenses Report — {month_label}",
                          fontsize=14, color="#cbd5e1", family="sans-serif")

                # Total
                ax_h.text(0.3, 6.6, "TOTAL", fontsize=9, color="#64748b",
                          family="sans-serif")
                ax_h.text(0.3, 5.2, f"{total:,.0f}", fontsize=42, color="#e2e8f0",
                          fontweight="bold", family="monospace")
                ax_h.text(0.3, 4.0, "EGP", fontsize=14, color="#94a3b8", family="sans-serif")

                # MoM box
                if prev_total > 0:
                    mom_txt = f"vs {prev_label}: {delta_pct:+.1f}%"
                    ax_h.add_patch(mpatches.FancyBboxPatch(
                        (5.5, 4.0), 4.2, 1.2, boxstyle="round,pad=0.05,rounding_size=0.18",
                        edgecolor=health_color_hex, facecolor=health_color_hex + "22",
                        linewidth=2,
                    ))
                    ax_h.text(5.7, 4.85, mom_txt, fontsize=12, color=health_color_hex,
                              fontweight="bold", family="monospace")
                    ax_h.text(5.7, 4.35, f"{prev_label}: {prev_total:,.0f} EGP",
                              fontsize=9, color="#94a3b8", family="sans-serif")

                # Item count
                ax_h.text(0.3, 2.6, f"{len(extras_list)} items", fontsize=11,
                          color="#475569", family="sans-serif")
                ax_h.text(0.3, 1.9, f"across {len(cat_breakdown)} categories",
                          fontsize=10, color="#475569", family="sans-serif")

                # ── Bottom: horizontal bar chart of categories ──
                ax_b = axes[1]
                ax_b.set_facecolor("#0d1b2a")
                cats_to_show = cat_breakdown[:10]
                cat_names  = [c[0] for c in cats_to_show]
                cat_values = [c[1] for c in cats_to_show]

                # Friendly category palette
                palette = ["#22c55e", "#3b82f6", "#f59e0b", "#a855f7",
                           "#ec4899", "#14b8a6", "#ef4444", "#eab308",
                           "#06b6d4", "#8b5cf6"]
                bar_colors = [palette[i % len(palette)] for i in range(len(cat_values))]

                y_pos = np.arange(len(cat_names))
                bars = ax_b.barh(y_pos, cat_values, color=bar_colors, edgecolor="none", height=0.65)
                ax_b.set_yticks(y_pos)
                ax_b.set_yticklabels(cat_names, fontsize=11, color="#e2e8f0")
                ax_b.invert_yaxis()
                ax_b.set_xlabel("EGP", color="#94a3b8", fontsize=10)
                ax_b.tick_params(colors="#94a3b8")
                ax_b.spines["top"].set_visible(False)
                ax_b.spines["right"].set_visible(False)
                ax_b.spines["bottom"].set_color("#334155")
                ax_b.spines["left"].set_color("#334155")
                ax_b.grid(axis="x", color="#1e293b", linestyle="--", alpha=0.6)
                ax_b.set_axisbelow(True)
                ax_b.set_title(f"Breakdown by category — {month_label}",
                               fontsize=13, color="#e2e8f0", pad=14, loc="left")

                # Value labels on bars
                max_v = max(cat_values) if cat_values else 1
                for bar, val in zip(bars, cat_values):
                    pct = (val / total * 100) if total else 0
                    ax_b.text(bar.get_width() + max_v * 0.01,
                              bar.get_y() + bar.get_height() / 2,
                              f"{val:,.0f} ({pct:.0f}%)",
                              va="center", color="#cbd5e1", fontsize=10,
                              family="monospace")

                # Footer
                fig.text(0.05, 0.015,
                         f"Generated {datetime.date.today().strftime('%Y-%m-%d')} · Thrivo Finance",
                         fontsize=8, color="#475569")
                fig.text(0.95, 0.015, "thrivo.app", fontsize=8, color="#475569",
                         ha="right")

                plt.tight_layout(rect=[0, 0.03, 1, 1])
                buf = io.BytesIO()
                plt.savefig(buf, format="png", facecolor="#0d1b2a",
                            edgecolor="none", bbox_inches="tight", dpi=150)
                plt.close(fig)
                buf.seek(0)
                return buf.getvalue()

            png_bytes = _build_extras_png(
                last_month_label, last_month_extras, last_month_total,
                cat_sorted, this_month_total, this_month_label,
                health_pct, health_color,
            )

            st.download_button(
                f"📸 Download PNG report — {last_month_label}",
                data=png_bytes,
                file_name=f"thrivo_extras_{last_month_str}.png",
                mime="image/png",
                use_container_width=True,
                type="primary",
            )

    with t3:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Assets")
            df_assets = pd.DataFrame(fin['assets']) if fin['assets'] else pd.DataFrame([{"asset": "Gold", "value": 0}])
            ed_assets = st.data_editor(df_assets, num_rows="dynamic",
                column_config={"value": st.column_config.NumberColumn("Value (EGP)", format="%.0f")},
                key="ed_assets", use_container_width=True)
            if not df_assets.equals(ed_assets):
                data["finance"]["assets"] = ed_assets.to_dict("records")
                save_data(data)
                st.rerun()
        with c2:
            st.subheader("Financial Goals")
            df_goals = pd.DataFrame(fin['goals']) if fin['goals'] else pd.DataFrame([{"goal": "House", "target": 1000000, "current": 0}])
            if "current" not in df_goals.columns:
                df_goals["current"] = 0
            ed_goals = st.data_editor(df_goals, num_rows="dynamic",
                column_config={
                    "target": st.column_config.NumberColumn("Target (EGP)", format="%.0f"),
                    "current": st.column_config.NumberColumn("Saved (EGP)", format="%.0f")
                },
                key="ed_fin_goals", use_container_width=True)
            if not df_goals.equals(ed_goals):
                data["finance"]["goals"] = ed_goals.to_dict("records")
                save_data(data)
                st.rerun()


    # ── AI FINANCIAL GOALS ADVISER ──
    st.divider()
    st.markdown("### 🤖 AI Financial Goals Adviser")
    st.caption("Powered by Gemini AI — analyses your expenses, savings, assets, and market data to build you a personalised plan.")

    gemini_key_fin = get_gemini_key()
    if not gemini_key_fin:
        st.info("🔑 Add your Gemini API key in the sidebar (🔑 AI Settings) to unlock the AI Financial Goals Adviser.")
    else:
        fa_c1, fa_c2 = st.columns(2)
        with fa_c1:
            include_credits = st.checkbox("Include credit card options in the plan?", value=True, key="fin_ai_credits")
            risk_profile = st.selectbox("Your risk tolerance", ["Conservative", "Moderate", "Aggressive"], index=1, key="fin_ai_risk")
            time_horizon = st.selectbox("Investment time horizon", ["Short (< 1 year)", "Medium (1–3 years)", "Long (3+ years)"], index=1, key="fin_ai_horizon")
        with fa_c2:
            custom_goal_input = st.text_input("Specific goal to plan for (optional)", placeholder="e.g. Buy a car in 18 months for 500,000 EGP", key="fin_ai_goal_input")
            market_context = st.checkbox("Include Gold/USD/BTC market context?", value=True, key="fin_ai_market")

        if st.button("🤖 Generate My Financial Plan", type="primary", key="gen_fin_plan", use_container_width=False):
            # Build comprehensive financial context
            goals_text = ""
            for g in fin.get("goals", []):
                target = safe_float(g.get("target", 0))
                current_saved = safe_float(g.get("current", 0))
                remaining = target - current_saved
                months_needed = (remaining / max(inc_egp - exp, 1)) if (inc_egp - exp) > 0 else 999
                goals_text += f"  • {g.get('goal','?')}: Target {target:,.0f} EGP, Saved {current_saved:,.0f} EGP, Remaining {remaining:,.0f} EGP\n"

            assets_text = "\n".join([f"  • {a.get('asset','?')}: {safe_float(a.get('value',0)):,.0f} EGP" for a in fin.get("assets",[])])
            income_text = "\n".join([f"  • {i.get('source','?')}: {safe_float(i.get('amount',0)):,.2f} {i.get('currency','EGP')}" for i in fin.get("income",[])])
            fixed_exp_text = "\n".join([f"  • {e.get('item','?')}: {safe_float(e.get('amount',0)):,.0f} EGP" for e in fin.get("expenses_monthly",[])])

            # Credit context
            credit_text = ""
            if include_credits:
                credit_data = data.get("credit", {})
                c_tx = credit_data.get("transactions", [])
                qnb_spent = sum(safe_float(t.get("amount",0)) for t in c_tx if t.get("bank","QNB")=="QNB" and t.get("type")=="Purchase")
                eg_spent  = sum(safe_float(t.get("amount",0)) for t in c_tx if t.get("bank","QNB")=="EGBank" and t.get("type")=="Purchase")
                qnb_limit = safe_float(credit_data.get("limits",{}).get("QNB",0))
                eg_limit  = safe_float(credit_data.get("limits",{}).get("EGBank",0))
                credit_text = f"""Credit Cards Available:
  • QNB: Limit {qnb_limit:,.0f} EGP, Spent {qnb_spent:,.0f} EGP, Rate 4.35%/month
  • EGBank: Limit {eg_limit:,.0f} EGP, Spent {eg_spent:,.0f} EGP, Rate 3%/month"""

            # Market context
            market_text = ""
            if market_context and data.get("price_history"):
                latest_k = sorted(data["price_history"].keys())[-1]
                ph = data["price_history"][latest_k]
                market_text = f"""Current Egyptian Market:
  • Gold 21k: {ph.get("g21",0):,.0f} EGP/gram
  • USD Rate: {ph.get("usd",0):.2f} EGP
  • Bitcoin: ${ph.get("btc_usd",0):,.0f} USD"""

            prompt_fin = f"""You are an expert Egyptian personal finance adviser. Create a detailed, personalised financial plan.

=== FINANCIAL PROFILE ===
Monthly Income: {inc_egp:,.0f} EGP/month
Income sources:
{income_text}

Monthly Fixed Expenses: {exp:,.0f} EGP
Fixed expenses:
{fixed_exp_text}

Net Monthly Savings Potential: {inc_egp - exp:,.0f} EGP
Savings Rate: {savings_rate:.1f}%

Total Assets: {assets:,.0f} EGP
Assets:
{assets_text if assets_text else "  • None listed"}

Financial Goals:
{goals_text if goals_text else "  • No specific goals set"}

{credit_text}

{market_text}

User Risk Profile: {risk_profile}
Investment Horizon: {time_horizon}
Specific Goal: {custom_goal_input if custom_goal_input else "General wealth building"}

=== TASK ===
Create a comprehensive financial plan with:
1. PRIORITY RANKING of goals (which to tackle first and why)
2. MONTHLY SAVINGS ALLOCATION (exact EGP split across goals/investments)
3. INVESTMENT STRATEGY (Gold, USD, stocks, or other assets suitable for Egypt)
4. TIMELINE for each goal (realistic months to achieve based on numbers)
5. CREDIT USAGE ADVICE (should they use credit cards to accelerate any goal? Specific recommendation with risk/benefit)
6. MARKET OPPORTUNITIES (any current Gold/USD/BTC opportunity worth acting on?)
7. RISK WARNINGS (3 specific risks to watch for)
8. QUICK WINS (2-3 actions to take this month)

Be specific with EGP amounts. Make the plan realistic for an Egyptian professional."""

            with st.spinner("🤖 Building your personalised financial plan..."):
                fin_plan = call_gemini(prompt_fin, max_tokens=1800, temperature=0.6)

            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #22c55e; border-radius:14px; padding:22px; margin-top:12px;'>
                <div style='color:#22c55e; font-size:0.82rem; font-weight:600; margin-bottom:12px;'>
                    🤖 AI Financial Plan — Generated {datetime.date.today().strftime('%B %d, %Y')}
                </div>
                <div style='color:#e2e8f0; font-size:0.87rem; white-space:pre-wrap; line-height:1.75;'>{fin_plan}</div>
            </div>""", unsafe_allow_html=True)




# ==========================================
# PAGE 4: CREDIT TRACKER — QNB & EGBank
# ==========================================
elif st.session_state['page'] == 'Credit':
    st.title("💳 Credit & Installments")
    st.caption("All your credit cards and installment programs in one place — Egyptian banks plus Valu, Souhoola, ContactNow, MidTakseet, Sympl, Halan, and more.")

    credit = data["credit"]
    today_dt = datetime.date.today()
    current_month_str = today_dt.strftime("%Y-%m")

    # ── My Accounts (v10.2+) — flexible card / installment-program manager ──
    accounts = credit.setdefault("accounts", [])

    # Catalog of common providers — pre-populated for convenience
    PROVIDER_PRESETS = {
        # Egyptian credit-card banks
        "QNB":           {"kind": "credit_card",   "color": "#a020f0", "default_apr": 52.2,  "min_pmt_pct": 5},
        "CIB":           {"kind": "credit_card",   "color": "#003366", "default_apr": 51.0,  "min_pmt_pct": 5},
        "NBE":           {"kind": "credit_card",   "color": "#006633", "default_apr": 48.0,  "min_pmt_pct": 5},
        "Banque Misr":   {"kind": "credit_card",   "color": "#dc143c", "default_apr": 48.0,  "min_pmt_pct": 5},
        "Bank of Alex":  {"kind": "credit_card",   "color": "#0066cc", "default_apr": 50.0,  "min_pmt_pct": 5},
        "EGBank":        {"kind": "credit_card",   "color": "#22c55e", "default_apr": 36.0,  "min_pmt_pct": 5},
        "ADIB":          {"kind": "credit_card",   "color": "#8b0000", "default_apr": 50.0,  "min_pmt_pct": 5},
        "HSBC Egypt":    {"kind": "credit_card",   "color": "#db0011", "default_apr": 51.0,  "min_pmt_pct": 5},
        # Egyptian installment apps / BNPL programs
        "Valu":          {"kind": "installment_app", "color": "#9333ea", "default_apr": 22.0, "min_pmt_pct": 0},
        "Souhoola":      {"kind": "installment_app", "color": "#f97316", "default_apr": 24.0, "min_pmt_pct": 0},
        "ContactNow":    {"kind": "installment_app", "color": "#0ea5e9", "default_apr": 26.0, "min_pmt_pct": 0},
        "Halan":         {"kind": "installment_app", "color": "#ef4444", "default_apr": 28.0, "min_pmt_pct": 0},
        "MidTakseet":    {"kind": "installment_app", "color": "#14b8a6", "default_apr": 24.0, "min_pmt_pct": 0},
        "Sympl":         {"kind": "installment_app", "color": "#ec4899", "default_apr": 0,    "min_pmt_pct": 0},
        "Khazna":        {"kind": "installment_app", "color": "#6366f1", "default_apr": 25.0, "min_pmt_pct": 0},
        "aman":          {"kind": "installment_app", "color": "#64748b", "default_apr": 24.0, "min_pmt_pct": 0},
        "Other / Custom":{"kind": "credit_card",   "color": "#475569", "default_apr": 50.0,  "min_pmt_pct": 5},
    }

    # ── Top-of-page overview metrics (across ALL accounts: legacy + user-added) ──
    _credit_legacy_balance = sum(float(v or 0) for v in credit.get("balances", {}).values())
    _credit_legacy_limit   = sum(float(v or 0) for v in credit.get("limits",   {}).values())
    _usd_rate_credit = _resolve_usd_rate(data)
    _accounts_balance = sum(amount_to_egp(a.get("balance", 0), a.get("currency", "EGP"), _usd_rate_credit) for a in accounts)
    _accounts_limit   = sum(amount_to_egp(a.get("limit",   0), a.get("currency", "EGP"), _usd_rate_credit) for a in accounts)
    total_debt    = _credit_legacy_balance + _accounts_balance
    total_limit   = _credit_legacy_limit   + _accounts_limit
    total_avail   = max(0, total_limit - total_debt)
    total_util    = (total_debt / total_limit * 100) if total_limit > 0 else 0

    util_color = ("#22c55e" if total_util <= 30 else
                  "#f59e0b" if total_util <= 60 else "#ef4444")
    util_label = ("Healthy" if total_util <= 30 else
                  "Watch this" if total_util <= 60 else "High — pay down")

    st.markdown(
        f"""<div style='background:linear-gradient(135deg, var(--bg-surface) 0%, var(--bg-surface-2) 100%);
            border:1px solid var(--border-2);border-radius:16px;padding:18px 22px;margin:12px 0 18px;'>
            <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:18px;'>
              <div>
                <div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;
                    letter-spacing:0.1em;margin-bottom:2px;'>Total Debt</div>
                <div style='font-family:JetBrains Mono,monospace;font-size:1.7rem;
                    font-weight:700;color:var(--text-heading);'>{total_debt:,.0f} EGP</div>
              </div>
              <div>
                <div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;
                    letter-spacing:0.1em;margin-bottom:2px;'>Available Credit</div>
                <div style='font-family:JetBrains Mono,monospace;font-size:1.7rem;
                    font-weight:700;color:#22c55e;'>{total_avail:,.0f} EGP</div>
              </div>
              <div>
                <div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;
                    letter-spacing:0.1em;margin-bottom:2px;'>Total Credit Limit</div>
                <div style='font-family:JetBrains Mono,monospace;font-size:1.7rem;
                    font-weight:700;color:var(--text-heading);'>{total_limit:,.0f} EGP</div>
              </div>
              <div>
                <div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;
                    letter-spacing:0.1em;margin-bottom:2px;'>Utilization</div>
                <div style='font-family:JetBrains Mono,monospace;font-size:1.7rem;
                    font-weight:700;color:{util_color};'>{total_util:.1f}%</div>
                <div style='font-size:0.72rem;color:{util_color};font-weight:600;'>{util_label}</div>
              </div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Tabbed layout (Overview + My Accounts only — QNB/EGBank detail follows below) ──
    credit_tab_overview, credit_tab_accounts = st.tabs([
        "📊 Overview",
        f"🏦 My Accounts ({len(accounts)})",
    ])

    # ╔══════════════════════════════════════════════════════════════════
    # ║ TAB 1 — OVERVIEW (visual breakdown of all accounts)
    # ╚══════════════════════════════════════════════════════════════════
    with credit_tab_overview:
        if not accounts and total_debt == 0:
            st.markdown(
                "<div style='background:var(--bg-surface);border:1px dashed var(--border-2);"
                "border-radius:12px;padding:32px 18px;text-align:center;color:var(--text-muted);'>"
                "<div style='font-size:2.4rem;margin-bottom:8px;'>💳</div>"
                "<div style='font-size:1rem;color:var(--text);font-weight:600;'>No credit accounts yet.</div>"
                "<div style='font-size:0.85rem;margin-top:6px;'>"
                "Switch to the <b>🏦 My Accounts</b> tab to add your first card or installment program.</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        else:
            # Pie chart of debt by account
            chart_rows = []
            for a in accounts:
                bal_egp = amount_to_egp(a.get("balance", 0), a.get("currency", "EGP"), _usd_rate_credit)
                if bal_egp > 0:
                    label = a.get("nickname") or a["provider"]
                    chart_rows.append({"name": label, "value": bal_egp, "color": a.get("color", "#475569")})
            for bank, bal in credit.get("balances", {}).items():
                if float(bal or 0) > 0:
                    chart_rows.append({"name": f"{bank} (legacy)", "value": float(bal),
                                       "color": "#3b82f6" if bank == "QNB" else "#22c55e"})

            if chart_rows:
                col_pie, col_bars = st.columns([1, 1])
                with col_pie:
                    st.markdown("#### 💸 Where your debt lives")
                    fig_pie = go.Figure(data=[go.Pie(
                        labels=[r["name"] for r in chart_rows],
                        values=[r["value"] for r in chart_rows],
                        hole=0.55,
                        marker=dict(colors=[r["color"] for r in chart_rows]),
                        textinfo="label+percent",
                        textfont=dict(color="#e2e8f0", size=11),
                    )])
                    fig_pie.update_layout(
                        height=280,
                        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                        showlegend=False,
                        margin=dict(l=0, r=0, t=10, b=0),
                    )
                    st.plotly_chart(fig_pie, use_container_width=True)
                with col_bars:
                    st.markdown("#### 📊 Utilization per account")
                    util_rows = []
                    for a in accounts:
                        lim = float(a.get("limit", 0) or 0)
                        bal = float(a.get("balance", 0) or 0)
                        if lim > 0:
                            util_rows.append({
                                "name":  (a.get("nickname") or a["provider"])[:18],
                                "util":  bal / lim * 100,
                                "color": a.get("color", "#475569"),
                            })
                    for bank in credit.get("limits", {}):
                        lim = float(credit["limits"].get(bank, 0) or 0)
                        bal = float(credit["balances"].get(bank, 0) or 0)
                        if lim > 0:
                            util_rows.append({"name": bank, "util": bal / lim * 100,
                                              "color": "#3b82f6" if bank == "QNB" else "#22c55e"})

                    if util_rows:
                        fig_bar = go.Figure()
                        fig_bar.add_trace(go.Bar(
                            x=[r["util"] for r in util_rows],
                            y=[r["name"] for r in util_rows],
                            orientation="h",
                            marker_color=[r["color"] for r in util_rows],
                            text=[f"{r['util']:.1f}%" for r in util_rows],
                            textposition="outside",
                        ))
                        # 30% healthy line
                        fig_bar.add_vline(x=30, line_dash="dot", line_color="#22c55e",
                                          line_width=1, opacity=0.5)
                        fig_bar.add_vline(x=60, line_dash="dot", line_color="#ef4444",
                                          line_width=1, opacity=0.5)
                        fig_bar.update_layout(
                            height=280,
                            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                            xaxis=dict(color="#94a3b8", showgrid=True, gridcolor="#1e293b",
                                       title="Utilization %", range=[0, max(105, max((r['util'] for r in util_rows), default=100) + 10)]),
                            yaxis=dict(color="#cbd5e1", showgrid=False, autorange="reversed"),
                            margin=dict(l=0, r=20, t=10, b=20),
                            showlegend=False,
                        )
                        st.plotly_chart(fig_bar, use_container_width=True)
                    else:
                        st.caption("Add accounts to see utilization breakdown.")

            # Health signals
            st.markdown("---")
            st.markdown("#### 🩺 Health Signals")
            signals_html = []
            if total_util <= 30:
                signals_html.append(("🟢", "Utilization is healthy", f"{total_util:.1f}% — well under the 30% credit-score threshold.", "var(--accent)"))
            elif total_util <= 60:
                signals_html.append(("🟡", "Utilization is climbing", f"{total_util:.1f}% — try to keep it under 30% for credit score.", "var(--warn)"))
            else:
                signals_html.append(("🔴", "High utilization", f"{total_util:.1f}% — pay down to protect your credit score.", "var(--danger)"))

            high_apr = sorted(
                [a for a in accounts if float(a.get("balance", 0) or 0) > 0],
                key=lambda x: float(x.get("apr", 0) or 0),
                reverse=True,
            )
            if high_apr and float(high_apr[0].get("apr", 0) or 0) >= 30:
                worst = high_apr[0]
                signals_html.append(("⚠️", f"High APR account",
                    f"{worst['provider']} at {worst.get('apr', 0):.1f}% APR carries balance — prioritize paying this first.",
                    "var(--warn)"))

            for emoji, title, msg, color in signals_html:
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                    f"border-left:3px solid {color};border-radius:10px;padding:10px 14px;margin-bottom:8px;'>"
                    f"<b>{emoji} {title}</b><br>"
                    f"<span style='color:var(--text-muted);font-size:0.86rem;'>{msg}</span></div>",
                    unsafe_allow_html=True,
                )

    # ╔══════════════════════════════════════════════════════════════════
    # ║ TAB 2 — MY ACCOUNTS (form + list, was the expander before)
    # ╚══════════════════════════════════════════════════════════════════
    with credit_tab_accounts:
        st.caption(
            "Add the credit cards and installment apps (Valu, Souhoola, etc.) you actually use. "
            "Each account is private to your login. For QNB / EGBank with full transaction-level "
            "tracking, use the <b>QNB & EGBank Detail</b> tab.",
        )

        # ── Add new account form ──
        with st.expander("➕ Add a new account", expanded=len(accounts) == 0):
            with st.form("add_credit_account_form_v2", clear_on_submit=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    provider_choice = st.selectbox(
                        "Provider",
                        options=list(PROVIDER_PRESETS.keys()),
                        help="Pick from the catalog. Use 'Other / Custom' for anything not listed.",
                    )
                    custom_name = ""
                    if provider_choice == "Other / Custom":
                        custom_name = st.text_input("Custom provider name *",
                                                    placeholder="e.g. AAIB, Mashreq, ...")
                    nickname = st.text_input("Nickname (optional)",
                                             placeholder="e.g. 'Daily card', 'Travel Visa'",
                                             help="Helpful if you have 2+ cards from the same bank")
                with c2:
                    preset = PROVIDER_PRESETS[provider_choice]
                    kind_choice = st.selectbox(
                        "Type", options=["credit_card", "installment_app"],
                        index=0 if preset["kind"] == "credit_card" else 1,
                        format_func=lambda k: "💳 Credit Card" if k == "credit_card" else "📲 Installment App",
                    )
                    currency_choice = st.selectbox("Currency", options=["EGP", "USD"], index=0)
                with c3:
                    limit_input    = st.number_input("Credit Limit",        min_value=0.0, step=1000.0, value=0.0)
                    balance_input  = st.number_input("Current Balance Owed", min_value=0.0, step=100.0,  value=0.0)

                c4, c5, c6 = st.columns([1, 1, 1])
                with c4:
                    apr_input = st.number_input("APR (% per year)",
                                                min_value=0.0, max_value=200.0, step=0.5,
                                                value=float(preset["default_apr"]),
                                                help="Annual percentage rate. Pre-filled from provider preset.")
                with c5:
                    min_pmt_input = st.number_input("Min payment (% of balance)",
                                                    min_value=0.0, max_value=100.0, step=1.0,
                                                    value=float(preset["min_pmt_pct"]),
                                                    help="0 for installment apps with fixed payment plans")
                with c6:
                    due_day_input = st.number_input("Statement due day (1-28)",
                                                    min_value=0, max_value=28, step=1, value=0,
                                                    help="Day of month payment is due. Set 0 if not applicable.")

                notes_input = st.text_input("Notes (optional)",
                                            placeholder="e.g. 'Cashback 1%, no FX fee, expires 12/2027'")

                submitted = st.form_submit_button("➕ Add Account", type="primary",
                                                  use_container_width=True)
                if submitted:
                    final_provider = (custom_name.strip() if provider_choice == "Other / Custom"
                                      else provider_choice)
                    if not final_provider:
                        st.error("Provider name is required.")
                    elif limit_input <= 0:
                        st.error("Credit limit must be greater than 0.")
                    else:
                        accounts.append({
                            "id":            f"acc_{int(time.time() * 1000)}",
                            "provider":      final_provider,
                            "nickname":      nickname.strip(),
                            "kind":          kind_choice,
                            "currency":      currency_choice,
                            "limit":         float(limit_input),
                            "balance":       float(balance_input),
                            "apr":           float(apr_input),
                            "min_pmt_pct":   float(min_pmt_input),
                            "due_day":       int(due_day_input) if due_day_input else None,
                            "notes":         notes_input.strip(),
                            "color":         preset["color"],
                            "added_on":      today_dt.isoformat(),
                        })
                        save_data(data)
                        st.success(f"✅ Added {final_provider}!")
                        st.rerun()

        if not accounts:
            st.markdown(
                "<div style='background:var(--bg-surface);border:1px dashed var(--border-2);"
                "border-radius:10px;padding:20px;text-align:center;color:var(--text-muted);font-size:0.9rem;"
                "margin-top:14px;'>"
                "📭 No accounts added yet. Use the form above to add your first card or installment program."
                "</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown("### Your accounts")
            # Sort: credit cards first, then installment apps
            sorted_accounts = sorted(accounts, key=lambda a: (a.get("kind") != "credit_card", a["provider"]))
            for acc in sorted_accounts:
                acc_limit = float(acc.get("limit", 0) or 0)
                acc_bal   = float(acc.get("balance", 0) or 0)
                acc_util  = (acc_bal / acc_limit * 100) if acc_limit > 0 else 0
                avail     = max(0, acc_limit - acc_bal)
                kind_label = "💳 Card" if acc.get("kind") == "credit_card" else "📲 Installment"
                util_color = ("#22c55e" if acc_util <= 30 else
                              "#f59e0b" if acc_util <= 60 else "#ef4444")

                cols = st.columns([4, 2, 2, 2, 1])
                with cols[0]:
                    display_name = acc["provider"]
                    if acc.get("nickname"):
                        display_name += f" · {acc['nickname']}"
                    st.markdown(
                        f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                        f"border-left:4px solid {acc.get('color', '#475569')};border-radius:10px;"
                        f"padding:10px 14px;'>"
                        f"<b>{display_name}</b><br>"
                        f"<span style='color:var(--text-muted);font-size:0.78rem;'>"
                        f"{kind_label} · APR {acc.get('apr', 0):.1f}%"
                        f"{' · Due day ' + str(acc['due_day']) if acc.get('due_day') else ''}"
                        f"</span></div>",
                        unsafe_allow_html=True,
                    )
                with cols[1]:
                    st.markdown(
                        f"<div style='padding:8px 0;text-align:center;'>"
                        f"<div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;'>Balance</div>"
                        f"<div style='color:var(--text);font-family:JetBrains Mono,monospace;font-weight:700;'>"
                        f"{acc_bal:,.0f} {acc.get('currency', 'EGP')}</div></div>",
                        unsafe_allow_html=True,
                    )
                with cols[2]:
                    st.markdown(
                        f"<div style='padding:8px 0;text-align:center;'>"
                        f"<div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;'>Available</div>"
                        f"<div style='color:#22c55e;font-family:JetBrains Mono,monospace;font-weight:700;'>"
                        f"{avail:,.0f} {acc.get('currency', 'EGP')}</div></div>",
                        unsafe_allow_html=True,
                    )
                with cols[3]:
                    st.markdown(
                        f"<div style='padding:8px 0;text-align:center;'>"
                        f"<div style='color:var(--text-dim);font-size:0.7rem;text-transform:uppercase;'>Utilization</div>"
                        f"<div style='color:{util_color};font-family:JetBrains Mono,monospace;font-weight:700;'>"
                        f"{acc_util:.1f}%</div></div>",
                        unsafe_allow_html=True,
                    )
                with cols[4]:
                    if st.button("🗑️", key=f"acc_del_{acc['id']}", help="Remove this account"):
                        credit["accounts"] = [a for a in accounts if a["id"] != acc["id"]]
                        save_data(data)
                        st.rerun()

                # Quick balance update inline
                with st.expander(f"  ↳ Update balance for {acc['provider']}", expanded=False):
                    upd_c1, upd_c2 = st.columns([3, 1])
                    with upd_c1:
                        new_bal = st.number_input(
                            "New current balance",
                            min_value=0.0, step=100.0,
                            value=float(acc.get("balance", 0)),
                            key=f"acc_balupd_{acc['id']}",
                        )
                    with upd_c2:
                        st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
                        if st.button("Save", key=f"acc_save_{acc['id']}", use_container_width=True):
                            for a in accounts:
                                if a["id"] == acc["id"]:
                                    a["balance"] = float(new_bal)
                                    break
                            save_data(data)
                            st.success("Balance updated.")
                            st.rerun()

    st.markdown("---")
    st.markdown("### 🏛️ Detailed QNB & EGBank Tracking")
    st.caption("Full transaction-level tracking with statement cycles, revolving interest, and installment plans for QNB and EGBank cards. For other banks/programs, use the **🏦 My Accounts** tab above.")

    # ── Bank Rate Configs ──
    BANKS = {
        "QNB": {
            "monthly_rate": 0.0435,       # 4.35%/month revolving
            "annual_rate": 0.522,
            "min_pmt_pct": 0.05,
            "cash_adv_fee": 0.04,
            "grace_days": 57,
            "install_rates": {6: 0.0218, 12: 0.0218, 18: 0.0199, 24: 0.0181, 36: 0.0181},
            "color": "#3b82f6",
            "late_fee": 200
        },
        "EGBank": {
            "monthly_rate": 0.03,         # 3%/month revolving
            "annual_rate": 0.36,
            "min_pmt_pct": 0.05,
            "cash_adv_fee": 0.03,
            "grace_days": 57,
            "install_rates": {6: 0.03, 12: 0.03, 18: 0.03, 24: 0.03, 36: 0.03},
            "color": "#22c55e",
            "late_fee": 150
        }
    }

    def safe_float(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0

    transactions   = credit.get("transactions", [])
    installment_plans = credit.get("installment_plans", [])
    limits  = credit.get("limits",   {"QNB": 0, "EGBank": 0})
    balances_init = credit.get("balances", {"QNB": 0, "EGBank": 0})  # starting manual balance override

    # ── Per-bank calculations ──
    def calc_bank(bank_name):
        cfg = BANKS[bank_name]
        bank_tx   = [t for t in transactions if t.get("bank", "QNB") == bank_name]
        bank_plan = [p for p in installment_plans if p.get("bank", "QNB") == bank_name]

        spent      = sum(safe_float(t.get("amount")) for t in bank_tx if t.get("type") == "Purchase")
        paid       = sum(safe_float(t.get("amount")) for t in bank_tx if t.get("type") == "Payment")
        cash_adv   = sum(safe_float(t.get("amount")) for t in bank_tx if t.get("type") == "Cash Advance")
        refunds    = sum(safe_float(t.get("amount")) for t in bank_tx if t.get("type") == "Refund")
        install_covered = sum(safe_float(p.get("purchase_amount")) for p in bank_plan)

        revolving = max(0, spent + cash_adv - install_covered - paid - refunds)
        interest  = revolving * cfg["monthly_rate"]
        cash_fee  = cash_adv * cfg["cash_adv_fee"]

        # Monthly installment dues
        monthly_install = 0
        for plan in bank_plan:
            start = plan.get("start_month", "")
            months = int(safe_float(plan.get("months", 0)))
            monthly_amt = safe_float(plan.get("monthly_amount", 0))
            if start and months > 0:
                try:
                    start_dt = datetime.datetime.strptime(start, "%Y-%m")
                    elapsed = (today_dt.year - start_dt.year)*12 + (today_dt.month - start_dt.month)
                    if 0 <= elapsed < months:
                        monthly_install += monthly_amt
                except: pass

        total_due = revolving + interest + monthly_install
        min_pmt   = max(revolving * cfg["min_pmt_pct"] + monthly_install, 100)
        limit     = safe_float(limits.get(bank_name, 0))
        available = max(0, limit - revolving - monthly_install) if limit > 0 else 0
        utilization = (revolving / limit * 100) if limit > 0 else 0

        return {
            "spent": spent, "paid": paid, "cash_adv": cash_adv, "revolving": revolving,
            "interest": interest, "cash_fee": cash_fee, "monthly_install": monthly_install,
            "total_due": total_due, "min_pmt": min_pmt, "limit": limit,
            "available": available, "utilization": utilization,
            "tx": bank_tx, "plans": bank_plan, "cfg": cfg
        }

    qnb   = calc_bank("QNB")
    egbnk = calc_bank("EGBank")

    # ── CREDIT LIMITS SETUP ──
    with st.expander("⚙️ Credit Card Limits & Setup", expanded=False):
        lc1, lc2 = st.columns(2)
        with lc1:
            st.markdown("**QNB Credit Limit**")
            new_qnb_limit = st.number_input("QNB Credit Limit (EGP)", min_value=0.0,
                value=safe_float(limits.get("QNB", 0)), step=1000.0, key="qnb_limit_input")
        with lc2:
            st.markdown("**EGBank Credit Limit**")
            new_eg_limit = st.number_input("EGBank Credit Limit (EGP)", min_value=0.0,
                value=safe_float(limits.get("EGBank", 0)), step=1000.0, key="eg_limit_input")
        if st.button("💾 Save Limits"):
            data["credit"]["limits"] = {"QNB": new_qnb_limit, "EGBank": new_eg_limit}
            save_data(data)
            st.success("✅ Limits saved!")
            st.rerun()

    st.divider()

    # ── TOP SUMMARY — BOTH BANKS ──
    st.markdown("### 📊 Summary — Both Cards")
    bank_cols = st.columns(2)

    for col_idx, (bname, bdata) in enumerate(zip(["QNB", "EGBank"], [qnb, egbnk])):
        cfg = BANKS[bname]
        with bank_cols[col_idx]:
            util_color = "#22c55e" if bdata["utilization"] < 30 else "#facc15" if bdata["utilization"] < 70 else "#ef4444"
            st.markdown(f"""
            <div style='background:#0d1b2a; border:2px solid {cfg["color"]}44; border-radius:14px; padding:18px 20px; margin-bottom:8px;'>
                <div style='color:{cfg["color"]}; font-weight:700; font-size:1rem; margin-bottom:12px;'>💳 {bname}</div>
                <div style='display:grid; grid-template-columns:1fr 1fr; gap:10px;'>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Revolving Balance</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.3rem; color:#ef4444; font-weight:700;'>{bdata["revolving"]:,.0f} EGP</div></div>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Monthly Interest</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.3rem; color:#fb923c;'>{bdata["interest"]:,.0f} EGP</div></div>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Installments Due</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.1rem; color:#a78bfa;'>{bdata["monthly_install"]:,.0f} EGP</div></div>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Total Due</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.1rem; color:#e2e8f0; font-weight:700;'>{bdata["total_due"]:,.0f} EGP</div></div>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Available Credit</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.1rem; color:#22c55e;'>{bdata["available"]:,.0f} EGP</div></div>
                    <div><div style='color:#475569; font-size:0.7rem; text-transform:uppercase;'>Min Payment</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.1rem; color:#facc15;'>{bdata["min_pmt"]:,.0f} EGP</div></div>
                </div>
                {'<div style="margin-top:10px;"><div style="background:#1e293b;border-radius:4px;height:6px;"><div style="background:' + util_color + ';width:' + str(min(bdata["utilization"],100)) + '%;height:6px;border-radius:4px;"></div></div><div style="color:#475569;font-size:0.7rem;margin-top:2px;">Credit Used: ' + f'{bdata["utilization"]:.1f}%' + '</div></div>' if bdata["limit"] > 0 else ""}
                <div style='margin-top:8px;color:#334155;font-size:0.72rem;'>
                    Rate: {cfg["monthly_rate"]*100:.2f}%/mo ({cfg["annual_rate"]*100:.1f}%/yr) · Cash Adv: {cfg["cash_adv_fee"]*100:.0f}% · Grace: {cfg["grace_days"]}d
                </div>
            </div>
            """, unsafe_allow_html=True)

    if qnb["revolving"] > 5000:
        st.warning(f"⚠️ QNB revolving balance {qnb['revolving']:,.0f} EGP → {qnb['interest']:,.0f} EGP/month interest (4.35%/mo, ~52.2% annual)")
    if egbnk["revolving"] > 5000:
        st.warning(f"⚠️ EGBank revolving balance {egbnk['revolving']:,.0f} EGP → {egbnk['interest']:,.0f} EGP/month interest (3%/mo, ~36% annual)")

    st.divider()

    tab_tx, tab_install, tab_summary, tab_calc = st.tabs(["💸 Transactions", "📋 Installment Plans", "📊 Monthly Statement", "🧮 Calculator"])

    # ── TAB 1: TRANSACTIONS (monthly scope) ──
    with tab_tx:
        credit_month_str = today_dt.strftime("%Y-%m")
        all_txns = credit.get("transactions", [])
        month_txns  = [t for t in all_txns if str(t.get("date","")).startswith(credit_month_str)]
        other_txns  = [t for t in all_txns if not str(t.get("date","")).startswith(credit_month_str)]

        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>"
            f"<h3 style='margin:0;'>💳 Transactions</h3>"
            f"<span style='color:#475569;font-size:0.8rem;'>Showing: {today_dt.strftime('%B %Y')} only · "
            f"All-time: {len(all_txns)} transactions</span></div>", unsafe_allow_html=True)
        st.caption("Select bank per transaction. Transactions reset each month view but all history is kept.")

        df_tx = pd.DataFrame(month_txns) if month_txns else pd.DataFrame(
            columns=["date", "bank", "description", "category", "type", "amount"])

        for col_name, default in [
            ("date", today_dt.strftime("%Y-%m-%d")), ("bank", "QNB"),
            ("description", ""), ("category", "Shopping"), ("type", "Purchase"), ("amount", 0)
        ]:
            if col_name not in df_tx.columns:
                df_tx[col_name] = default

        ed_tx = st.data_editor(
            df_tx.assign(date=pd.to_datetime(df_tx["date"], errors="coerce")),
            num_rows="dynamic",
            column_config={
                "date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "bank": st.column_config.SelectboxColumn("Bank", options=["QNB", "EGBank"], width="small"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "category": st.column_config.SelectboxColumn("Category",
                    options=["Shopping", "Food & Dining", "Travel", "Fuel", "Medical",
                             "Subscriptions", "Electronics", "Cash Advance", "Business", "Other"]),
                "type": st.column_config.SelectboxColumn("Type",
                    options=["Purchase", "Payment", "Cash Advance", "Refund"]),
                "amount": st.column_config.NumberColumn("Amount (EGP)", format="%.2f", min_value=0),
            },
            key="ed_credit_tx", use_container_width=True)

        if st.button("💾 Save Transactions", key="save_tx"):
            records = ed_tx.to_dict("records")
            for r in records:
                if "date" in r and not isinstance(r["date"], str):
                    r["date"] = str(r["date"])
            # Merge: keep other months + updated current month
            data["credit"]["transactions"] = other_txns + records
            save_data(data)
            st.success("✅ Saved!")
            st.rerun()

        # Month summary cards
        if not ed_tx.empty:
            this_m = ed_tx.to_dict("records")
            m_purchase = sum(safe_float(t.get("amount",0)) for t in this_m if t.get("type")=="Purchase")
            m_payment  = sum(safe_float(t.get("amount",0)) for t in this_m if t.get("type")=="Payment")
            m_cash     = sum(safe_float(t.get("amount",0)) for t in this_m if t.get("type")=="Cash Advance")
            sc1,sc2,sc3 = st.columns(3)
            sc1.metric("Purchases", f"{m_purchase:,.0f} EGP")
            sc2.metric("Payments Made", f"{m_payment:,.0f} EGP")
            sc3.metric("Cash Advances", f"{m_cash:,.0f} EGP")

        # Spending chart split by bank
        if len(df_tx) > 0:
            st.divider()
            chart_c1, chart_c2 = st.columns(2)
            for cidx, bname in enumerate(["QNB", "EGBank"]):
                with [chart_c1, chart_c2][cidx]:
                    st.markdown(f"**{bname} Spending by Category**")
                    b_purchases = df_tx[(df_tx.get("bank", "QNB") == bname if "bank" in df_tx.columns else True) & (df_tx["type"] == "Purchase")].copy()
                    if not b_purchases.empty:
                        b_purchases["amount"] = b_purchases["amount"].apply(safe_float)
                        cat_s = b_purchases.groupby("category")["amount"].sum().reset_index()
                        fig_c = go.Figure(go.Pie(labels=cat_s["category"], values=cat_s["amount"], hole=0.55,
                            marker=dict(colors=["#3b82f6","#22c55e","#f59e0b","#ef4444","#a78bfa","#fb923c","#06b6d4","#ec4899","#84cc16"])))
                        fig_c.update_layout(height=250, paper_bgcolor="#080c14", font=dict(color="#94a3b8"),
                            legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=0,r=0,t=10,b=0))
                        st.plotly_chart(fig_c, use_container_width=True)

    # ── TAB 2: INSTALLMENT PLANS ──
    with tab_install:
        st.subheader("Installment Plans (تقسيط)")

        df_plans = pd.DataFrame(installment_plans) if installment_plans else pd.DataFrame(
            columns=["bank","name","purchase_amount","months","monthly_amount","interest_rate","start_month","paid_months"])
        for col_name, default in [
            ("bank","QNB"),("name",""),("purchase_amount",0),("months",12),
            ("monthly_amount",0),("interest_rate",0),("start_month",today_dt.strftime("%Y-%m")),("paid_months",0)
        ]:
            if col_name not in df_plans.columns:
                df_plans[col_name] = default

        ed_plans = st.data_editor(df_plans, num_rows="dynamic",
            column_config={
                "bank": st.column_config.SelectboxColumn("Bank", options=["QNB","EGBank"], width="small"),
                "name": st.column_config.TextColumn("Plan Name", width="medium"),
                "purchase_amount": st.column_config.NumberColumn("Purchase (EGP)", format="%.0f"),
                "months": st.column_config.NumberColumn("Total Months", min_value=1, max_value=60),
                "monthly_amount": st.column_config.NumberColumn("Monthly (EGP)", format="%.2f"),
                "interest_rate": st.column_config.NumberColumn("Rate %", format="%.2f"),
                "start_month": st.column_config.TextColumn("Start (YYYY-MM)"),
                "paid_months": st.column_config.NumberColumn("Paid Months", min_value=0),
            }, key="ed_plans", use_container_width=True)

        if st.button("💾 Save Plans", key="save_plans"):
            data["credit"]["installment_plans"] = ed_plans.to_dict("records")
            save_data(data)
            st.success("✅ Plans saved!")
            st.rerun()

        if not ed_plans.empty:
            st.divider()
            for _, plan in ed_plans.iterrows():
                name = plan.get("name","Plan")
                bname = plan.get("bank","QNB")
                total_months = int(safe_float(plan.get("months",0)))
                paid = int(safe_float(plan.get("paid_months",0)))
                monthly = safe_float(plan.get("monthly_amount",0))
                purchase = safe_float(plan.get("purchase_amount",0))
                remaining = max(0, total_months - paid)
                remaining_amt = remaining * monthly
                pct = (paid / total_months * 100) if total_months > 0 else 0
                bcolor = BANKS.get(bname, BANKS["QNB"])["color"]
                if name and total_months > 0:
                    status_c = "#22c55e" if remaining == 0 else bcolor
                    st.markdown(f"""
                    <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:12px; padding:14px 18px; margin-bottom:10px;'>
                        <div style='display:flex; justify-content:space-between; margin-bottom:8px;'>
                            <span style='color:#e2e8f0; font-weight:600;'>📦 {name}</span>
                            <span style='background:{bcolor}22; color:{bcolor}; border-radius:999px; padding:2px 10px; font-size:0.75rem;'>{bname}</span>
                        </div>
                        <div style='display:flex; gap:20px; flex-wrap:wrap; margin-bottom:8px;'>
                            <span style='color:#94a3b8; font-size:0.82rem;'>Original: <b style="font-family:JetBrains Mono,monospace;">{purchase:,.0f} EGP</b></span>
                            <span style='color:{bcolor}; font-size:0.82rem;'>Monthly: <b style="font-family:JetBrains Mono,monospace;">{monthly:,.2f} EGP</b></span>
                            <span style='color:#fb923c; font-size:0.82rem;'>Remaining: <b style="font-family:JetBrains Mono,monospace;">{remaining_amt:,.0f} EGP</b></span>
                            <span style='color:#a78bfa; font-size:0.82rem;'>{paid}/{total_months} months</span>
                        </div>
                        <div style='background:#1e293b; border-radius:4px; height:6px;'>
                            <div style='background:{status_c}; width:{min(pct,100):.0f}%; height:6px; border-radius:4px;'></div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

    # ── TAB 3: MONTHLY STATEMENT ──
    with tab_summary:
        st.subheader(f"Monthly Statement — {today_dt.strftime('%B %Y')}")
        stmt_bank = st.radio("Select Bank", ["QNB", "EGBank", "Combined"], horizontal=True)

        def render_statement(bname, bdata):
            cfg = BANKS.get(bname, BANKS["QNB"]) if bname != "Combined" else None
            this_month_tx = [t for t in (transactions if bname == "Combined" else bdata["tx"])
                             if str(t.get("date","")).startswith(current_month_str)]
            m_purchase = sum(safe_float(t.get("amount")) for t in this_month_tx if t.get("type") == "Purchase")
            m_payment  = sum(safe_float(t.get("amount")) for t in this_month_tx if t.get("type") == "Payment")
            m_cash     = sum(safe_float(t.get("amount")) for t in this_month_tx if t.get("type") == "Cash Advance")
            cash_fee   = m_cash * (cfg["cash_adv_fee"] if cfg else 0.04)
            revolving  = bdata["revolving"] if bname != "Combined" else qnb["revolving"] + egbnk["revolving"]
            interest   = bdata["interest"]  if bname != "Combined" else qnb["interest"] + egbnk["interest"]
            install    = bdata["monthly_install"] if bname != "Combined" else qnb["monthly_install"] + egbnk["monthly_install"]
            total      = revolving + interest + install
            min_p      = bdata["min_pmt"] if bname != "Combined" else qnb["min_pmt"] + egbnk["min_pmt"]
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:16px; padding:22px; margin-bottom:14px;'>
                <div style='color:#60a5fa; font-size:0.78rem; letter-spacing:0.1em; text-transform:uppercase; margin-bottom:14px;'>
                    💳 {bname} Statement — {today_dt.strftime('%B %Y')}
                </div>
                <div style='display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px;'>
                    <div><div style='color:#64748b; font-size:0.72rem;'>New Purchases</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#e2e8f0;'>{m_purchase:,.2f} EGP</div></div>
                    <div><div style='color:#64748b; font-size:0.72rem;'>Payments Made</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#22c55e;'>{m_payment:,.2f} EGP</div></div>
                    <div><div style='color:#64748b; font-size:0.72rem;'>Cash Advances</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#fb923c;'>{m_cash:,.2f} EGP</div></div>
                    <div><div style='color:#64748b; font-size:0.72rem;'>Cash Advance Fee</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#ef4444;'>{cash_fee:,.2f} EGP</div></div>
                    <div><div style='color:#64748b; font-size:0.72rem;'>Installments Due</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#a78bfa;'>{install:,.2f} EGP</div></div>
                    <div><div style='color:#64748b; font-size:0.72rem;'>Interest on Revolving</div>
                         <div style='font-family:JetBrains Mono,monospace; color:#ef4444;'>{interest:,.2f} EGP</div></div>
                </div>
                <div style='border-top:1px solid #1e293b; margin-top:14px; padding-top:14px; display:flex; justify-content:space-between;'>
                    <div><div style='color:#64748b; font-size:0.72rem;'>TOTAL DUE</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.8rem; font-weight:700; color:#ef4444;'>{total:,.2f} EGP</div></div>
                    <div style='text-align:right;'><div style='color:#64748b; font-size:0.72rem;'>MIN PAYMENT</div>
                         <div style='font-family:JetBrains Mono,monospace; font-size:1.2rem; color:#fb923c;'>{min_p:,.2f} EGP</div></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        if stmt_bank == "QNB":
            render_statement("QNB", qnb)
            st.markdown("""<div class='insight-card'>ℹ️ <b>QNB:</b> 4.35%/mo (~52.2%/yr) · Cash Advance 4% · Grace 57 days · Min payment 5%</div>""", unsafe_allow_html=True)
        elif stmt_bank == "EGBank":
            render_statement("EGBank", egbnk)
            st.markdown("""<div class='insight-card'>ℹ️ <b>EGBank:</b> 3%/mo (~36%/yr) · Cash Advance 3% · Grace 57 days · Secured card option available</div>""", unsafe_allow_html=True)
        else:
            render_statement("Combined", None)

    # ── TAB 4: CALCULATOR ──
    with tab_calc:
        st.subheader("🧮 Installment Plan Calculator")
        calc_bank_sel = st.radio("Calculate for Bank", ["QNB", "EGBank"], horizontal=True)
        cfg_calc = BANKS[calc_bank_sel]

        st.markdown(f"""
        <div class='insight-card' style='margin-bottom:10px;'>
            📋 <b>{calc_bank_sel} Installment Rates:</b>
            {' | '.join([f'{m}m → {r*100:.2f}%/mo' for m,r in cfg_calc["install_rates"].items()])}
        </div>""", unsafe_allow_html=True)

        cc1, cc2 = st.columns(2)
        with cc1:
            purchase_amt = st.number_input("Purchase Amount (EGP)", min_value=0.0, step=100.0, value=10000.0, key="calc_purchase")
            num_months = st.selectbox("Months", [3,6,9,12,18,24,36], index=3, key="calc_months")
            auto_rate = cfg_calc["install_rates"].get(num_months, cfg_calc["monthly_rate"]) * 100
            use_auto = st.checkbox(f"Use {calc_bank_sel} standard rate for {num_months}m ({auto_rate:.2f}%/mo)", value=True)
            monthly_rate_pct = auto_rate if use_auto else st.number_input("Custom Rate (%/mo)", min_value=0.0, max_value=10.0, value=auto_rate, step=0.01)
            admin_fee_pct = st.number_input("One-time Admin Fee (%)", min_value=0.0, max_value=10.0, value=0.0, step=0.1)

        monthly_rate_calc = monthly_rate_pct / 100
        admin_fee = purchase_amt * (admin_fee_pct / 100)
        total_financed = purchase_amt + admin_fee
        if monthly_rate_calc > 0:
            monthly_pmt = total_financed * (monthly_rate_calc * (1 + monthly_rate_calc)**num_months) / ((1 + monthly_rate_calc)**num_months - 1)
        else:
            monthly_pmt = total_financed / num_months
        total_paid_calc = monthly_pmt * num_months
        total_interest = total_paid_calc - purchase_amt

        with cc2:
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:14px; padding:20px; margin-top:24px;'>
                <div style='color:#60a5fa; font-size:0.78rem; letter-spacing:0.1em; text-transform:uppercase; margin-bottom:12px;'>{calc_bank_sel} Calculation</div>
                <div style='font-family:JetBrains Mono,monospace; font-size:2rem; font-weight:700; color:#22c55e; margin-bottom:12px;'>{monthly_pmt:,.2f} EGP/mo</div>
                <div style='display:grid; grid-template-columns:1fr 1fr; gap:8px; font-size:0.82rem;'>
                    <div><div style='color:#475569;'>Total You Pay</div><div style='color:#e2e8f0; font-family:JetBrains Mono,monospace;'>{total_paid_calc:,.2f}</div></div>
                    <div><div style='color:#475569;'>Total Interest+Fees</div><div style='color:#ef4444; font-family:JetBrains Mono,monospace;'>{total_interest:,.2f}</div></div>
                    <div><div style='color:#475569;'>Admin Fee</div><div style='color:#fb923c; font-family:JetBrains Mono,monospace;'>{admin_fee:,.2f}</div></div>
                    <div><div style='color:#475569;'>Duration</div><div style='color:#a78bfa; font-family:JetBrains Mono,monospace;'>{num_months} months</div></div>
                </div>
            </div>""", unsafe_allow_html=True)

        # Amortization
        st.divider()
        sched = []
        bal = total_financed
        for m in range(1, num_months + 1):
            int_part = bal * monthly_rate_calc
            prin_part = monthly_pmt - int_part
            bal = max(0, bal - prin_part)
            pay_dt = (today_dt.replace(day=1) + datetime.timedelta(days=32*m))
            sched.append({"Month": pay_dt.strftime("%b %Y"), "Payment": f"{monthly_pmt:,.2f}",
                          "Interest": f"{int_part:,.2f}", "Principal": f"{prin_part:,.2f}", "Remaining": f"{bal:,.2f}"})
        st.dataframe(pd.DataFrame(sched), use_container_width=True, hide_index=True)

        plan_name_input = st.text_input("Save this plan as:", placeholder="e.g. New Laptop")
        if st.button("➕ Add to My Plans") and plan_name_input:
            new_plan = {"bank": calc_bank_sel, "name": plan_name_input, "purchase_amount": purchase_amt,
                        "months": num_months, "monthly_amount": round(monthly_pmt, 2),
                        "interest_rate": monthly_rate_pct, "start_month": today_dt.strftime("%Y-%m"), "paid_months": 0}
            data["credit"]["installment_plans"].append(new_plan)
            save_data(data)
            st.success(f"✅ '{plan_name_input}' added to {calc_bank_sel} plans!")
            st.rerun()



# ==========================================
# GYM TRACKER PAGE
# ==========================================
elif st.session_state['page'] == 'Gym':
    st.title("🏋️ Gym Tracker")

    gym = data["gym"]
    today_iso = datetime.date.today().strftime("%Y-%m-%d")

    def safe_float(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0

    # ── SUMMARY METRICS ──
    sessions = gym.get("sessions", [])
    total_sessions = len(sessions)
    this_month_str = datetime.date.today().strftime("%Y-%m")
    month_sessions = [s for s in sessions if str(s.get("date", "")).startswith(this_month_str)]
    current_streak_gym = 0
    for i in range(30):
        chk = (datetime.date.today() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        if any(s.get("date") == chk for s in sessions):
            current_streak_gym += 1
        elif i > 0:
            break

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sessions", total_sessions)
    c2.metric("This Month", len(month_sessions))
    c3.metric("Current Streak", f"{current_streak_gym} 🔥")
    avg_duration = sum(safe_float(s.get("duration_min", 0)) for s in sessions) / max(len(sessions), 1)
    c4.metric("Avg Duration", f"{avg_duration:.0f} min")

    st.divider()

    tab_log, tab_plan, tab_habits, tab_progress = st.tabs(["📝 Log Session", "💪 Workout Planner", "🥗 Health Habits", "📊 Progress"])

    # ── TAB 1: LOG SESSION ──
    with tab_log:
        st.subheader(f"Log Workout — {selected_date.strftime('%A, %b %d')}")

        # Check if session exists for selected date
        existing_session = next((s for s in sessions if s.get("date") == current_day_str), None)

        with st.form("session_form"):
            lc1, lc2 = st.columns(2)
            with lc1:
                muscle_groups = st.multiselect("Muscle Groups Trained",
                    ["Chest", "Back", "Shoulders", "Biceps", "Triceps", "Legs", "Glutes",
                     "Core/Abs", "Cardio", "Full Body", "Rest Day"],
                    default=existing_session.get("muscle_groups", []) if existing_session else [])
                duration = st.number_input("Duration (minutes)", min_value=0, max_value=300,
                    value=int(safe_float(existing_session.get("duration_min", 60))) if existing_session else 60)
                workout_type = st.selectbox("Session Type",
                    ["Strength", "Hypertrophy", "Cardio", "HIIT", "Mobility/Stretch", "Rest"],
                    index=["Strength", "Hypertrophy", "Cardio", "HIIT", "Mobility/Stretch", "Rest"].index(
                        existing_session.get("type", "Strength")) if existing_session else 0)
            with lc2:
                energy_level = st.select_slider("Energy Level", options=[1,2,3,4,5],
                    value=int(safe_float(existing_session.get("energy", 3))) if existing_session else 3,
                    format_func=lambda x: ["💀 Dead","😴 Low","⚡ OK","🚀 Good","🌟 Beast"][x-1])
                session_rating = st.select_slider("Session Rating", options=[1,2,3,4,5],
                    value=int(safe_float(existing_session.get("rating", 3))) if existing_session else 3,
                    format_func=lambda x: ["😞 Terrible","😕 Bad","😐 OK","😊 Good","🔥 Fire"][x-1])
                body_weight = st.number_input("Body Weight (kg)", min_value=0.0, max_value=250.0,
                    value=safe_float(existing_session.get("body_weight", 0)) if existing_session else 0.0, step=0.1)
            notes_gym = st.text_area("Session Notes / PRs / Observations",
                value=existing_session.get("notes", "") if existing_session else "", height=80)

            submitted = st.form_submit_button("💾 Save Session", use_container_width=True)
            if submitted:
                new_session = {
                    "date": current_day_str,
                    "muscle_groups": muscle_groups,
                    "duration_min": duration,
                    "type": workout_type,
                    "energy": energy_level,
                    "rating": session_rating,
                    "body_weight": body_weight,
                    "notes": notes_gym
                }
                # Update or insert
                sessions_clean = [s for s in sessions if s.get("date") != current_day_str]
                sessions_clean.append(new_session)
                data["gym"]["sessions"] = sessions_clean
                save_data(data)
                st.success("✅ Session saved!")
                st.rerun()

        # Show existing exercises for today
        if existing_session:
            st.divider()
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #22c55e; border-radius:12px; padding:14px 18px;'>
                <div style='color:#22c55e; font-size:0.8rem; font-weight:600; margin-bottom:6px;'>✅ SESSION LOGGED</div>
                <div style='display:flex; gap:20px; flex-wrap:wrap;'>
                    <span style='color:#94a3b8; font-size:0.85rem;'>⏱ {existing_session.get('duration_min',0)} min</span>
                    <span style='color:#94a3b8; font-size:0.85rem;'>💪 {', '.join(existing_session.get('muscle_groups',[]))}</span>
                    <span style='color:#94a3b8; font-size:0.85rem;'>⚡ {existing_session.get('type','')}</span>
                    <span style='color:#94a3b8; font-size:0.85rem;'>⚖️ {existing_session.get('body_weight',0)} kg</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

    # ── TAB 2: WORKOUT PLANNER ──
    with tab_plan:
        st.subheader("💪 Weekly Workout Plan & Exercise Tracker")
        st.caption("Plan your exercises, sets, reps, and weights for each day.")

        workouts = gym.get("workouts", [])

        # Day selector
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        selected_day_plan = st.selectbox("Select Day to Plan", day_names, key="plan_day")

        day_workouts = [w for w in workouts if w.get("day") == selected_day_plan]

        df_workout = pd.DataFrame(day_workouts) if day_workouts else pd.DataFrame(
            columns=["exercise", "sets", "reps", "weight_kg", "rest_sec", "notes"])

        for col, default in [("exercise",""), ("sets",3), ("reps",10), ("weight_kg",0), ("rest_sec",60), ("notes","")]:
            if col not in df_workout.columns:
                df_workout[col] = default

        # ── AI PLAN GENERATOR ──
        with st.expander("🤖 Generate AI Workout Plan for this Day", expanded=False):
            st.markdown("<p style='color:#a78bfa; font-size:0.85rem;'>Tell the AI what you want to train and it will build a full plan for you.</p>", unsafe_allow_html=True)

            ai_col1, ai_col2 = st.columns(2)
            with ai_col1:
                ai_muscle_groups = st.multiselect("Target Muscle Groups",
                    ["Chest", "Back", "Shoulders", "Biceps", "Triceps", "Legs", "Glutes", "Core/Abs", "Full Body", "Cardio"],
                    key="ai_muscles")
                ai_goal = st.selectbox("Training Goal",
                    ["Hypertrophy (Muscle Size)", "Strength (Heavy Weight)", "Endurance/Cardio",
                     "Weight Loss (Fat Burn)", "Mobility & Flexibility", "Beginner General Fitness"],
                    key="ai_goal")
                ai_level = st.selectbox("My Fitness Level",
                    ["Beginner", "Intermediate", "Advanced"], index=1, key="ai_level")
            with ai_col2:
                ai_duration = st.number_input("Available Time (minutes)", min_value=20, max_value=180, value=60, key="ai_duration")
                ai_equipment = st.multiselect("Available Equipment",
                    ["Barbell", "Dumbbells", "Cable Machine", "Smith Machine", "Pull-up Bar",
                     "Resistance Bands", "Leg Press", "Lat Pulldown", "Treadmill", "Full Gym"],
                    default=["Full Gym"], key="ai_equipment")
                ai_notes_user = st.text_area("Special Notes / Injuries / Preferences",
                    placeholder="e.g. bad left knee, focus on upper chest, no overhead press...",
                    height=70, key="ai_extra_notes")

            generate_btn = st.button("✨ Generate AI Plan", type="primary", key="gen_ai_plan", use_container_width=True)

            if generate_btn:
                if not ai_muscle_groups:
                    st.warning("Please select at least one muscle group.")
                else:
                    prompt = f"""You are an expert personal trainer. Create a detailed gym workout plan for ONE training session.

Training Details:
- Day: {selected_day_plan}
- Target Muscles: {', '.join(ai_muscle_groups)}
- Goal: {ai_goal}
- Level: {ai_level}
- Duration: {ai_duration} minutes
- Equipment Available: {', '.join(ai_equipment) if ai_equipment else 'Full Gym'}
- Special Notes: {ai_notes_user if ai_notes_user else 'None'}

IMPORTANT: Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Each object must have EXACTLY these keys:
"exercise" (string), "sets" (integer), "reps" (string like "10" or "8-12"),
"weight_kg" (number, 0 for bodyweight), "rest_sec" (integer), "notes" (short string).

Include warm-up first and cool-down/stretches at end. Aim for {ai_duration} minutes."""

                    with st.spinner("🤖 Generating your workout plan..."):
                        ai_exercises = None
                        error_msg = ""

                        # ── Call Gemini for workout plan ──
                        import re as _re_gym
                        ai_exercises = None
                        error_msg = ""
                        gym_key = get_gemini_key()
                        if not gym_key:
                            error_msg = "No Gemini API key set. Add it in the sidebar. Showing built-in plan."
                        else:
                            try:
                                raw_gym = call_gemini(prompt, max_tokens=2000, temperature=0.7)
                                # Strip markdown fences
                                raw_gym = raw_gym.replace("```json","").replace("```","").strip()
                                # Extract the JSON array
                                match_gym = _re_gym.search(r'\[.*?\]', raw_gym, _re_gym.DOTALL)
                                if match_gym:
                                    raw_gym = match_gym.group()
                                ai_exercises = json.loads(raw_gym)
                                if not isinstance(ai_exercises, list):
                                    raise ValueError("Response was not a list")
                            except json.JSONDecodeError as je:
                                error_msg = f"Gemini returned non-JSON (parse error). Using built-in plan."
                                ai_exercises = None
                            except Exception as eg:
                                error_msg = f"Gemini error: {str(eg)[:100]}. Using built-in plan."
                                ai_exercises = None

                        # ── Rule-based fallback plan if both APIs fail ──
                        if not ai_exercises:
                            muscle_plans = {
                                "Chest": [
                                    {"exercise": "Warm-up: Light Cardio", "sets": 1, "reps": "5 min", "weight_kg": 0, "rest_sec": 0, "notes": "Get the blood flowing"},
                                    {"exercise": "Barbell Bench Press", "sets": 4, "reps": "8-10", "weight_kg": 60, "rest_sec": 90, "notes": "Control the descent"},
                                    {"exercise": "Incline Dumbbell Press", "sets": 3, "reps": "10-12", "weight_kg": 22, "rest_sec": 75, "notes": "Squeeze at top"},
                                    {"exercise": "Cable Fly", "sets": 3, "reps": "12-15", "weight_kg": 12, "rest_sec": 60, "notes": "Full stretch at bottom"},
                                    {"exercise": "Push-ups", "sets": 3, "reps": "AMRAP", "weight_kg": 0, "rest_sec": 60, "notes": "To failure"},
                                    {"exercise": "Cool-down Stretches", "sets": 1, "reps": "5 min", "weight_kg": 0, "rest_sec": 0, "notes": "Chest and shoulder stretches"},
                                ],
                                "Back": [
                                    {"exercise": "Warm-up: Band Pull-Aparts", "sets": 2, "reps": "15", "weight_kg": 0, "rest_sec": 30, "notes": "Activate rear delts"},
                                    {"exercise": "Deadlift", "sets": 4, "reps": "5-6", "weight_kg": 80, "rest_sec": 120, "notes": "Neutral spine"},
                                    {"exercise": "Lat Pulldown", "sets": 3, "reps": "10-12", "weight_kg": 55, "rest_sec": 75, "notes": "Pull to upper chest"},
                                    {"exercise": "Seated Cable Row", "sets": 3, "reps": "10-12", "weight_kg": 50, "rest_sec": 75, "notes": "Elbows close to body"},
                                    {"exercise": "Dumbbell Row", "sets": 3, "reps": "10", "weight_kg": 25, "rest_sec": 60, "notes": "One arm at a time"},
                                    {"exercise": "Cool-down Stretches", "sets": 1, "reps": "5 min", "weight_kg": 0, "rest_sec": 0, "notes": "Lats and lower back"},
                                ],
                                "Legs": [
                                    {"exercise": "Warm-up: Leg Swings & Hip Circles", "sets": 1, "reps": "5 min", "weight_kg": 0, "rest_sec": 0, "notes": "Open up the hips"},
                                    {"exercise": "Barbell Squat", "sets": 4, "reps": "8-10", "weight_kg": 70, "rest_sec": 120, "notes": "Below parallel"},
                                    {"exercise": "Leg Press", "sets": 3, "reps": "12", "weight_kg": 120, "rest_sec": 90, "notes": "Full range of motion"},
                                    {"exercise": "Romanian Deadlift", "sets": 3, "reps": "10-12", "weight_kg": 60, "rest_sec": 90, "notes": "Feel the hamstring stretch"},
                                    {"exercise": "Leg Curl", "sets": 3, "reps": "12-15", "weight_kg": 35, "rest_sec": 60, "notes": "Controlled eccentric"},
                                    {"exercise": "Calf Raises", "sets": 4, "reps": "15-20", "weight_kg": 0, "rest_sec": 45, "notes": "Full stretch at bottom"},
                                    {"exercise": "Cool-down Stretches", "sets": 1, "reps": "5 min", "weight_kg": 0, "rest_sec": 0, "notes": "Quad, hamstring, calf stretches"},
                                ],
                            }
                            primary = ai_muscle_groups[0] if ai_muscle_groups else "Chest"
                            ai_exercises = muscle_plans.get(primary, muscle_plans["Chest"])
                            st.info(f"ℹ️ Using built-in plan for {primary}. Connect an AI API for custom plans.")

                        if ai_exercises:
                            st.session_state["ai_generated_plan"] = ai_exercises
                            st.session_state["ai_plan_day"] = selected_day_plan
                            st.success(f"✅ Plan ready — {len(ai_exercises)} exercises for {selected_day_plan}!")
                        elif error_msg:
                            st.error(f"⚠️ Could not generate plan: {error_msg}")

            # Show AI plan preview and save option
            if st.session_state.get("ai_generated_plan") and st.session_state.get("ai_plan_day") == selected_day_plan:
                ai_plan = st.session_state["ai_generated_plan"]
                st.markdown("<h4 style='color:#a78bfa; margin-top:12px;'>📋 AI-Generated Plan Preview</h4>", unsafe_allow_html=True)

                for ex in ai_plan:
                    w = ex.get('weight_kg', 0)
                    weight_str = f"{w} kg" if w and float(w) > 0 else "Bodyweight"
                    st.markdown(f"""
                    <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:8px;
                         padding:10px 14px; margin-bottom:6px; display:flex; justify-content:space-between; align-items:center;'>
                        <div>
                            <span style='color:#e2e8f0; font-weight:600; font-size:0.9rem;'>{ex.get('exercise','')}</span>
                            <span style='color:#475569; font-size:0.78rem; margin-left:10px;'>{ex.get('notes','')}</span>
                        </div>
                        <div style='display:flex; gap:16px; font-family:JetBrains Mono,monospace; font-size:0.8rem;'>
                            <span style='color:#60a5fa;'>{ex.get('sets',0)} × {ex.get('reps',0)}</span>
                            <span style='color:#22c55e;'>{weight_str}</span>
                            <span style='color:#475569;'>⏱{ex.get('rest_sec',60)}s</span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                save_col, discard_col = st.columns(2)
                with save_col:
                    if st.button("💾 Save This AI Plan to My Schedule", key="save_ai_plan", use_container_width=True):
                        # Format for storage
                        other_days = [w for w in workouts if w.get("day") != selected_day_plan]
                        new_exercises = []
                        for ex in ai_plan:
                            entry = {
                                "exercise": str(ex.get("exercise", "")),
                                "sets": int(ex.get("sets", 3)),
                                "reps": str(ex.get("reps", "10")),
                                "weight_kg": float(ex.get("weight_kg", 0)),
                                "rest_sec": int(ex.get("rest_sec", 60)),
                                "notes": str(ex.get("notes", "")),
                                "day": selected_day_plan
                            }
                            new_exercises.append(entry)
                        data["gym"]["workouts"] = other_days + new_exercises
                        save_data(data)
                        del st.session_state["ai_generated_plan"]
                        st.success(f"✅ AI plan saved for {selected_day_plan}!")
                        st.rerun()
                with discard_col:
                    if st.button("🗑️ Discard", key="discard_ai_plan", use_container_width=True):
                        del st.session_state["ai_generated_plan"]
                        st.rerun()

        st.divider()
        st.markdown(f"<p style='color:#a78bfa; font-size:0.85rem;'>✏️ Manual plan for <b>{selected_day_plan}</b> — edit directly below:</p>", unsafe_allow_html=True)
        ed_workout = st.data_editor(
            df_workout, num_rows="dynamic",
            column_config={
                "exercise": st.column_config.TextColumn("Exercise", width="large"),
                "sets": st.column_config.NumberColumn("Sets", min_value=1, max_value=20),
                "reps": st.column_config.TextColumn("Reps", width="small",
                    help="e.g. 10, 8-12, AMRAP"),
                "weight_kg": st.column_config.NumberColumn("Weight (kg)", format="%.1f"),
                "rest_sec": st.column_config.NumberColumn("Rest (sec)", min_value=0),
                "notes": st.column_config.TextColumn("Notes"),
            },
            key=f"workout_editor_{selected_day_plan}", use_container_width=True
        )

        if st.button(f"💾 Save {selected_day_plan} Plan"):
            # Keep other days
            other_days = [w for w in workouts if w.get("day") != selected_day_plan]
            new_day_workouts = []
            for _, row in ed_workout.iterrows():
                if row.get("exercise") and str(row.get("exercise","")).strip():
                    entry = row.to_dict()
                    entry["day"] = selected_day_plan
                    new_day_workouts.append(entry)
            data["gym"]["workouts"] = other_days + new_day_workouts
            save_data(data)
            st.success(f"✅ {selected_day_plan} plan saved!")
            st.rerun()

        # Show weekly plan summary
        st.divider()
        st.markdown("<h4>📅 Full Week Overview</h4>", unsafe_allow_html=True)
        week_cols = st.columns(7)
        for i, day in enumerate(day_names):
            day_ex = [w for w in workouts if w.get("day") == day]
            with week_cols[i]:
                count = len(day_ex)
                color = "#22c55e" if count > 0 else "#1e293b"
                st.markdown(f"""
                <div style='background:{color}22; border:1px solid {color}; border-radius:8px; padding:8px; text-align:center;'>
                    <div style='font-size:0.7rem; color:#94a3b8;'>{day[:3]}</div>
                    <div style='font-family:JetBrains Mono,monospace; font-size:1.1rem; color:{color}; font-weight:700;'>{count}</div>
                    <div style='font-size:0.65rem; color:#475569;'>ex</div>
                </div>
                """, unsafe_allow_html=True)

    # ── TAB 3: HEALTH HABITS ──
    with tab_habits:
        st.subheader("🥗 Daily Health & Nutrition Habits")

        habits_data = gym.get("habits", [])
        today_habits = next((h for h in habits_data if h.get("date") == current_day_str), None)

        with st.form("habits_form"):
            hc1, hc2 = st.columns(2)
            with hc1:
                st.markdown("**💧 Hydration & Nutrition**")
                water_liters = st.number_input("Water Intake (liters)", min_value=0.0, max_value=10.0,
                    value=safe_float(today_habits.get("water_liters", 0)) if today_habits else 0.0, step=0.25)
                protein_g = st.number_input("Protein (grams)", min_value=0, max_value=500,
                    value=int(safe_float(today_habits.get("protein_g", 0))) if today_habits else 0)
                calories = st.number_input("Calories (kcal)", min_value=0, max_value=10000,
                    value=int(safe_float(today_habits.get("calories", 0))) if today_habits else 0)
                meal_count = st.number_input("Meals / Day", min_value=0, max_value=10,
                    value=int(safe_float(today_habits.get("meal_count", 3))) if today_habits else 3)

            with hc2:
                st.markdown("**😴 Recovery & Wellness**")
                sleep_hours = st.number_input("Sleep (hours)", min_value=0.0, max_value=24.0,
                    value=safe_float(today_habits.get("sleep_hours", 7)) if today_habits else 7.0, step=0.25)
                supplements = st.multiselect("Supplements Taken",
                    ["Protein Shake", "Creatine", "Omega-3", "Vitamin D", "Magnesium",
                     "Pre-workout", "BCAA", "Zinc", "Caffeine", "Multivitamin"],
                    default=today_habits.get("supplements", []) if today_habits else [])
                steps = st.number_input("Steps Today", min_value=0, max_value=100000,
                    value=int(safe_float(today_habits.get("steps", 0))) if today_habits else 0)
                cheat_meal = st.checkbox("Cheat Meal Today?",
                    value=today_habits.get("cheat_meal", False) if today_habits else False)

            habits_note = st.text_area("Diet / Recovery Notes",
                value=today_habits.get("notes", "") if today_habits else "", height=60)
            save_habits = st.form_submit_button("💾 Save Habits", use_container_width=True)

            if save_habits:
                new_habit = {
                    "date": current_day_str,
                    "water_liters": water_liters,
                    "protein_g": protein_g,
                    "calories": calories,
                    "meal_count": meal_count,
                    "sleep_hours": sleep_hours,
                    "supplements": supplements,
                    "steps": steps,
                    "cheat_meal": cheat_meal,
                    "notes": habits_note
                }
                habits_clean = [h for h in habits_data if h.get("date") != current_day_str]
                habits_clean.append(new_habit)
                data["gym"]["habits"] = habits_clean
                save_data(data)
                st.success("✅ Habits saved!")
                st.rerun()

    # ── TAB 4: PROGRESS ──
    with tab_progress:
        st.subheader("📊 Progress Analytics")

        if sessions:
            df_sessions = pd.DataFrame(sessions)
            df_sessions["date"] = pd.to_datetime(df_sessions["date"])
            df_sessions = df_sessions.sort_values("date")

            prog_c1, prog_c2 = st.columns(2)

            with prog_c1:
                # Sessions per week bar chart
                df_sessions["week"] = df_sessions["date"].dt.strftime("W%U")
                week_counts = df_sessions.groupby("week").size().reset_index(name="sessions")
                fig_weeks = go.Figure(go.Bar(
                    x=week_counts["week"], y=week_counts["sessions"],
                    marker_color="#3b82f6", opacity=0.85
                ))
                fig_weeks.update_layout(
                    title="Sessions per Week", height=220,
                    plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b"),
                    font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=30,b=0)
                )
                st.plotly_chart(fig_weeks, use_container_width=True)

            with prog_c2:
                # Muscle group frequency
                all_muscles = []
                for s in sessions:
                    all_muscles.extend(s.get("muscle_groups", []))
                if all_muscles:
                    muscle_counts = pd.Series(all_muscles).value_counts().reset_index()
                    muscle_counts.columns = ["Muscle", "Count"]
                    fig_muscle = go.Figure(go.Bar(
                        x=muscle_counts["Count"], y=muscle_counts["Muscle"],
                        orientation="h", marker_color="#22c55e", opacity=0.85
                    ))
                    fig_muscle.update_layout(
                        title="Muscle Group Frequency", height=220,
                        plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                        xaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b"),
                        yaxis=dict(color="#475569", showgrid=False),
                        font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=30,b=0)
                    )
                    st.plotly_chart(fig_muscle, use_container_width=True)

            # Body weight trend
            weight_data = [{"date": s["date"], "weight": safe_float(s.get("body_weight", 0))}
                           for s in sessions if safe_float(s.get("body_weight", 0)) > 0]
            if weight_data:
                df_weight = pd.DataFrame(weight_data)
                df_weight["date"] = pd.to_datetime(df_weight["date"])
                fig_w = go.Figure(go.Scatter(
                    x=df_weight["date"], y=df_weight["weight"],
                    mode="lines+markers",
                    line=dict(color="#f97316", width=2),
                    marker=dict(color="#f97316", size=6),
                    fill="tozeroy", fillcolor="rgba(249,115,22,0.08)"
                ))
                fig_w.update_layout(
                    title="Body Weight Trend (kg)", height=200,
                    plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#f97316", showgrid=True, gridcolor="#1e293b"),
                    font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=30,b=0)
                )
                st.plotly_chart(fig_w, use_container_width=True)

            # Habit trends
            habits_log = gym.get("habits", [])
            if habits_log:
                df_hab = pd.DataFrame(habits_log)
                df_hab["date"] = pd.to_datetime(df_hab["date"])
                df_hab = df_hab.sort_values("date")
                df_hab["water_liters"] = df_hab["water_liters"].apply(safe_float)
                df_hab["protein_g"] = df_hab["protein_g"].apply(safe_float)
                df_hab["sleep_hours"] = df_hab["sleep_hours"].apply(safe_float)

                fig_hab = go.Figure()
                fig_hab.add_trace(go.Scatter(x=df_hab["date"], y=df_hab["water_liters"],
                    name="Water (L)", line=dict(color="#60a5fa", width=2)))
                fig_hab.add_trace(go.Scatter(x=df_hab["date"], y=df_hab["sleep_hours"],
                    name="Sleep (h)", line=dict(color="#a78bfa", width=2)))
                fig_hab.update_layout(
                    title="Water & Sleep Trends", height=200,
                    plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b"),
                    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
                    font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=30,b=0)
                )
                st.plotly_chart(fig_hab, use_container_width=True)
        else:
            st.info("Start logging gym sessions to see your progress analytics here.")


# ==========================================
# EGX STOCKS PAGE
# ==========================================
elif st.session_state['page'] == 'Stocks':
    st.title("📈 EGX Stock Tracker")
    st.caption("Track Egyptian Exchange (EGX) stocks — live prices aggregated from Investing.com, StockAnalysis & Mubasher with automatic failover.")

    def safe_float(val):
        try:
            return float(val) if val is not None else 0.0
        except:
            return 0.0

    # Default watchlist: ORHD + 5 similar mid-cap EGX stocks
    DEFAULT_WATCHLIST = [
        {"symbol": "ORHD.CA",  "name": "Orascom Development Egypt", "shares": 0, "avg_price": 0},
        {"symbol": "MNHD.CA",  "name": "Madinet Masr for Housing",   "shares": 0, "avg_price": 0},
        {"symbol": "PHDC.CA",  "name": "Palm Hills Developments",    "shares": 0, "avg_price": 0},
        {"symbol": "EMFD.CA",  "name": "Emaar Misr for Development", "shares": 0, "avg_price": 0},
        {"symbol": "HELI.CA",  "name": "Heliopolis Housing",         "shares": 0, "avg_price": 0},
        {"symbol": "TMGH.CA",  "name": "Talaat Moustafa Group",      "shares": 0, "avg_price": 0},
    ]

    stocks_data = data["stocks"]
    if not stocks_data.get("watchlist"):
        stocks_data["watchlist"] = DEFAULT_WATCHLIST
        data["stocks"] = stocks_data
        save_data(data)

    watchlist = stocks_data["watchlist"]

    # ──────────────────────────────────────────────────────────────────────
    #  LIVE EGX PRICE FETCHER — multi-source with graceful fallback
    # ──────────────────────────────────────────────────────────────────────
    #  Primary:   Investing.com   → most reliable, live prices, volume, OHLC
    #  Secondary: StockAnalysis   → cross-verification & backup
    #  Tertiary:  Mubasher Arabic → official EGX partner (Arabic site, less blocked)
    #  Quaternary: Stooq CSV      → historical fallback
    #  Final:     Cached price    → last known price from local history
    # ──────────────────────────────────────────────────────────────────────
    import re as _re_stocks

    # Investing.com uses slugs, not tickers. Map EGX tickers → Investing.com slugs.
    # Missing tickers auto-fall-through to the next source.
    INVESTING_SLUGS = {
        "ORHD": "orascom-development-egypt",
        "MNHD": "madinet-nasr-for-housing-and-development",
        "PHDC": "palm-hills-development",
        "EMFD": "emaar-misr-for-development",
        "HELI": "heliopolis-housing",
        "TMGH": "t-m-g-holding",
        "COMI": "commercial-intl-bank-(egypt)",
        "ETEL": "telecom-egypt",
        "SWDY": "elsewedy-cable",
        "HRHO": "ef-hermes-hold",
        "JUFO": "juhayna-food-industries",
        "AMOC": "alexandria-mineral-oils",
        "ORAS": "orascom-construction-ltd",
        "FWRY": "fawry-banking-and-payment",
        "EGAL": "egypt-aluminum",
        "EAST": "eastern-company",
        "EFIH": "ef-holding",
        "EKHO": "egyptian-kuwaiti-holding",
        "SKPC": "sidi-kerir-petrochemicals",
        "ABUK": "abou-kir-fertilizers",
    }

    _HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _parse_number(txt):
        """Parse a number like '93.90' or '1,030,250' safely."""
        if not txt:
            return None
        try:
            cleaned = str(txt).replace(",", "").replace("\xa0", "").strip()
            # Strip trailing parentheses like '+3.19%'
            cleaned = _re_stocks.sub(r"[^\d\.\-]", "", cleaned)
            return float(cleaned) if cleaned else None
        except Exception:
            return None

    def _fetch_from_investing(ticker_plain: str):
        """Primary source — Investing.com. Returns dict or None."""
        slug = INVESTING_SLUGS.get(ticker_plain.upper())
        if not slug:
            return None
        try:
            url = f"https://www.investing.com/equities/{slug}"
            r = requests.get(url, headers=_HEADERS, timeout=12)
            if r.status_code != 200:
                return None
            html = r.text
            # Extract from the FAQ block — robust regex pattern
            # Pattern: "trading at a price of <price> EGP, with a previous close of <prev> EGP"
            m_price_prev = _re_stocks.search(
                r"trading at a price of\s*([\d,.]+)\s*EGP.*?previous close of\s*([\d,.]+)\s*EGP",
                html, _re_stocks.S | _re_stocks.I)
            # Pattern: day range
            m_range = _re_stocks.search(
                r"day range of\s*([\d,.]+)\s*EGP\s*to\s*([\d,.]+)\s*EGP",
                html, _re_stocks.S | _re_stocks.I)
            # Pattern: 52-week range
            m_52w = _re_stocks.search(
                r"52-week range spans from\s*([\d,.]+)\s*EGP\s*to\s*([\d,.]+)\s*EGP",
                html, _re_stocks.S | _re_stocks.I)

            if not m_price_prev:
                return None
            price = _parse_number(m_price_prev.group(1))
            prev  = _parse_number(m_price_prev.group(2))
            if not price or price <= 0:
                return None
            low   = _parse_number(m_range.group(1)) if m_range else price
            high  = _parse_number(m_range.group(2)) if m_range else price
            w52_low  = _parse_number(m_52w.group(1)) if m_52w else 0
            w52_high = _parse_number(m_52w.group(2)) if m_52w else 0

            change = round(price - (prev or price), 3)
            change_pct = round(((price - prev) / prev * 100) if prev else 0, 2)

            return {
                "price": price, "prev_close": prev or price,
                "change": change, "change_pct": change_pct,
                "high": high or price, "low": low or price,
                "w52_high": w52_high, "w52_low": w52_low,
                "volume": 0,
                "currency": "EGP", "closes": [], "timestamps": [],
                "source": "Investing.com"
            }
        except Exception:
            return None

    def _fetch_from_stockanalysis(ticker_plain: str):
        """Secondary — StockAnalysis.com. Returns dict or None."""
        try:
            url = f"https://stockanalysis.com/quote/egx/{ticker_plain.upper()}/"
            r = requests.get(url, headers=_HEADERS, timeout=12)
            if r.status_code != 200:
                return None
            html = r.text
            # Price typically appears as: <price>\n-4.12 (-4.39%)\nAt close:
            # Flexible regex — find the first standalone number before "At close" / "Previous Close"
            m_open = _re_stocks.search(
                r"Open[^\d\-\|]{0,40}?([\d,.]+)", html, _re_stocks.I)
            m_prev = _re_stocks.search(
                r"Previous Close[^\d\-\|]{0,40}?([\d,.]+)", html, _re_stocks.I)
            m_range = _re_stocks.search(
                r"Day.s Range[^\d\-\|]{0,40}?([\d,.]+)\s*-\s*([\d,.]+)", html, _re_stocks.I)
            m_vol = _re_stocks.search(
                r"Volume[^\d\-\|]{0,40}?([\d,.]+)", html, _re_stocks.I)

            # Price: pattern "XX.XX -X.XX (-X.XX%)"
            m_price = _re_stocks.search(
                r">\s*([\d,]+\.\d{1,4})\s*<[^>]*>\s*[+\-]?[\d,.]+\s*\([+\-]?[\d,.]+%\)",
                html)
            if not m_price:
                # Fallback — try simpler pattern near currency tag
                m_price = _re_stocks.search(
                    r"Currency is EGP[^\d]{0,200}([\d,]+\.\d{1,4})", html, _re_stocks.S)
            if not m_price:
                return None

            price = _parse_number(m_price.group(1))
            if not price or price <= 0:
                return None
            prev = _parse_number(m_prev.group(1)) if m_prev else price
            low  = _parse_number(m_range.group(1)) if m_range else price
            high = _parse_number(m_range.group(2)) if m_range else price
            vol  = _parse_number(m_vol.group(1)) if m_vol else 0

            return {
                "price": price, "prev_close": prev or price,
                "change": round(price - (prev or price), 3),
                "change_pct": round(((price - prev) / prev * 100) if prev else 0, 2),
                "high": high or price, "low": low or price,
                "w52_high": 0, "w52_low": 0,
                "volume": vol or 0,
                "currency": "EGP", "closes": [], "timestamps": [],
                "source": "StockAnalysis"
            }
        except Exception:
            return None

    def _fetch_from_mubasher(ticker_plain: str):
        """Tertiary — Mubasher Arabic (less blocked than English)."""
        try:
            url = f"https://www.mubasher.info/markets/EGX/stocks/{ticker_plain.upper()}"
            r = requests.get(url, headers=_HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            html = r.text
            # Mubasher exposes price in a JSON-LD or meta tag
            m_price = _re_stocks.search(
                r'"price"\s*:\s*"?([\d.]+)"?', html)
            m_prev = _re_stocks.search(
                r'"previousClose"\s*:\s*"?([\d.]+)"?', html)
            if not m_price:
                return None
            price = _parse_number(m_price.group(1))
            if not price or price <= 0:
                return None
            prev = _parse_number(m_prev.group(1)) if m_prev else price
            return {
                "price": price, "prev_close": prev or price,
                "change": round(price - (prev or price), 3),
                "change_pct": round(((price - prev) / prev * 100) if prev else 0, 2),
                "high": price, "low": price,
                "w52_high": 0, "w52_low": 0, "volume": 0,
                "currency": "EGP", "closes": [], "timestamps": [],
                "source": "Mubasher"
            }
        except Exception:
            return None

    def _fetch_from_stooq(ticker_plain: str):
        """Quaternary — Stooq CSV. End-of-day only."""
        try:
            ticker = f"{ticker_plain.lower()}.ca"
            url = f"https://stooq.com/q/d/l/?s={ticker}&i=d"
            r = requests.get(url, headers=_HEADERS, timeout=10)
            if r.status_code != 200 or "Date" not in r.text:
                return None
            import io as _io
            df = pd.read_csv(_io.StringIO(r.text))
            if df.empty or "Close" not in df.columns:
                return None
            df = df.dropna(subset=["Close"]).sort_values("Date")
            last = df.iloc[-1]
            price = float(last["Close"])
            prev  = float(df.iloc[-2]["Close"]) if len(df) > 1 else price
            if price <= 0:
                return None
            return {
                "price": price, "prev_close": prev,
                "change": round(price - prev, 3),
                "change_pct": round(((price - prev) / prev * 100) if prev else 0, 2),
                "high": float(last.get("High", price) or price),
                "low":  float(last.get("Low",  price) or price),
                "w52_high": 0, "w52_low": 0,
                "volume": float(last.get("Volume", 0) or 0),
                "currency": "EGP",
                "closes": df["Close"].tolist()[-30:], "timestamps": [],
                "source": "Stooq (EOD)"
            }
        except Exception:
            return None

    @st.cache_data(ttl=180)  # 3-minute cache for near-live refresh
    def fetch_egx_price(symbol):
        """
        Fetch a single EGX stock price with layered fallbacks.
        `symbol` may be with or without the .CA suffix — both are handled.
        """
        ticker_plain = symbol.upper().replace(".CA", "").strip()
        if not ticker_plain:
            return _empty_price("⚠️ Invalid ticker")

        # Try each source in priority order
        for fetcher in (_fetch_from_investing,
                        _fetch_from_stockanalysis,
                        _fetch_from_mubasher,
                        _fetch_from_stooq):
            result = fetcher(ticker_plain)
            if result and result.get("price", 0) > 0:
                return result

        # Final fallback — use cached last-known price
        ph = data.get("stocks", {}).get("price_history", {})
        if ph:
            last_date = sorted(ph.keys())[-1]
            saved_p = ph[last_date].get(symbol, 0) or ph[last_date].get(f"{ticker_plain}.CA", 0)
            if saved_p and saved_p > 0:
                return {
                    "price": float(saved_p), "prev_close": float(saved_p),
                    "change": 0, "change_pct": 0,
                    "high": float(saved_p), "low": float(saved_p),
                    "w52_high": 0, "w52_low": 0, "volume": 0,
                    "currency": "EGP", "closes": [], "timestamps": [],
                    "source": f"⚠️ cached {last_date}"
                }

        return _empty_price("⚠️ N/A — all sources down")

    def _empty_price(src_label: str):
        return {
            "price": 0, "prev_close": 0, "change": 0, "change_pct": 0,
            "high": 0, "low": 0, "w52_high": 0, "w52_low": 0, "volume": 0,
            "currency": "EGP", "closes": [], "timestamps": [], "source": src_label
        }

    col_ref, col_info_stocks, col_time_stocks = st.columns([1, 2, 2])
    with col_ref:
        if st.button("🔄 Refresh Prices", key="refresh_stocks"):
            st.cache_data.clear()
            st.rerun()
    with col_info_stocks:
        st.markdown(f"<span style='color:#475569; font-size:0.8rem;'>⏱ Prices cache for 3 min · EGX trades Sun–Thu 10:00–14:30 Cairo time.</span>", unsafe_allow_html=True)
    with col_time_stocks:
        now_cairo = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        st.markdown(f"<span style='color:#475569; font-size:0.8rem;'>🕐 Cairo time: {now_cairo.strftime('%H:%M:%S')}</span>", unsafe_allow_html=True)

    # Fetch all prices
    with st.spinner("Fetching EGX prices..."):
        price_results = {}
        for stock in watchlist:
            sym = stock.get("symbol", "")
            if sym:
                price_results[sym] = fetch_egx_price(sym)

    # Save today's prices to history
    today_iso = datetime.date.today().strftime("%Y-%m-%d")
    if today_iso not in stocks_data.get("price_history", {}):
        stocks_data["price_history"][today_iso] = {}
    for sym, res in price_results.items():
        if res["price"] > 0:
            stocks_data["price_history"][today_iso][sym] = res["price"]
    data["stocks"] = stocks_data
    save_data(data)

    # ── PRICE CARDS ──
    st.markdown("<h3 style='margin-bottom:10px;'>📊 Live Prices</h3>", unsafe_allow_html=True)

    # Check for wholesale source failure (all sources returned empty)
    any_live = any(r.get("price", 0) > 0 and not r.get("source", "").startswith("⚠️")
                   for r in price_results.values())
    if not any_live and price_results:
        st.warning(
            "⚠️ All live price sources are currently unreachable. Showing cached data where available. "
            "Try again in a few minutes, or verify prices directly at "
            "[investing.com](https://www.investing.com/equities/egypt) · "
            "[stockanalysis.com](https://stockanalysis.com/list/egyptian-stock-exchange/)."
        )

    cols = st.columns(len(watchlist)) if watchlist else [st]
    for i, stock in enumerate(watchlist):
        sym = stock.get("symbol", "")
        name = stock.get("name", sym)
        res = price_results.get(sym, {})
        price = res.get("price", 0)
        chg = res.get("change", 0)
        chg_pct = res.get("change_pct", 0)
        src = res.get("source", "")

        # Source classification → badge color
        is_live = price > 0 and not src.startswith("⚠️")
        is_cached = src.startswith("⚠️ cached")
        is_failed = price == 0 or src.startswith("⚠️ N/A")

        if is_live:
            src_color = "#22c55e"
            status_dot = "🟢"
            border_col = "#1e4a2e" if chg >= 0 else "#4a1e1e"
        elif is_cached:
            src_color = "#f59e0b"
            status_dot = "🟡"
            border_col = "#4a3a1e"
        else:
            src_color = "#ef4444"
            status_dot = "🔴"
            border_col = "#1e293b"

        chg_color = "#22c55e" if chg >= 0 else "#ef4444"
        arrow = "▲" if chg >= 0 else "▼"

        with cols[i]:
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid {border_col}; border-radius:10px;
                 padding:12px 10px; text-align:center; margin-bottom:4px;'>
                <div style='color:#64748b; font-size:0.65rem; text-transform:uppercase; letter-spacing:0.05em;'>
                    {status_dot} {sym.replace(".CA","")}
                </div>
                <div style='font-family:JetBrains Mono,monospace; font-size:1.3rem; font-weight:700;
                     color:{"#e2e8f0" if price>0 else "#475569"};'>
                    {"—" if price==0 else f"{price:,.2f}"}
                </div>
                <div style='font-size:0.75rem; color:{chg_color}; font-weight:600;'>
                    {"N/A" if price==0 else f"{arrow} {abs(chg_pct):.2f}%"}
                </div>
                <div style='font-size:0.65rem; color:#334155; margin-top:2px;'>{name[:20]}</div>
                <div style='font-size:0.58rem; color:{src_color}; margin-top:4px;
                     padding-top:4px; border-top:1px solid #1e293b;
                     font-family:JetBrains Mono,monospace; letter-spacing:0.03em;'>
                    {src if src else "—"}
                </div>
            </div>
            """, unsafe_allow_html=True)

    st.divider()

    # ── PORTFOLIO & CHART TABS ──
    tab_chart, tab_portfolio, tab_watchlist_mgr = st.tabs(["📈 Price Charts", "💼 My Portfolio", "⚙️ Manage Watchlist"])

    with tab_chart:
        chart_sym = st.selectbox("Select Stock", [s["symbol"] for s in watchlist],
            format_func=lambda x: f"{x.replace('.CA','')} — {next((s['name'] for s in watchlist if s['symbol']==x), x)}")
        res_chart = price_results.get(chart_sym, {})
        closes = res_chart.get("closes", [])
        timestamps = res_chart.get("timestamps", [])

        name_chart = next((s["name"] for s in watchlist if s["symbol"] == chart_sym), chart_sym)

        # Build chart from saved daily price history (works even without intraday closes)
        ph_stocks = stocks_data.get("price_history", {})
        hist_prices = []
        for date_key in sorted(ph_stocks.keys()):
            p_val = ph_stocks[date_key].get(chart_sym, None)
            if p_val:
                hist_prices.append({"date": date_key, "price": float(p_val)})

        if hist_prices:
            df_hist_stock = pd.DataFrame(hist_prices)
            df_hist_stock["date"] = pd.to_datetime(df_hist_stock["date"])
            ma5_s = df_hist_stock["price"].rolling(5, min_periods=1).mean()
            fig_stock = go.Figure()
            fig_stock.add_trace(go.Scatter(x=df_hist_stock["date"], y=df_hist_stock["price"],
                name=name_chart, mode="lines+markers",
                line=dict(color="#3b82f6", width=2.5), marker=dict(size=5),
                fill="tozeroy", fillcolor="rgba(59,130,246,0.07)"))
            fig_stock.add_trace(go.Scatter(x=df_hist_stock["date"], y=ma5_s,
                name="5-day MA", mode="lines",
                line=dict(color="#f97316", width=1.5, dash="dot")))
            fig_stock.update_layout(
                title=f"{name_chart} — History", height=300,
                plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#3b82f6", showgrid=True, gridcolor="#1e293b", title="Price (EGP)"),
                legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
                font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=40,b=0))
            st.plotly_chart(fig_stock, use_container_width=True)
        else:
            cur_price = price_results.get(chart_sym, {}).get("price", 0)
            if cur_price > 0:
                st.info(f"Today's price fetched: {cur_price:,.2f} EGP. Historical chart will build daily as you open this page.")
            else:
                st.warning(f"No price data for {chart_sym}. The data source may be temporarily unavailable. Try refreshing.")

        if True:  # Always show comparison section
            # Comparison chart using saved history
            st.subheader("📊 Comparative Performance (from your history)")
            ph = stocks_data.get("price_history", {})
            if len(ph) > 1:
                comp_data = []
                for date_key in sorted(ph.keys()):
                    row = {"date": date_key}
                    for stock in watchlist:
                        sym = stock["symbol"]
                        row[sym] = ph[date_key].get(sym, None)
                    comp_data.append(row)
                df_comp = pd.DataFrame(comp_data)
                df_comp["date"] = pd.to_datetime(df_comp["date"])

                fig_comp = go.Figure()
                colors_comp = ["#3b82f6","#22c55e","#f97316","#a78bfa","#facc15","#ef4444"]
                for idx, stock in enumerate(watchlist):
                    sym = stock["symbol"]
                    if sym in df_comp.columns:
                        vals = df_comp[sym].dropna()
                        if len(vals) > 1:
                            # Normalize to % change from first observation
                            base = vals.iloc[0]
                            normalized = ((vals / base) - 1) * 100
                            fig_comp.add_trace(go.Scatter(
                                x=df_comp["date"][:len(vals)], y=normalized,
                                name=sym.replace(".CA",""),
                                line=dict(color=colors_comp[idx % len(colors_comp)], width=2)
                            ))
                fig_comp.add_hline(y=0, line_dash="dot", line_color="#334155")
                fig_comp.update_layout(
                    height=280, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b",
                               title="% Change vs First Observed"),
                    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
                    font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=10,b=0)
                )
                st.plotly_chart(fig_comp, use_container_width=True)

    with tab_portfolio:
        st.subheader("💼 My Portfolio")
        st.caption("Enter your shares and average buy price to track P&L.")

        df_port = pd.DataFrame(watchlist)
        if "shares" not in df_port.columns: df_port["shares"] = 0
        if "avg_price" not in df_port.columns: df_port["avg_price"] = 0

        ed_port = st.data_editor(
            df_port[["symbol","name","shares","avg_price"]],
            num_rows="fixed",
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", disabled=True),
                "name": st.column_config.TextColumn("Company", disabled=True),
                "shares": st.column_config.NumberColumn("Shares Held", min_value=0),
                "avg_price": st.column_config.NumberColumn("Avg Buy Price (EGP)", format="%.2f")
            },
            key="portfolio_editor", use_container_width=True
        )

        if st.button("💾 Save Portfolio"):
            for i, row in ed_port.iterrows():
                if i < len(data["stocks"]["watchlist"]):
                    data["stocks"]["watchlist"][i]["shares"] = row["shares"]
                    data["stocks"]["watchlist"][i]["avg_price"] = row["avg_price"]
            save_data(data)
            st.success("✅ Portfolio saved!")
            st.rerun()

        # P&L Table
        st.divider()
        pnl_rows = []
        total_invested = 0
        total_current = 0
        for stock in watchlist:
            sym = stock["symbol"]
            shares = safe_float(stock.get("shares", 0))
            avg_buy = safe_float(stock.get("avg_price", 0))
            current_p = price_results.get(sym, {}).get("price", 0)
            invested = shares * avg_buy
            current_val = shares * current_p
            pnl = current_val - invested
            pnl_pct = (pnl / invested * 100) if invested > 0 else 0
            total_invested += invested
            total_current += current_val
            if shares > 0 or avg_buy > 0:
                pnl_rows.append({
                    "Stock": sym.replace(".CA", ""),
                    "Shares": int(shares),
                    "Avg Buy": f"{avg_buy:,.2f}",
                    "Current": f"{current_p:,.2f}",
                    "Invested": f"{invested:,.0f} EGP",
                    "Value Now": f"{current_val:,.0f} EGP",
                    "P&L": f"{'▲' if pnl>=0 else '▼'} {abs(pnl):,.0f} EGP ({pnl_pct:+.1f}%)"
                })

        if pnl_rows:
            st.dataframe(pd.DataFrame(pnl_rows), use_container_width=True, hide_index=True)
            total_pnl = total_current - total_invested
            total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
            pnl_color = "#22c55e" if total_pnl >= 0 else "#ef4444"
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid {pnl_color}44; border-radius:12px; padding:16px 20px;
                 display:flex; justify-content:space-between; align-items:center; margin-top:8px;'>
                <div>
                    <div style='color:#64748b; font-size:0.75rem;'>TOTAL PORTFOLIO</div>
                    <div style='font-family:JetBrains Mono,monospace; font-size:1.5rem; color:#e2e8f0;'>{total_current:,.0f} EGP</div>
                </div>
                <div style='text-align:right;'>
                    <div style='color:#64748b; font-size:0.75rem;'>TOTAL P&L</div>
                    <div style='font-family:JetBrains Mono,monospace; font-size:1.5rem; color:{pnl_color};'>
                        {'▲' if total_pnl>=0 else '▼'} {abs(total_pnl):,.0f} EGP ({total_pnl_pct:+.1f}%)
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.info("Enter your shares and average buy price in the table above to track your P&L.")

    with tab_watchlist_mgr:
        st.subheader("⚙️ Manage Your Watchlist")
        st.caption("Add or remove EGX stocks. Use the raw EGX ticker (e.g. ORHD, COMI, ETEL). Live prices are aggregated from multiple trusted sources with automatic failover.")

        # Build display without symbol column (show ticker only)
        wl_display = [{"ticker": s["symbol"].replace(".CA","").upper(), "name": s["name"]} for s in watchlist]
        df_wl = pd.DataFrame(wl_display) if wl_display else pd.DataFrame(columns=["ticker","name"])

        ed_wl = st.data_editor(
            df_wl, num_rows="dynamic",
            column_config={
                "ticker": st.column_config.TextColumn("EGX Ticker (e.g. ORHD)", width="medium",
                    help="Use the raw EGX ticker symbol — no suffix needed"),
                "name": st.column_config.TextColumn("Company Display Name", width="large")
            },
            key="watchlist_editor", use_container_width=True
        )

        if st.button("💾 Update Watchlist"):
            new_wl = []
            for _, row in ed_wl.iterrows():
                raw_ticker = str(row.get("ticker","")).strip().upper().replace(".CA","")
                if raw_ticker:
                    sym_with_ca = raw_ticker + ".CA"
                    existing = next((s for s in watchlist if s["symbol"].replace(".CA","").upper() == raw_ticker), {})
                    new_wl.append({
                        "symbol": sym_with_ca,
                        "name": str(row.get("name", raw_ticker)).strip(),
                        "shares": existing.get("shares", 0),
                        "avg_price": existing.get("avg_price", 0)
                    })
            data["stocks"]["watchlist"] = new_wl
            save_data(data)
            st.success("✅ Watchlist updated!")
            st.rerun()

        st.markdown("""
        <div class='insight-card' style='margin-top:12px;'>
            💡 <b>Common EGX Tickers:</b><br>
            ORHD (Orascom Development) · MNHD (Madinet Masr) · PHDC (Palm Hills) ·
            EMFD (Emaar Misr) · TMGH (Talaat Moustafa) · COMI (CIB) ·
            ETEL (Telecom Egypt) · SWDY (Sewedy Electric) · HRHO (EFG Hermes) ·
            JUFO (Juhayna Food) · AMOC (Alexandria Mineral Oils) · ORAS (Orascom Construction) ·
            FWRY (Fawry) · EGAL (Egypt Aluminum) · ABUK (Abou Kir Fertilizers)<br><br>
            <b>Data Sources (auto-failover):</b> Investing.com → StockAnalysis.com → Mubasher → Stooq → cached.<br>
            <span style='color:#64748b;font-size:0.75rem;'>
              🟢 live price · 🟡 cached fallback · 🔴 all sources unreachable
            </span>
        </div>
        """, unsafe_allow_html=True)


# ==========================================
# BUSINESS TRACKER PAGE
# ==========================================
elif st.session_state['page'] == 'Business':
    st.title("🏪 Business Tracker")
    st.caption("Track P&L for your Specialty Coffee Shop and Fashion Store")

    biz = data["business"]
    today_dt_b = datetime.date.today()

    def safe_float(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0

    BUSINESSES = {
        "coffee_shop":   {"name": "☕ Specialty Coffee Online Shop", "color": "#f97316", "key": "coffee_shop"},
        "fashion_store": {"name": "👗 Fashion Online & Offline Store",  "color": "#a78bfa", "key": "fashion_store"},
    }

    biz_tab_sel = st.radio("Select Business", ["☕ Coffee Shop", "👗 Fashion Store", "📊 Combined Overview"],
                           horizontal=True, key="biz_sel")
    biz_key = "coffee_shop" if "Coffee" in biz_tab_sel else "fashion_store" if "Fashion" in biz_tab_sel else "combined"

    def calc_biz(key):
        b = biz.get(key, {"income": [], "expenses": [], "inventory": [], "metrics": []})
        income_egp  = sum(safe_float(i.get("amount")) for i in b.get("income", []))
        expense_egp = sum(safe_float(e.get("amount")) for e in b.get("expenses", []))
        gross_profit = income_egp - expense_egp
        margin = (gross_profit / income_egp * 100) if income_egp > 0 else 0
        return {"income": income_egp, "expenses": expense_egp, "profit": gross_profit,
                "margin": margin, "data": b}

    coffee_calc = calc_biz("coffee_shop")
    fashion_calc = calc_biz("fashion_store")

    # ── COMBINED OVERVIEW ──
    if biz_key == "combined":
        st.markdown("### 📊 Combined Business Overview")
        total_income   = coffee_calc["income"]  + fashion_calc["income"]
        total_expenses = coffee_calc["expenses"] + fashion_calc["expenses"]
        total_profit   = coffee_calc["profit"]   + fashion_calc["profit"]
        total_margin   = (total_profit / total_income * 100) if total_income > 0 else 0

        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Total Revenue", f"{total_income:,.0f} EGP")
        cc2.metric("Total Expenses", f"{total_expenses:,.0f} EGP")
        cc3.metric("Net Profit", f"{total_profit:,.0f} EGP",
                   delta=f"{total_margin:.1f}% margin")
        cc4.metric("Coffee Profit", f"{coffee_calc['profit']:,.0f} EGP")

        # Side-by-side bar chart
        fig_cmp = go.Figure()
        fig_cmp.add_trace(go.Bar(name="Revenue",  x=["Coffee Shop","Fashion Store"],
            y=[coffee_calc["income"],  fashion_calc["income"]],  marker_color=["#f97316","#a78bfa"]))
        fig_cmp.add_trace(go.Bar(name="Expenses", x=["Coffee Shop","Fashion Store"],
            y=[coffee_calc["expenses"],fashion_calc["expenses"]], marker_color=["#7c2d12","#4c1d95"]))
        fig_cmp.add_trace(go.Bar(name="Profit",   x=["Coffee Shop","Fashion Store"],
            y=[coffee_calc["profit"],  fashion_calc["profit"]],
            marker_color=["#22c55e" if coffee_calc["profit"]>=0 else "#ef4444",
                          "#22c55e" if fashion_calc["profit"]>=0 else "#ef4444"]))
        fig_cmp.update_layout(barmode="group", height=280, plot_bgcolor="#080c14",
            paper_bgcolor="#080c14", font=dict(color="#94a3b8"),
            xaxis=dict(color="#475569"), yaxis=dict(color="#475569", gridcolor="#1e293b"),
            legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=0,r=0,t=10,b=0))
        st.plotly_chart(fig_cmp, use_container_width=True)

        # Health indicators
        st.divider()
        for bname, bcalc in [("Coffee Shop", coffee_calc), ("Fashion Store", fashion_calc)]:
            margin_c = "#22c55e" if bcalc["margin"] > 20 else "#facc15" if bcalc["margin"] > 5 else "#ef4444"
            status = "🟢 Healthy" if bcalc["margin"] > 20 else "🟡 Watch" if bcalc["margin"] > 5 else "🔴 Loss Risk"
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:10px; padding:14px 18px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;'>
                <div><span style='color:#e2e8f0; font-weight:600;'>{bname}</span>
                     <span style='margin-left:10px; color:{margin_c}; font-size:0.85rem;'>{status}</span></div>
                <div style='display:flex; gap:24px; font-family:JetBrains Mono,monospace; font-size:0.9rem;'>
                    <span style='color:#22c55e;'>Revenue: {bcalc["income"]:,.0f}</span>
                    <span style='color:#ef4444;'>Costs: {bcalc["expenses"]:,.0f}</span>
                    <span style='color:{margin_c};'>Margin: {bcalc["margin"]:.1f}%</span>
                </div>
            </div>
            """, unsafe_allow_html=True)
        st.stop()

    # ── SINGLE BUSINESS VIEW ──
    b_cfg   = BUSINESSES[biz_key]
    b_calc  = coffee_calc if biz_key == "coffee_shop" else fashion_calc
    b_data  = biz.get(biz_key, {"income": [], "expenses": [], "inventory": [], "metrics": []})

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Total Revenue",  f"{b_calc['income']:,.0f} EGP")
    mc2.metric("Total Expenses", f"{b_calc['expenses']:,.0f} EGP")
    profit_color_str = "normal" if b_calc['profit'] >= 0 else "inverse"
    mc3.metric("Net Profit", f"{b_calc['profit']:,.0f} EGP", delta=f"{b_calc['margin']:.1f}% margin")
    mc4.metric("Transactions", len(b_data.get("income", [])) + len(b_data.get("expenses", [])))

    st.divider()

    btab1, btab2, btab3, btab4 = st.tabs(["💵 Income", "💸 Expenses", "📦 Inventory", "📈 Analytics"])

    with btab1:
        st.subheader(f"Income — {b_cfg['name']}")
        income_data = b_data.get("income", [])
        df_inc_b = pd.DataFrame(income_data) if income_data else pd.DataFrame(
            columns=["date", "source", "category", "amount", "channel", "notes"])
        for col_n, dflt in [("date", today_dt_b.strftime("%Y-%m-%d")), ("source",""),
                             ("category","Sales"), ("amount",0), ("channel","Online"), ("notes","")]:
            if col_n not in df_inc_b.columns: df_inc_b[col_n] = dflt
        ed_inc_b = st.data_editor(
            df_inc_b.assign(date=pd.to_datetime(df_inc_b["date"], errors="coerce")),
            num_rows="dynamic",
            column_config={
                "date": st.column_config.DateColumn("Date"),
                "source": st.column_config.TextColumn("Source/Product", width="medium"),
                "category": st.column_config.SelectboxColumn("Category",
                    options=["Sales","Delivery","Wholesale","Subscription","Consignment","Other"] if biz_key=="coffee_shop"
                            else ["Online Sales","Offline Sales","Wholesale","Consignment","Returns","Other"]),
                "amount": st.column_config.NumberColumn("Amount (EGP)", format="%.2f"),
                "channel": st.column_config.SelectboxColumn("Channel",
                    options=["Online","Offline","Both"] if biz_key=="fashion_store" else ["Online","WhatsApp","Instagram","Other"]),
                "notes": st.column_config.TextColumn("Notes"),
            }, key=f"ed_biz_inc_{biz_key}", use_container_width=True)
        ed_inc_b["date"] = ed_inc_b["date"].astype(str)
        if st.button("💾 Save Income", key=f"save_biz_inc_{biz_key}"):
            data["business"][biz_key]["income"] = ed_inc_b.to_dict("records")
            save_data(data); st.success("✅ Saved!"); st.rerun()

    with btab2:
        st.subheader(f"Expenses — {b_cfg['name']}")
        exp_data = b_data.get("expenses", [])
        df_exp_b = pd.DataFrame(exp_data) if exp_data else pd.DataFrame(
            columns=["date","item","category","amount","supplier","recurring","notes"])
        for col_n, dflt in [("date", today_dt_b.strftime("%Y-%m-%d")),("item",""),
                             ("category","COGS"),("amount",0),("supplier",""),("recurring",False),("notes","")]:
            if col_n not in df_exp_b.columns: df_exp_b[col_n] = dflt
        coffee_cats = ["COGS (Beans/Milk/Syrups)","Packaging","Delivery/Shipping","Marketing",
                       "Platform Fees","Salary","Rent/Storage","Equipment","Utilities","Other"]
        fashion_cats = ["COGS (Product Cost)","Packaging","Shipping","Marketing/Ads","Platform Fees",
                        "Salary","Rent (Store)","Utilities","Returns/Refunds","Other"]
        ed_exp_b = st.data_editor(
            df_exp_b.assign(date=pd.to_datetime(df_exp_b["date"], errors="coerce")),
            num_rows="dynamic",
            column_config={
                "date": st.column_config.DateColumn("Date"),
                "item": st.column_config.TextColumn("Item/Description", width="medium"),
                "category": st.column_config.SelectboxColumn("Category",
                    options=coffee_cats if biz_key=="coffee_shop" else fashion_cats),
                "amount": st.column_config.NumberColumn("Amount (EGP)", format="%.2f"),
                "supplier": st.column_config.TextColumn("Supplier/Vendor"),
                "recurring": st.column_config.CheckboxColumn("Recurring?"),
                "notes": st.column_config.TextColumn("Notes"),
            }, key=f"ed_biz_exp_{biz_key}", use_container_width=True)
        ed_exp_b["date"] = ed_exp_b["date"].astype(str)
        if st.button("💾 Save Expenses", key=f"save_biz_exp_{biz_key}"):
            data["business"][biz_key]["expenses"] = ed_exp_b.to_dict("records")
            save_data(data); st.success("✅ Saved!"); st.rerun()

        # Expense breakdown donut
        if not df_exp_b.empty:
            df_exp_b["amount"] = df_exp_b["amount"].apply(safe_float)
            exp_by_cat = df_exp_b.groupby("category")["amount"].sum().reset_index()
            if not exp_by_cat.empty:
                fig_exp_d = go.Figure(go.Pie(labels=exp_by_cat["category"], values=exp_by_cat["amount"],
                    hole=0.55, marker=dict(colors=["#3b82f6","#22c55e","#f59e0b","#ef4444","#a78bfa",
                                                    "#fb923c","#06b6d4","#ec4899","#84cc16","#f43f5e"])))
                fig_exp_d.update_layout(height=260, paper_bgcolor="#080c14", font=dict(color="#94a3b8"),
                    legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=10,r=10,t=10,b=10))
                st.plotly_chart(fig_exp_d, use_container_width=True)

    with btab3:
        st.subheader(f"Inventory — {b_cfg['name']}")
        inv_data = b_data.get("inventory", [])
        df_inv = pd.DataFrame(inv_data) if inv_data else pd.DataFrame(
            columns=["item","sku","qty","unit","cost_per_unit","reorder_at","supplier","notes"])
        for col_n, dflt in [("item",""),("sku",""),("qty",0),("unit","kg" if biz_key=="coffee_shop" else "pcs"),
                             ("cost_per_unit",0),("reorder_at",0),("supplier",""),("notes","")]:
            if col_n not in df_inv.columns: df_inv[col_n] = dflt
        ed_inv = st.data_editor(df_inv, num_rows="dynamic",
            column_config={
                "item": st.column_config.TextColumn("Item", width="medium"),
                "sku": st.column_config.TextColumn("SKU/Code", width="small"),
                "qty": st.column_config.NumberColumn("Quantity", format="%.2f"),
                "unit": st.column_config.TextColumn("Unit"),
                "cost_per_unit": st.column_config.NumberColumn("Cost/Unit (EGP)", format="%.2f"),
                "reorder_at": st.column_config.NumberColumn("Reorder At"),
                "supplier": st.column_config.TextColumn("Supplier"),
                "notes": st.column_config.TextColumn("Notes"),
            }, key=f"ed_inv_{biz_key}", use_container_width=True)
        if st.button("💾 Save Inventory", key=f"save_inv_{biz_key}"):
            data["business"][biz_key]["inventory"] = ed_inv.to_dict("records")
            save_data(data); st.success("✅ Saved!"); st.rerun()

        # Low stock alerts
        if not ed_inv.empty:
            low = ed_inv[ed_inv.apply(lambda r: safe_float(r.get("qty",0)) <= safe_float(r.get("reorder_at",0)) and safe_float(r.get("reorder_at",0)) > 0, axis=1)]
            if not low.empty:
                st.warning(f"⚠️ **{len(low)} item(s) at or below reorder level:**")
                for _, row in low.iterrows():
                    st.markdown(f"<div class='insight-card'>🔴 <b>{row.get('item','?')}</b> — {row.get('qty',0)} {row.get('unit','')} left (reorder at {row.get('reorder_at',0)})</div>", unsafe_allow_html=True)

    with btab4:
        st.subheader("📈 Business Analytics")
        # Monthly revenue trend from income entries
        income_data_all = b_data.get("income", [])
        if income_data_all:
            df_trend = pd.DataFrame(income_data_all)
            df_trend["date"] = pd.to_datetime(df_trend["date"], errors="coerce")
            df_trend["amount"] = df_trend["amount"].apply(safe_float)
            df_trend["month"] = df_trend["date"].dt.strftime("%Y-%m")
            monthly_rev = df_trend.groupby("month")["amount"].sum().reset_index()
            fig_trend = go.Figure(go.Bar(x=monthly_rev["month"], y=monthly_rev["amount"],
                marker_color=b_cfg["color"], opacity=0.85))
            fig_trend.update_layout(title="Monthly Revenue", height=220, plot_bgcolor="#080c14",
                paper_bgcolor="#080c14", font=dict(color="#94a3b8"),
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#475569", gridcolor="#1e293b"),
                margin=dict(l=0,r=0,t=30,b=0))
            st.plotly_chart(fig_trend, use_container_width=True)

        exp_all = b_data.get("expenses", [])
        if exp_all:
            df_exp_t = pd.DataFrame(exp_all)
            df_exp_t["amount"] = df_exp_t["amount"].apply(safe_float)
            df_exp_t["date"] = pd.to_datetime(df_exp_t["date"], errors="coerce")
            df_exp_t["month"] = df_exp_t["date"].dt.strftime("%Y-%m")
            monthly_exp = df_exp_t.groupby("month")["amount"].sum().reset_index()

            if income_data_all:
                combined_df = monthly_rev.merge(monthly_exp, on="month", how="outer", suffixes=("_rev","_exp")).fillna(0)
                combined_df["profit"] = combined_df["amount_rev"] - combined_df["amount_exp"]
                fig_pl = go.Figure()
                fig_pl.add_trace(go.Bar(name="Revenue",  x=combined_df["month"], y=combined_df["amount_rev"], marker_color=b_cfg["color"], opacity=0.7))
                fig_pl.add_trace(go.Bar(name="Expenses", x=combined_df["month"], y=combined_df["amount_exp"], marker_color="#ef4444", opacity=0.7))
                fig_pl.add_trace(go.Scatter(name="Net Profit", x=combined_df["month"], y=combined_df["profit"],
                    mode="lines+markers", line=dict(color="#22c55e", width=2.5), marker=dict(size=7)))
                fig_pl.update_layout(barmode="group", title="Revenue vs Expenses vs Profit", height=280,
                    plot_bgcolor="#080c14", paper_bgcolor="#080c14", font=dict(color="#94a3b8"),
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#475569", gridcolor="#1e293b"),
                    legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=0,r=0,t=30,b=0))
                st.plotly_chart(fig_pl, use_container_width=True)
        else:
            st.info("Add income and expense entries to see analytics.")


# ==========================================
# AGILE BOARD PAGE
# ==========================================
elif st.session_state['page'] == 'Agile':
    st.title("🗂️ Agile Task Board")
    st.caption("Senior Data Scientist — Pharma Retail | Daily standup, sprint tracking & task management")

    agile = data["agile"]
    today_dt_a = datetime.date.today()

    def safe_float(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0

    tasks    = agile.get("tasks", [])
    sprints  = agile.get("sprints", [])

    STATUSES = ["🆕 New", "🔄 In Progress", "🔍 In Review", "🧪 Testing", "✅ Done", "❌ Blocked"]
    PRIORITIES = ["🔴 Critical", "🟠 High", "🟡 Medium", "🟢 Low"]
    TAGS = ["EDA", "Model Training", "Feature Engineering", "Data Pipeline", "Dashboard",
            "Reporting", "Stakeholder", "Code Review", "Documentation", "Bug Fix", "Research", "Meeting"]

    # ── TODAY'S STANDUP VIEW ──
    with st.expander("📢 Today's Standup — What to Show Your Manager", expanded=True):
        today_str_a = today_dt_a.strftime("%Y-%m-%d")
        in_progress = [t for t in tasks if "In Progress" in t.get("status","")]
        done_today  = [t for t in tasks if "Done" in t.get("status","") and t.get("updated_date","") == today_str_a]
        blocked     = [t for t in tasks if "Blocked" in t.get("status","")]
        planned     = [t for t in tasks if t.get("planned_date","") == today_str_a and "Done" not in t.get("status","")]

        su_c1, su_c2, su_c3 = st.columns(3)
        with su_c1:
            st.markdown("**✅ Done Yesterday / Today**")
            if done_today:
                for t in done_today:
                    st.markdown(f"<div class='insight-card'>✅ {t.get('title','')}<br><span style='color:#475569;font-size:0.75rem;'>{t.get('tag','')}</span></div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='insight-card' style='color:#475569;'>Nothing marked done yet today</div>", unsafe_allow_html=True)

        with su_c2:
            st.markdown("**🔄 Working On Today**")
            items_today = in_progress + planned
            if items_today:
                for t in items_today[:5]:
                    st.markdown(f"<div class='insight-card'>🔄 {t.get('title','')}<br><span style='color:#475569;font-size:0.75rem;'>{t.get('tag','')} · {t.get('priority','')}</span></div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='insight-card' style='color:#475569;'>No tasks planned for today</div>", unsafe_allow_html=True)

        with su_c3:
            st.markdown("**❌ Blockers**")
            if blocked:
                for t in blocked:
                    blocker_note = t.get("blocker_note","")
                    st.markdown(f"<div class='insight-card' style='border-left-color:#ef4444;'>❌ {t.get('title','')}<br><span style='color:#ef4444;font-size:0.75rem;'>{blocker_note}</span></div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='insight-card' style='color:#22c55e;'>🟢 No blockers</div>", unsafe_allow_html=True)

        # Copy standup text
        standup_text = f"📅 Standup — {today_dt_a.strftime('%A, %b %d')}\n\n"
        standup_text += "✅ Done:\n" + ("\n".join(f"  • {t['title']}" for t in done_today) or "  • Nothing completed yet") + "\n\n"
        standup_text += "🔄 Today:\n" + ("\n".join(f"  • {t['title']}" for t in items_today[:5]) or "  • TBD") + "\n\n"
        standup_text += "❌ Blockers:\n" + ("\n".join(f"  • {t['title']}: {t.get('blocker_note','')}" for t in blocked) or "  • None")
        st.text_area("📋 Copy & paste to Slack/Teams:", value=standup_text, height=160, key="standup_copy")

    st.divider()

    # ── KANBAN BOARD ──
    st.markdown("### 🗂️ Kanban Board")
    status_groups = {}
    for s in STATUSES:
        status_groups[s] = [t for t in tasks if t.get("status","") == s]

    kanban_cols = st.columns(len(STATUSES))
    status_colors = {"🆕 New":"#1e3a5f","🔄 In Progress":"#1c3a1c","🔍 In Review":"#3a2a00",
                     "🧪 Testing":"#1a1a4a","✅ Done":"#0a2a1a","❌ Blocked":"#4a1a1a"}
    for idx, status in enumerate(STATUSES):
        with kanban_cols[idx]:
            s_tasks = status_groups[status]
            color = status_colors.get(status,"#0d1b2a")
            st.markdown(f"<div style='background:{color}; border-radius:8px; padding:8px; margin-bottom:6px; text-align:center; font-size:0.82rem; font-weight:600; color:#e2e8f0;'>{status} <span style='color:#64748b;'>({len(s_tasks)})</span></div>", unsafe_allow_html=True)
            for t in s_tasks:
                pri_colors = {"🔴 Critical":"#ef4444","🟠 High":"#fb923c","🟡 Medium":"#facc15","🟢 Low":"#22c55e"}
                pc = pri_colors.get(t.get("priority","🟢 Low"), "#475569")
                st.markdown(f"""
                <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-left:3px solid {pc}; border-radius:8px; padding:10px; margin-bottom:6px;'>
                    <div style='color:#e2e8f0; font-size:0.82rem; font-weight:600; margin-bottom:4px;'>{t.get("title","")}</div>
                    <div style='display:flex; gap:6px; flex-wrap:wrap;'>
                        <span style='background:#1e3a5f; color:#93c5fd; border-radius:4px; padding:1px 6px; font-size:0.65rem;'>{t.get("tag","")}</span>
                        <span style='color:#475569; font-size:0.65rem;'>SP:{t.get("story_points",1)}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)

    st.divider()

    # ── TASK MANAGEMENT ──
    task_tab1, task_tab2, task_tab3 = st.tabs(["📋 All Tasks", "➕ Add / Edit Task", "🏃 Sprint Planning"])

    with task_tab1:
        if tasks:
            # Filter controls
            fc1, fc2, fc3 = st.columns(3)
            with fc1: filter_status = st.multiselect("Filter by Status", STATUSES, default=[], key="f_status")
            with fc2: filter_pri = st.multiselect("Filter by Priority", PRIORITIES, default=[], key="f_pri")
            with fc3: filter_tag = st.multiselect("Filter by Tag", TAGS, default=[], key="f_tag")

            filtered = [t for t in tasks if
                (not filter_status or t.get("status","") in filter_status) and
                (not filter_pri    or t.get("priority","") in filter_pri) and
                (not filter_tag    or t.get("tag","") in filter_tag)]

            df_tasks = pd.DataFrame(filtered) if filtered else pd.DataFrame(tasks)
            display_cols = ["title","status","priority","tag","story_points","planned_date","updated_date","blocker_note"]
            display_cols = [c for c in display_cols if c in df_tasks.columns]
            st.dataframe(df_tasks[display_cols], use_container_width=True, hide_index=True)
        else:
            st.info("No tasks yet. Add your first task in the 'Add / Edit Task' tab.")

    with task_tab2:
        st.subheader("Add or Edit a Task")
        # Edit existing or create new
        task_titles = ["— Create New Task —"] + [t.get("title","") for t in tasks]
        selected_task_title = st.selectbox("Select task to edit, or create new:", task_titles, key="edit_task_sel")

        existing_task = None
        if selected_task_title != "— Create New Task —":
            existing_task = next((t for t in tasks if t.get("title","") == selected_task_title), None)

        with st.form("task_form"):
            tc1, tc2 = st.columns(2)
            with tc1:
                t_title = st.text_input("Task Title *", value=existing_task.get("title","") if existing_task else "")
                t_desc  = st.text_area("Description", value=existing_task.get("description","") if existing_task else "", height=80)
                t_tag   = st.selectbox("Tag/Type", TAGS,
                    index=TAGS.index(existing_task.get("tag", TAGS[0])) if existing_task and existing_task.get("tag") in TAGS else 0)
                t_sprint = st.text_input("Sprint", value=existing_task.get("sprint","") if existing_task else "")
            with tc2:
                t_status = st.selectbox("Status", STATUSES,
                    index=STATUSES.index(existing_task.get("status", STATUSES[0])) if existing_task and existing_task.get("status") in STATUSES else 0)
                t_priority = st.selectbox("Priority", PRIORITIES,
                    index=PRIORITIES.index(existing_task.get("priority", PRIORITIES[2])) if existing_task and existing_task.get("priority") in PRIORITIES else 2)
                t_sp = st.number_input("Story Points", min_value=1, max_value=21, value=int(safe_float(existing_task.get("story_points",1))) if existing_task else 1)
                t_planned = st.date_input("Planned Date", value=datetime.date.today(), key="t_planned_date")
                t_blocker = st.text_input("Blocker Note (if blocked)", value=existing_task.get("blocker_note","") if existing_task else "")

            sub_task = st.form_submit_button("💾 Save Task", use_container_width=True)
            if sub_task:
                if not t_title.strip():
                    st.warning("Task title is required.")
                else:
                    task_obj = {
                        "id": existing_task.get("id", str(int(time.time()*1000))) if existing_task else str(int(time.time()*1000)),
                        "title": t_title.strip(), "description": t_desc,
                        "status": t_status, "priority": t_priority, "tag": t_tag,
                        "story_points": t_sp, "sprint": t_sprint,
                        "planned_date": t_planned.strftime("%Y-%m-%d"),
                        "updated_date": today_dt_a.strftime("%Y-%m-%d"),
                        "blocker_note": t_blocker
                    }
                    if existing_task:
                        data["agile"]["tasks"] = [task_obj if t.get("id") == existing_task.get("id") else t for t in tasks]
                    else:
                        data["agile"]["tasks"].append(task_obj)
                    save_data(data)
                    st.success(f"✅ Task '{t_title}' saved!")
                    st.rerun()

        # Delete button
        if existing_task:
            if st.button("🗑️ Delete This Task", key="delete_task"):
                data["agile"]["tasks"] = [t for t in tasks if t.get("id") != existing_task.get("id")]
                save_data(data)
                st.success("Task deleted.")
                st.rerun()

    with task_tab3:
        st.subheader("🏃 Sprint Planning")
        sprint_name = st.text_input("Sprint Name", placeholder="e.g. Sprint 12 — April 2025")
        sprint_tasks = st.multiselect("Tasks in this sprint", [t.get("title","") for t in tasks], key="sprint_tasks")
        sprint_goal  = st.text_area("Sprint Goal", height=60)
        sprint_start = st.date_input("Start Date", key="s_start")
        sprint_end   = st.date_input("End Date", key="s_end")
        if st.button("💾 Save Sprint"):
            sprint_obj = {"name": sprint_name, "goal": sprint_goal,
                          "start": sprint_start.strftime("%Y-%m-%d"), "end": sprint_end.strftime("%Y-%m-%d"),
                          "tasks": sprint_tasks}
            data["agile"]["sprints"].append(sprint_obj)
            save_data(data)
            st.success(f"✅ Sprint '{sprint_name}' saved!")
            st.rerun()

        if sprints:
            st.divider()
            st.markdown("**Existing Sprints:**")
            for sp in sprints[-3:]:
                done_sp = sum(1 for t in tasks if t.get("title") in sp.get("tasks",[]) and "Done" in t.get("status",""))
                total_sp = len(sp.get("tasks",[]))
                pct_sp = (done_sp / total_sp * 100) if total_sp > 0 else 0
                st.markdown(f"""
                <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                    <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;'>
                        <span style='color:#e2e8f0; font-weight:600;'>{sp.get("name","")}</span>
                        <span style='color:#60a5fa; font-size:0.8rem; font-family:JetBrains Mono,monospace;'>{done_sp}/{total_sp} tasks · {pct_sp:.0f}%</span>
                    </div>
                    <div style='background:#1e293b; border-radius:4px; height:5px;'>
                        <div style='background:#22c55e; width:{min(pct_sp,100):.0f}%; height:5px; border-radius:4px;'></div>
                    </div>
                    <div style='color:#475569; font-size:0.75rem; margin-top:4px;'>{sp.get("start","")} → {sp.get("end","")}</div>
                </div>
                """, unsafe_allow_html=True)


# ==========================================
# AI FINANCIAL ADVISER PAGE
# ==========================================
elif st.session_state['page'] == 'AIAdviser':
    st.title("🤖 AI Financial Adviser")
    st.caption("Personalized advice powered by Gemini AI — linked to your Finance Hub and Credit Tracker data")

    import re as _re

    def safe_float(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0

    fin   = data["finance"]
    credit = data["credit"]
    today_dt_ai = datetime.date.today()
    BANKS_AI = {
        "QNB":    {"monthly_rate": 0.0435, "annual_rate": 0.522, "cash_adv_fee": 0.04, "grace_days": 57,
                   "install_rates": {6:0.0218,12:0.0218,18:0.0199,24:0.0181,36:0.0181}},
        "EGBank": {"monthly_rate": 0.03,   "annual_rate": 0.36,  "cash_adv_fee": 0.03, "grace_days": 57,
                   "install_rates": {6:0.03,12:0.03,18:0.03,24:0.03,36:0.03}},
    }

    # ── Build financial context snapshot ──
    def build_financial_context():
        # Income
        usd_rate = 50.0
        if data.get("price_history"):
            latest_ph = sorted(data["price_history"].keys())[-1]
            usd_rate = data["price_history"][latest_ph].get("usd", 50.0)
        income_items = fin.get("income", [])
        monthly_income_egp = 0
        for item in income_items:
            amt = safe_float(item.get("amount",0))
            curr = item.get("currency","EGP")
            monthly_income_egp += amt * usd_rate if curr == "USD" else amt

        # Expenses
        fixed_exp = sum(safe_float(e.get("amount",0)) for e in fin.get("expenses_monthly",[]))
        extra_exp = sum(safe_float(e.get("amount",0)) for e in fin.get("expenses_extra",[]))
        total_expenses = fixed_exp + extra_exp
        monthly_savings = monthly_income_egp - total_expenses
        savings_rate = (monthly_savings / monthly_income_egp * 100) if monthly_income_egp > 0 else 0

        # Assets
        assets_val = sum(safe_float(a.get("value",0)) for a in fin.get("assets",[]))

        # Credit
        transactions  = credit.get("transactions",[])
        install_plans = credit.get("installment_plans",[])
        limits = credit.get("limits", {"QNB":0,"EGBank":0})

        credit_summary = {}
        for bname, cfg in BANKS_AI.items():
            b_tx = [t for t in transactions if t.get("bank","QNB") == bname]
            spent   = sum(safe_float(t.get("amount",0)) for t in b_tx if t.get("type")=="Purchase")
            paid    = sum(safe_float(t.get("amount",0)) for t in b_tx if t.get("type")=="Payment")
            cash_a  = sum(safe_float(t.get("amount",0)) for t in b_tx if t.get("type")=="Cash Advance")
            inst_c  = sum(safe_float(p.get("purchase_amount",0)) for p in install_plans if p.get("bank","QNB")==bname)
            revolving = max(0, spent + cash_a - inst_c - paid)
            interest  = revolving * cfg["monthly_rate"]
            monthly_install = 0
            for plan in [p for p in install_plans if p.get("bank","QNB")==bname]:
                start = plan.get("start_month","")
                months_p = int(safe_float(plan.get("months",0)))
                monthly_amt = safe_float(plan.get("monthly_amount",0))
                if start and months_p > 0:
                    try:
                        start_dt = datetime.datetime.strptime(start, "%Y-%m")
                        elapsed = (today_dt_ai.year - start_dt.year)*12 + (today_dt_ai.month - start_dt.month)
                        if 0 <= elapsed < months_p:
                            monthly_install += monthly_amt
                    except: pass
            credit_summary[bname] = {"revolving": revolving, "interest": interest,
                                     "monthly_install": monthly_install, "limit": safe_float(limits.get(bname,0))}

        total_credit_burden = sum(v["revolving"] + v["interest"] + v["monthly_install"] for v in credit_summary.values())
        total_install_monthly = sum(v["monthly_install"] for v in credit_summary.values())

        return {
            "monthly_income_egp": monthly_income_egp,
            "fixed_expenses": fixed_exp, "total_expenses": total_expenses,
            "monthly_savings": monthly_savings, "savings_rate": savings_rate,
            "assets_total": assets_val,
            "credit_summary": credit_summary,
            "total_credit_burden": total_credit_burden,
            "total_install_monthly": total_install_monthly,
            "usd_rate": usd_rate,
            "income_sources": income_items,
        }

    ctx = build_financial_context()

    # ── FINANCIAL HEALTH DASHBOARD ──
    st.markdown("### 💡 Your Financial Snapshot")
    sh1, sh2, sh3, sh4 = st.columns(4)
    sh1.metric("Monthly Income",  f"{ctx['monthly_income_egp']:,.0f} EGP")
    sh2.metric("Monthly Savings", f"{ctx['monthly_savings']:,.0f} EGP", delta=f"{ctx['savings_rate']:.1f}% rate")
    sh3.metric("Total Credit Burden", f"{ctx['total_credit_burden']:,.0f} EGP/mo")
    debt_to_income = (ctx['total_credit_burden'] / ctx['monthly_income_egp'] * 100) if ctx['monthly_income_egp'] > 0 else 0
    sh4.metric("Debt-to-Income Ratio", f"{debt_to_income:.1f}%",
               delta="⚠️ High" if debt_to_income > 35 else "✅ Good")

    if debt_to_income > 35:
        st.error(f"⚠️ Your debt-to-income ratio is {debt_to_income:.1f}%. Above 35% increases financial risk.")
    elif debt_to_income > 20:
        st.warning(f"⚠️ Debt-to-income at {debt_to_income:.1f}%. Consider reducing credit spending.")
    else:
        st.success(f"✅ Healthy debt-to-income ratio: {debt_to_income:.1f}%")

    st.divider()

    # ── AI ADVISER TABS ──
    ai_t1, ai_t2, ai_t3 = st.tabs(["💬 Ask AI Adviser", "🛍️ Purchase Decision Helper", "🏦 Bank Offer Analyser"])

    # call_gemini is defined globally at top of file
    call_claude = call_gemini  # alias for backward compat in this page

    def build_profile_str(ctx):
        cs = ctx["credit_summary"]
        lines = [
            f"Monthly Income: {ctx['monthly_income_egp']:,.0f} EGP",
            f"Fixed Expenses: {ctx['fixed_expenses']:,.0f} EGP/month",
            f"Total Expenses: {ctx['total_expenses']:,.0f} EGP/month",
            f"Monthly Savings: {ctx['monthly_savings']:,.0f} EGP ({ctx['savings_rate']:.1f}% savings rate)",
            f"Total Assets: {ctx['assets_total']:,.0f} EGP",
            f"USD Rate: {ctx['usd_rate']:,.2f} EGP",
            f"QNB Card — Revolving: {cs['QNB']['revolving']:,.0f} EGP, Interest: {cs['QNB']['interest']:,.0f} EGP/mo, Installments: {cs['QNB']['monthly_install']:,.0f} EGP/mo, Limit: {cs['QNB']['limit']:,.0f} EGP",
            f"EGBank Card — Revolving: {cs['EGBank']['revolving']:,.0f} EGP, Interest: {cs['EGBank']['interest']:,.0f} EGP/mo, Installments: {cs['EGBank']['monthly_install']:,.0f} EGP/mo, Limit: {cs['EGBank']['limit']:,.0f} EGP",
            f"Total Monthly Credit Burden: {ctx['total_credit_burden']:,.0f} EGP",
            f"Debt-to-Income Ratio: {(ctx['total_credit_burden']/ctx['monthly_income_egp']*100) if ctx['monthly_income_egp']>0 else 0:.1f}%",
        ]
        return "\n".join(lines)

    with ai_t1:
        st.subheader("💬 Ask Your AI Financial Adviser")
        st.caption("Ask anything about your finances — the AI has full context of your income, expenses, and credit data.")

        if "ai_chat_history" not in st.session_state:
            st.session_state["ai_chat_history"] = []

        # Display chat history
        for msg in st.session_state["ai_chat_history"]:
            role_icon = "🧑" if msg["role"] == "user" else "🤖"
            bg = "#0d1b2a" if msg["role"] == "assistant" else "#111827"
            st.markdown(f"""
            <div style='background:{bg}; border:1px solid #1e3a5f; border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                <div style='color:#60a5fa; font-size:0.75rem; margin-bottom:4px;'>{role_icon} {"AI Adviser" if msg["role"]=="assistant" else "You"}</div>
                <div style='color:#e2e8f0; font-size:0.88rem; white-space:pre-wrap;'>{msg["content"]}</div>
            </div>
            """, unsafe_allow_html=True)

        user_q = st.text_area("Your question:", placeholder="e.g. Should I use my credit card for a 15,000 EGP purchase? Which card is better?", height=80, key="ai_question_input")
        if st.button("🚀 Ask Adviser", type="primary", key="ask_adviser"):
            if user_q.strip():
                profile = build_profile_str(ctx)
                full_prompt = f"""You are a smart personal financial adviser for an Egyptian professional.
You have full access to their financial data. Be specific, concise, and practical.
Respond in clear English. Use EGP amounts. Focus on actionable advice.

--- FINANCIAL PROFILE ---
{profile}
--- END PROFILE ---

User question: {user_q}"""
                with st.spinner("🤖 Thinking..."):
                    answer = call_claude(full_prompt)
                st.session_state["ai_chat_history"].append({"role":"user","content":user_q})
                st.session_state["ai_chat_history"].append({"role":"assistant","content":answer})
                st.rerun()

        if st.button("🗑️ Clear Chat", key="clear_chat"):
            st.session_state["ai_chat_history"] = []
            st.rerun()

    with ai_t2:
        st.subheader("🛍️ Purchase Decision Helper")
        st.caption("Enter a purchase you're considering and get an AI analysis of the best payment method.")

        pd_c1, pd_c2 = st.columns(2)
        with pd_c1:
            purchase_item  = st.text_input("What do you want to buy?", placeholder="e.g. Laptop, Car AC unit, TV")
            purchase_price = st.number_input("Price (EGP)", min_value=0.0, step=500.0, value=15000.0)
            purchase_urgency = st.selectbox("Urgency", ["Nice to have","Needed soon","Urgent/Required"])
            purchase_notes   = st.text_area("Any notes?", placeholder="e.g. found on installment with Jumia, 0% interest for 6 months", height=60)
        with pd_c2:
            st.markdown("<br>", unsafe_allow_html=True)
            available_cash = st.number_input("Available Cash Right Now (EGP)", min_value=0.0, step=500.0, value=0.0)
            consider_qnb   = st.checkbox("Consider QNB Card?",    value=True)
            consider_egbank= st.checkbox("Consider EGBank Card?", value=True)
            consider_cash  = st.checkbox("Pay Cash?",             value=True)

        if st.button("🤖 Analyse This Purchase", type="primary", key="analyse_purchase"):
            profile = build_profile_str(ctx)
            cs = ctx["credit_summary"]
            options_txt = []
            if consider_cash:   options_txt.append(f"Cash (available: {available_cash:,.0f} EGP)")
            if consider_qnb:    options_txt.append(f"QNB (revolving rate 4.35%/mo, available credit ~{max(0,cs['QNB']['limit']-cs['QNB']['revolving']):,.0f} EGP, installment options: 6/12/18/24/36m at 2.18-1.81%/mo)")
            if consider_egbank: options_txt.append(f"EGBank (revolving rate 3%/mo, available credit ~{max(0,cs['EGBank']['limit']-cs['EGBank']['revolving']):,.0f} EGP, installment 3%/mo all tenors)")

            full_prompt = f"""You are an expert personal financial adviser for an Egyptian professional.
Analyse this purchase decision and give a concrete recommendation.

--- FINANCIAL PROFILE ---
{profile}
--- END PROFILE ---

PURCHASE REQUEST:
- Item: {purchase_item}
- Price: {purchase_price:,.0f} EGP
- Urgency: {purchase_urgency}
- Notes: {purchase_notes if purchase_notes else "None"}

PAYMENT OPTIONS TO EVALUATE:
{chr(10).join(f"  {i+1}. {opt}" for i,opt in enumerate(options_txt))}

Please provide:
1. RECOMMENDATION (which option and why in 2 sentences)
2. COST BREAKDOWN (total cost for each option over 12 months)
3. IMPACT ON FINANCES (effect on monthly cash flow and savings rate)
4. RISK ASSESSMENT (1-2 sentences on financial risk)
5. FINAL VERDICT (short action sentence)

Be specific with EGP numbers. Focus on the person's actual financial situation."""

            with st.spinner("🤖 Analysing your purchase..."):
                analysis = call_claude(full_prompt, max_tokens=1500)
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:12px; padding:20px; margin-top:12px;'>
                <div style='color:#60a5fa; font-size:0.8rem; margin-bottom:10px;'>🤖 AI Analysis — {purchase_item}</div>
                <div style='color:#e2e8f0; font-size:0.88rem; white-space:pre-wrap; line-height:1.7;'>{analysis}</div>
            </div>""", unsafe_allow_html=True)

    with ai_t3:
        st.subheader("🏦 Bank Offer Analyser")
        st.caption("Got a special offer from QNB or EGBank? Paste the details and get an AI verdict.")

        of_c1, of_c2 = st.columns(2)
        with of_c1:
            offer_bank = st.selectbox("Which bank sent the offer?", ["QNB","EGBank","Other Bank"])
            offer_type = st.selectbox("Offer Type", ["Credit Limit Increase","0% Installment","Balance Transfer",
                                                      "Cashback/Rewards","Loan Offer","Reduced Interest Rate","Other"])
            offer_desc = st.text_area("Paste or describe the offer in detail:", height=120,
                placeholder="e.g. 0% installment on electronics for 12 months, no processing fee, via participating merchants...")
        with of_c2:
            offer_amount = st.number_input("Offer Amount / Value (EGP)", min_value=0.0, step=1000.0)
            offer_deadline = st.text_input("Offer Deadline / Validity", placeholder="e.g. Valid until May 31, 2025")
            offer_concern   = st.text_area("Any specific concern?", height=60,
                placeholder="e.g. I'm worried about hidden fees or impact on my credit score")

        if st.button("🤖 Evaluate This Offer", type="primary", key="eval_offer"):
            profile = build_profile_str(ctx)
            full_prompt = f"""You are a sharp personal financial adviser specialising in Egyptian banking products.
Evaluate this bank offer and give a clear recommendation.

--- FINANCIAL PROFILE ---
{profile}
--- END PROFILE ---

BANK OFFER DETAILS:
- Bank: {offer_bank}
- Offer Type: {offer_type}
- Amount/Value: {offer_amount:,.0f} EGP
- Deadline: {offer_deadline if offer_deadline else "Not specified"}
- Description: {offer_desc}
- User Concern: {offer_concern if offer_concern else "None"}

Please analyse:
1. IS THIS OFFER GOOD? (Yes / Conditional Yes / No — in one sentence)
2. WHAT'S IN IT FOR THE BANK? (hidden costs, catch, or fine print to watch)
3. HOW IT FITS YOUR SITUATION (based on their actual financial profile)
4. NEGOTIATION TIP (can they get a better deal?)
5. RECOMMENDATION (accept / decline / negotiate — specific action)

Be direct, honest, and protect the user's financial interests."""

            with st.spinner("🤖 Evaluating the offer..."):
                verdict = call_claude(full_prompt, max_tokens=1200)
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:12px; padding:20px; margin-top:12px;'>
                <div style='color:#a78bfa; font-size:0.8rem; margin-bottom:10px;'>🏦 Offer Verdict — {offer_bank} {offer_type}</div>
                <div style='color:#e2e8f0; font-size:0.88rem; white-space:pre-wrap; line-height:1.7;'>{verdict}</div>
            </div>""", unsafe_allow_html=True)



# ==========================================
# ⚡ AI REFLECTION JOURNAL (New Feature)
# ==========================================
elif st.session_state['page'] == 'Journal':
    st.title("✍️ AI Reflection Journal")
    st.caption("End your day with a Gemini-powered personal reflection. Build self-awareness, track growth, and never lose a thought.")

    journal_data = data.get("journal", {})
    today_j = datetime.date.today().strftime("%Y-%m-%d")
    today_entry = journal_data.get(today_j, {"text": "", "mood": 3, "wins": "", "challenges": "", "ai_reflection": ""})

    jc1, jc2 = st.columns([3, 2])
    with jc1:
        st.markdown("### 📝 Today's Entry")
        j_text = st.text_area("What happened today? (brain dump)", value=today_entry.get("text",""),
            height=140, key="journal_text", placeholder="Write freely — no judgment. What happened, what you felt, what you noticed...")
        j_wins = st.text_input("🏆 Today's win (big or small):", value=today_entry.get("wins",""), key="journal_wins",
            placeholder="e.g. Finished the presentation, had a great conversation, hit the gym")
        j_challenges = st.text_input("⚡ What challenged you today?", value=today_entry.get("challenges",""), key="journal_challenges",
            placeholder="e.g. Stayed up too late, procrastinated on task X")
        mood_val = st.select_slider("Overall Mood", [1,2,3,4,5], value=today_entry.get("mood",3),
            format_func=lambda x: ["😞 Rough","😕 Low","😐 Okay","😊 Good","🔥 Excellent"][x-1], key="journal_mood")

        sc1, sc2 = st.columns(2)
        with sc1:
            if st.button("💾 Save Entry", key="save_journal", use_container_width=True, type="primary"):
                if "journal" not in data: data["journal"] = {}
                data["journal"][today_j] = {
                    "text": j_text, "mood": mood_val, "wins": j_wins,
                    "challenges": j_challenges, "ai_reflection": today_entry.get("ai_reflection","")
                }
                save_data(data)
                st.success("✅ Journal saved!")
                st.rerun()
        with sc2:
            gemini_j = get_gemini_key()
            if gemini_j:
                if st.button("🤖 AI Reflection", key="ai_reflect", use_container_width=True):
                    if not j_text and not j_wins and not j_challenges:
                        st.warning("Write something first!")
                    else:
                        j_prompt = f"""You are a wise, warm personal coach and journaling partner.
A person shared their day with you:

What happened: {j_text or 'Not written'}
Today's win: {j_wins or 'None mentioned'}
Challenge: {j_challenges or 'None mentioned'}
Mood: {['Rough','Low','Okay','Good','Excellent'][mood_val-1]}

Provide a thoughtful 3-paragraph reflection:
1. Acknowledge their experience with empathy (1-2 sentences)
2. Find the hidden insight or lesson in their day (2-3 sentences)
3. One specific, actionable suggestion for tomorrow (1-2 sentences)

Tone: warm, honest, like a trusted mentor. Max 150 words."""
                        with st.spinner("🤖 Reflecting..."):
                            ai_r = call_gemini(j_prompt, max_tokens=1200, temperature=0.8)
                        if "journal" not in data: data["journal"] = {}
                        data["journal"][today_j] = {
                            "text": j_text, "mood": mood_val, "wins": j_wins,
                            "challenges": j_challenges, "ai_reflection": ai_r
                        }
                        save_data(data); st.rerun()
            else:
                st.info("Add Gemini key for AI reflections.")

    with jc2:
        st.markdown("### 📊 Your Journal")
        # Mood trend last 14 days
        mood_history = [(d, v.get("mood",0)) for d, v in journal_data.items() if v.get("mood",0) > 0]
        if mood_history:
            mood_history.sort()
            df_mood_j = pd.DataFrame(mood_history[-14:], columns=["Date","Mood"])
            df_mood_j["Date"] = pd.to_datetime(df_mood_j["Date"])
            fig_mj = go.Figure(go.Scatter(x=df_mood_j["Date"], y=df_mood_j["Mood"],
                mode="lines+markers", fill="tozeroy",
                line=dict(color="#a78bfa", width=2.5), marker=dict(size=7, color="#a78bfa"),
                fillcolor="rgba(167,139,250,0.1)"))
            fig_mj.update_layout(height=180, plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                xaxis=dict(color="#475569", showgrid=False),
                yaxis=dict(color="#a78bfa", range=[0,6], showgrid=True, gridcolor="#1e293b"),
                margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig_mj, use_container_width=True)
            avg_m = sum(v for _,v in mood_history[-7:])/min(7,len(mood_history))
            st.metric("7-day Avg Mood", f"{avg_m:.1f}/5")

        # Show today's AI reflection
        ai_ref = today_entry.get("ai_reflection","")
        if ai_ref:
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #a78bfa44; border-radius:10px; padding:14px; margin-top:8px;'>
                <div style='color:#a78bfa; font-size:0.75rem; margin-bottom:6px; font-weight:600;'>🤖 Today's AI Reflection</div>
                <div style='color:#e2e8f0; font-size:0.84rem; line-height:1.65;'>{ai_ref}</div>
            </div>""", unsafe_allow_html=True)

        # Past entries list
        st.markdown("**Recent Entries**")
        sorted_entries = sorted(journal_data.keys(), reverse=True)[:7]
        for d_key in sorted_entries:
            entry = journal_data[d_key]
            mood_icons = {1:"😞",2:"😕",3:"😐",4:"😊",5:"🔥"}
            m_icon = mood_icons.get(entry.get("mood",3),"😐")
            wins_short = (entry.get("wins","") or "No win logged")[:40]
            st.markdown(
                f"<div style='background:#0d1117; border-radius:8px; padding:8px 12px; margin-bottom:4px; "
                f"display:flex; justify-content:space-between; align-items:center;'>"
                f"<div><span style='color:#475569; font-size:0.75rem; font-family:JetBrains Mono,monospace;'>{d_key}</span>"
                f"<div style='color:#94a3b8; font-size:0.8rem;'>{wins_short}</div></div>"
                f"<span style='font-size:1.2rem;'>{m_icon}</span></div>",
                unsafe_allow_html=True)


# ==========================================
# 🔥 HABIT STREAK ARENA (New Feature)
# ==========================================
elif st.session_state['page'] == 'Streaks':
    st.title("🔥 Habit Streak Arena")
    st.caption("Gamify your consistency. Build unbreakable habits, track your longest streaks, compete with your past self.")

    streak_data = data.get("custom_streaks", [])
    today_s = datetime.date.today()
    today_s_str = today_s.strftime("%Y-%m-%d")

    def calc_streak_info(habit):
        check_ins = set(habit.get("check_ins", []))
        # Current streak
        streak = 0
        d = today_s
        while d.strftime("%Y-%m-%d") in check_ins:
            streak += 1
            d -= datetime.timedelta(days=1)
        # Longest streak
        if not check_ins:
            return {"current": 0, "longest": 0, "total": 0, "today_done": today_s_str in check_ins}
        sorted_days = sorted(check_ins)
        max_streak = cur = 1
        for i in range(1, len(sorted_days)):
            prev = datetime.datetime.strptime(sorted_days[i-1], "%Y-%m-%d").date()
            curr = datetime.datetime.strptime(sorted_days[i], "%Y-%m-%d").date()
            if (curr - prev).days == 1:
                cur += 1
                max_streak = max(max_streak, cur)
            else:
                cur = 1
        return {"current": streak, "longest": max(max_streak, streak), "total": len(check_ins), "today_done": today_s_str in check_ins}

    # ── QUICK CHECK-IN (top section) ──
    st.markdown("### ⚡ Today's Check-In")
    if streak_data:
        check_cols = st.columns(min(len(streak_data), 4))
        for i, habit in enumerate(streak_data):
            info = calc_streak_info(habit)
            with check_cols[i % 4]:
                is_done = info["today_done"]
                btn_color = habit.get("color", "#3b82f6")
                emoji = habit.get("emoji", "🎯")
                st.markdown(f"""
                <div style='background:{"#052e16" if is_done else "#0d1b2a"}; border:2px solid {btn_color if is_done else "#1e3a5f"};
                     border-radius:14px; padding:16px; text-align:center; margin-bottom:8px;'>
                    <div style='font-size:2rem;'>{emoji}</div>
                    <div style='color:#e2e8f0; font-weight:600; font-size:0.88rem; margin:4px 0;'>{habit["name"]}</div>
                    <div style='font-family:JetBrains Mono,monospace; font-size:1.5rem; color:{btn_color}; font-weight:700;'>{info["current"]} 🔥</div>
                    <div style='color:#475569; font-size:0.72rem;'>Best: {info["longest"]} · Total: {info["total"]}</div>
                </div>""", unsafe_allow_html=True)
                btn_label = "✅ Done!" if is_done else f"Check In"
                btn_type = "primary" if not is_done else "secondary"
                if st.button(btn_label, key=f"checkin_{i}", use_container_width=True):
                    if not is_done:
                        if "check_ins" not in data["custom_streaks"][i]:
                            data["custom_streaks"][i]["check_ins"] = []
                        data["custom_streaks"][i]["check_ins"].append(today_s_str)
                    else:
                        data["custom_streaks"][i]["check_ins"] = [c for c in data["custom_streaks"][i]["check_ins"] if c != today_s_str]
                    save_data(data); st.rerun()
    else:
        st.info("Create your first habit below to start tracking streaks!")

    st.divider()

    # ── ADD NEW HABIT ──
    with st.expander("➕ Add New Habit", expanded=not bool(streak_data)):
        hc1, hc2, hc3 = st.columns(3)
        with hc1:
            new_habit_name = st.text_input("Habit Name", placeholder="e.g. Morning Run", key="new_streak_name")
        with hc2:
            new_habit_emoji = st.text_input("Emoji", value="🎯", max_chars=2, key="new_streak_emoji")
        with hc3:
            new_habit_color = st.color_picker("Color", value="#3b82f6", key="new_streak_color")
        new_habit_target = st.number_input("Daily target (optional, e.g. 30 min)", min_value=0, value=0, key="new_streak_target")
        new_habit_unit = st.text_input("Unit (e.g. minutes, pages, km)", value="", key="new_streak_unit")

        if st.button("➕ Create Habit", type="primary", key="create_habit") and new_habit_name:
            if "custom_streaks" not in data: data["custom_streaks"] = []
            data["custom_streaks"].append({
                "name": new_habit_name.strip(),
                "emoji": new_habit_emoji or "🎯",
                "color": new_habit_color,
                "target": new_habit_target,
                "unit": new_habit_unit,
                "check_ins": [],
                "created": today_s_str
            })
            save_data(data); st.success(f"✅ '{new_habit_name}' added!"); st.rerun()

    # ── LEADERBOARD ── (your streaks ranked)
    if streak_data:
        st.divider()
        st.markdown("### 🏆 Your Streak Leaderboard")
        lb_rows = []
        for habit in streak_data:
            info = calc_streak_info(habit)
            lb_rows.append({
                "Habit": habit.get("emoji","🎯") + " " + habit["name"],
                "Current 🔥": info["current"],
                "Best Ever": info["longest"],
                "Total Days": info["total"],
                "Status": "✅ Done" if info["today_done"] else "⬜ Not done"
            })
        lb_rows.sort(key=lambda x: x["Current 🔥"], reverse=True)
        for rank, row in enumerate(lb_rows):
            medal = ["🥇","🥈","🥉"][rank] if rank < 3 else f"#{rank+1}"
            c = "#22c55e" if "Done" in row["Status"] else "#475569"
            st.markdown(
                f"<div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:10px; "
                f"padding:12px 16px; margin-bottom:6px; display:flex; justify-content:space-between; align-items:center;'>"
                f"<div><span style='font-size:1.1rem;'>{medal}</span>"
                f"<span style='color:#e2e8f0; font-weight:600; font-size:0.9rem; margin-left:8px;'>{row['Habit']}</span></div>"
                f"<div style='display:flex; gap:20px; font-family:JetBrains Mono,monospace; font-size:0.85rem;'>"
                f"<span style='color:#f97316;'>{row["Current 🔥"]}🔥</span>"
                f"<span style='color:#facc15;'>Best: {row['Best Ever']}</span>"
                f"<span style='color:{c};'>{row['Status']}</span></div></div>",
                unsafe_allow_html=True)

        # Manage habits
        st.divider()
        with st.expander("🗑️ Manage Habits"):
            del_name = st.selectbox("Delete a habit", ["—"] + [h["name"] for h in streak_data], key="del_habit_sel")
            if st.button("🗑️ Delete", key="del_habit_btn") and del_name != "—":
                data["custom_streaks"] = [h for h in data["custom_streaks"] if h["name"] != del_name]
                save_data(data); st.success(f"Deleted '{del_name}'"); st.rerun()


# ==========================================
# 🍽️ AI CHEF — MEALS TRACKER
# ==========================================
elif st.session_state['page'] == 'Chef':
    import random as _rnd

    st.title("🍽️ AI Chef & Meals Tracker")
    st.caption("Track your meals, discover new recipes, and get AI-powered daily meal suggestions tailored to your taste.")

    meals_data   = data.get("meals", {"log": [], "favorites": [], "custom_meals": []})
    today_chef   = datetime.date.today()
    today_chef_s = today_chef.strftime("%Y-%m-%d")

    def sf(v):
        try: return float(v) if v else 0.0
        except: return 0.0

    chef_t1, chef_t2, chef_t3, chef_t4 = st.tabs(
        ["📅 Today's Plan", "📓 Meal Log", "⭐ Favorites", "➕ My Meals"])

    # ── TAB 1: TODAY'S AI PLAN ──
    with chef_t1:
        st.markdown("### 🌅 Today's Meal Plan")
        today_log = [m for m in meals_data.get("log", []) if m.get("date") == today_chef_s]
        slots = {"Breakfast":"🌅","Lunch":"☀️","Dinner":"🌙","Snack":"🍎"}

        logged_slots = {m.get("meal_type"): m for m in today_log}

        c1, c2 = st.columns([2, 1])
        with c1:
            for slot, icon in slots.items():
                meal = logged_slots.get(slot)
                if meal:
                    cal_str = f" · {meal.get('calories',0)} kcal" if meal.get('calories') else ""
                    st.markdown(
                        f"<div style='background:#052e16;border:1px solid #16a34a;border-radius:10px;"
                        f"padding:12px 16px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;'>"
                        f"<div><span style='font-size:1.1rem;'>{icon}</span>"
                        f"<span style='color:#e2e8f0;font-weight:600;margin-left:8px;'>{slot}</span>"
                        f"<span style='color:#22c55e;margin-left:8px;font-size:0.85rem;'>{meal.get('name','')}</span>"
                        f"<span style='color:#475569;font-size:0.75rem;'>{cal_str}</span></div>"
                        f"<span style='color:#22c55e;font-size:0.8rem;'>✅ Logged</span></div>",
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f"<div style='background:#0d1b2a;border:1px solid #1e3a5f;border-radius:10px;"
                        f"padding:12px 16px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;'>"
                        f"<div><span style='font-size:1.1rem;'>{icon}</span>"
                        f"<span style='color:#64748b;margin-left:8px;'>{slot} — not logged yet</span></div>"
                        f"</div>", unsafe_allow_html=True)

        with c2:
            # Nutrition summary
            total_cal  = sum(sf(m.get("calories",0)) for m in today_log)
            total_prot = sum(sf(m.get("protein_g",0)) for m in today_log)
            total_carb = sum(sf(m.get("carbs_g",0)) for m in today_log)
            st.markdown(f"""
            <div style='background:#0d1b2a;border:1px solid #1e3a5f;border-radius:12px;padding:16px;'>
                <div style='color:#60a5fa;font-size:0.8rem;font-weight:600;margin-bottom:10px;'>📊 Today's Nutrition</div>
                <div style='font-family:JetBrains Mono,monospace;'>
                    <div style='display:flex;justify-content:space-between;margin-bottom:6px;'>
                        <span style='color:#475569;font-size:0.8rem;'>Calories</span>
                        <span style='color:#f97316;font-weight:700;'>{total_cal:.0f} kcal</span>
                    </div>
                    <div style='display:flex;justify-content:space-between;margin-bottom:6px;'>
                        <span style='color:#475569;font-size:0.8rem;'>Protein</span>
                        <span style='color:#22c55e;'>{total_prot:.0f} g</span>
                    </div>
                    <div style='display:flex;justify-content:space-between;'>
                        <span style='color:#475569;font-size:0.8rem;'>Carbs</span>
                        <span style='color:#60a5fa;'>{total_carb:.0f} g</span>
                    </div>
                </div>
            </div>""", unsafe_allow_html=True)

        st.divider()
        st.markdown("### 🤖 AI Meal Suggestions")
        gemini_chef = get_gemini_key()
        if not gemini_chef:
            st.info("🔑 Add your Gemini API key in the sidebar to unlock AI meal suggestions.")
        else:
            pref_col, btn_col = st.columns([3, 1])
            with pref_col:
                diet_pref = st.multiselect("Dietary preferences",
                    ["No restrictions","Halal","Vegetarian","Low-carb","High-protein",
                     "Mediterranean","Dairy-free","Gluten-free"],
                    default=["No restrictions"], key="diet_pref")
                dislike = st.text_input("Ingredients to avoid", placeholder="e.g. mushrooms, olives",
                    key="chef_dislike")
                which_meal = st.selectbox("Suggest for", ["All meals","Breakfast","Lunch","Dinner","Snack"],
                    key="chef_which_meal")
            with btn_col:
                st.markdown("<br>", unsafe_allow_html=True)
                gen_meals_btn = st.button("✨ Suggest Meals", key="gen_meals_btn",
                    type="primary", use_container_width=True)

            if gen_meals_btn:
                favs = [m.get("name","") for m in meals_data.get("favorites",[])]
                fav_str = ", ".join(favs[:5]) if favs else "none yet"
                prompt = f"""You are a friendly personal chef. Suggest today's meal plan for an Egyptian person.

Dietary preferences: {", ".join(diet_pref)}
Foods to avoid: {dislike or "none"}
Favourite meals they enjoy: {fav_str}
Suggest for: {which_meal}

For each meal provide:
- Meal name
- 3-4 main ingredients
- Preparation time
- Approximate calories
- One sentence why it's a good choice today

Format each meal clearly with emoji. Keep suggestions practical, delicious, and realistic for an Egyptian kitchen.
If suggesting Egyptian dishes, include them — they are always welcome."""
                with st.spinner("🤖 Preparing your meal plan..."):
                    suggestion = call_gemini(prompt, max_tokens=2500, temperature=0.8)
                st.markdown(f"""
                <div style='background:#0d1b2a;border:1px solid #22c55e44;border-radius:12px;padding:18px;margin-top:8px;'>
                    <div style='color:#22c55e;font-size:0.8rem;font-weight:600;margin-bottom:10px;'>🤖 AI Chef's Recommendations</div>
                    <div style='color:#e2e8f0;font-size:0.87rem;white-space:pre-wrap;line-height:1.7;'>{suggestion}</div>
                </div>""", unsafe_allow_html=True)

    # ── TAB 2: MEAL LOG ──
    with chef_t2:
        st.markdown("### 📓 Log a Meal")
        with st.form("log_meal_form"):
            lc1, lc2, lc3 = st.columns(3)
            with lc1:
                log_name  = st.text_input("Meal Name *", placeholder="e.g. Grilled Chicken with Rice")
                log_type  = st.selectbox("Meal Type", ["Breakfast","Lunch","Dinner","Snack"])
                log_date  = st.date_input("Date", value=today_chef, key="log_meal_date")
            with lc2:
                log_cal   = st.number_input("Calories (kcal)", min_value=0, value=0, step=10)
                log_prot  = st.number_input("Protein (g)", min_value=0.0, value=0.0, step=1.0)
                log_carb  = st.number_input("Carbs (g)", min_value=0.0, value=0.0, step=1.0)
            with lc3:
                log_fat   = st.number_input("Fat (g)", min_value=0.0, value=0.0, step=1.0)
                log_ingr  = st.text_area("Ingredients", placeholder="Chicken, Rice, Tomato...", height=80)
                log_notes = st.text_input("Notes", placeholder="How was it?")
            log_fav = st.checkbox("⭐ Add to Favorites")
            save_meal_btn = st.form_submit_button("💾 Log Meal", use_container_width=True)

            if save_meal_btn and log_name.strip():
                entry = {
                    "date": log_date.strftime("%Y-%m-%d"),
                    "name": log_name.strip(),
                    "meal_type": log_type,
                    "calories": log_cal,
                    "protein_g": log_prot,
                    "carbs_g": log_carb,
                    "fat_g": log_fat,
                    "ingredients": log_ingr,
                    "notes": log_notes,
                    "logged_at": datetime.datetime.now().isoformat()
                }
                data["meals"]["log"].append(entry)
                if log_fav:
                    existing_favs = [f.get("name","") for f in data["meals"]["favorites"]]
                    if log_name.strip() not in existing_favs:
                        data["meals"]["favorites"].append({"name": log_name.strip(),
                            "meal_type": log_type, "calories": log_cal, "ingredients": log_ingr})
                save_data(data)
                st.success(f"✅ '{log_name}' logged!")
                st.rerun()

        st.divider()
        st.markdown("### 📅 Recent Meals")
        log_all = data.get("meals",{}).get("log",[])
        if log_all:
            df_log = pd.DataFrame(sorted(log_all, key=lambda x: x.get("date",""), reverse=True)[:30])
            show_cols = [c for c in ["date","meal_type","name","calories","protein_g","notes"] if c in df_log.columns]
            st.dataframe(df_log[show_cols], use_container_width=True, hide_index=True)

            # AI ingredient suggestions
            if get_gemini_key():
                st.divider()
                st.markdown("#### 🛒 AI Shopping List")
                if st.button("🤖 Generate Shopping List from Recent Meals", key="gen_shopping"):
                    recent_names = [m.get("name","") for m in log_all[-10:]]
                    p = f"Based on these recent meals: {', '.join(recent_names)}, generate a clean, grouped shopping list with sections (Proteins, Vegetables, Grains, Dairy, Other). Keep it concise and practical."
                    with st.spinner("Building list..."):
                        shop = call_gemini(p, max_tokens=1500)
                    st.markdown(f"<div style='background:#0d1b2a;border:1px solid #3b82f6;border-radius:10px;padding:14px;white-space:pre-wrap;color:#e2e8f0;font-size:0.86rem;'>{shop}</div>", unsafe_allow_html=True)
        else:
            st.info("No meals logged yet. Use the form above to start tracking.")

    # ── TAB 3: FAVORITES ──
    with chef_t3:
        st.markdown("### ⭐ Favorite Meals")
        favs = data.get("meals",{}).get("favorites",[])
        if favs:
            for i, fav in enumerate(favs):
                fc1, fc2 = st.columns([5,1])
                with fc1:
                    cal_s = f" · {fav.get('calories',0)} kcal" if fav.get('calories') else ""
                    st.markdown(
                        f"<div style='background:#0d1b2a;border:1px solid #facc1544;border-radius:8px;"
                        f"padding:10px 14px;margin-bottom:6px;display:flex;justify-content:space-between;'>"
                        f"<div><span style='font-size:1.1rem;'>⭐</span>"
                        f"<span style='color:#e2e8f0;font-weight:600;margin-left:8px;'>{fav.get('name','')}</span>"
                        f"<span style='color:#475569;margin-left:8px;font-size:0.8rem;'>{fav.get('meal_type','')}{cal_s}</span></div>"
                        f"</div>", unsafe_allow_html=True)
                with fc2:
                    if st.button("🗑️", key=f"del_fav_{i}"):
                        data["meals"]["favorites"].pop(i)
                        save_data(data); st.rerun()

            # Get AI recipe for a favorite
            if get_gemini_key():
                st.divider()
                sel_fav = st.selectbox("Get full recipe for:", [f.get("name","") for f in favs], key="sel_fav_recipe")
                if st.button("🍳 Get Full Recipe", key="get_recipe_btn"):
                    fav_detail = next((f for f in favs if f.get("name") == sel_fav), {})
                    p = f"Give me the full recipe for '{sel_fav}'. Include: ingredients with quantities for 2 servings, step-by-step instructions, cooking time, and a nutrition estimate per serving. Make it practical and clear."
                    with st.spinner("Preparing recipe..."):
                        recipe = call_gemini(p, max_tokens=2000)
                    st.markdown(f"<div style='background:#0d1b2a;border:1px solid #facc1544;border-radius:10px;padding:16px;white-space:pre-wrap;color:#e2e8f0;font-size:0.86rem;line-height:1.7;'>{recipe}</div>", unsafe_allow_html=True)
        else:
            st.info("No favorites yet. Log a meal and check '⭐ Add to Favorites'.")

    # ── TAB 4: CUSTOM MEALS ──
    with chef_t4:
        st.markdown("### ➕ My Custom Meals Library")
        st.caption("Save your own recipes to quickly log them later.")
        with st.form("custom_meal_form"):
            cm1, cm2 = st.columns(2)
            with cm1:
                cm_name  = st.text_input("Meal Name *", key="cm_name")
                cm_type  = st.selectbox("Type", ["Breakfast","Lunch","Dinner","Snack"], key="cm_type")
                cm_cal   = st.number_input("Avg Calories", min_value=0, value=0, key="cm_cal")
            with cm2:
                cm_ingr  = st.text_area("Ingredients", height=80, key="cm_ingr",
                    placeholder="List main ingredients...")
                cm_recipe= st.text_area("Recipe / Instructions", height=80, key="cm_recipe",
                    placeholder="Quick preparation notes...")
            save_cm = st.form_submit_button("💾 Save Custom Meal", use_container_width=True)
            if save_cm and cm_name.strip():
                data["meals"]["custom_meals"].append({
                    "name": cm_name.strip(), "meal_type": cm_type,
                    "calories": cm_cal, "ingredients": cm_ingr, "recipe": cm_recipe
                })
                save_data(data); st.success("✅ Saved!"); st.rerun()

        cm_list = data.get("meals",{}).get("custom_meals",[])
        if cm_list:
            st.divider()
            for i, cm in enumerate(cm_list):
                with st.expander(f"🍽️ {cm.get('name','')} · {cm.get('meal_type','')} · {cm.get('calories',0)} kcal"):
                    st.markdown(f"**Ingredients:** {cm.get('ingredients','—')}")
                    st.markdown(f"**Recipe:** {cm.get('recipe','—')}")
                    if st.button("🗑️ Delete", key=f"del_cm_{i}"):
                        data["meals"]["custom_meals"].pop(i)
                        save_data(data); st.rerun()


# ==========================================
# 🌍 LANGUAGE LAB
# ==========================================
elif st.session_state['page'] == 'Language':
    import random as _rnd2

    st.title("🌍 Language Lab")
    st.caption("Learn, practice, and track your daily language progress — powered by AI.")

    lang_data = data.get("language", {"sessions":[],"vocab_learned":[],"quiz_history":[],"settings":{}})
    if "settings" not in lang_data: lang_data["settings"] = {}
    if "sessions" not in lang_data: lang_data["sessions"] = []
    if "vocab_learned" not in lang_data: lang_data["vocab_learned"] = []
    if "quiz_history" not in lang_data: lang_data["quiz_history"] = []

    today_lang = datetime.date.today()
    today_lang_s = today_lang.strftime("%Y-%m-%d")

    # Language settings
    settings = lang_data.get("settings", {})
    target_lang = settings.get("target_language", "German")
    native_lang = settings.get("native_language", "Arabic")
    level = settings.get("level", "Beginner (A1)")

    lang_t1, lang_t2, lang_t3, lang_t4, lang_t5 = st.tabs(
        ["🎯 Daily Practice", "📖 Vocabulary", "🧠 Quiz", "📝 Session Log", "⚙️ Settings"])

    # ── TAB 1: DAILY PRACTICE ──
    with lang_t1:
        st.markdown(f"### 🎯 {target_lang} Daily Practice — {today_lang.strftime('%A, %b %d')}")

        # Streak
        sessions = lang_data.get("sessions", [])
        streak_l = 0
        for i in range(30):
            d_check = (today_lang - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if any(s.get("date") == d_check for s in sessions):
                streak_l += 1
            elif i > 0:
                break

        lm1, lm2, lm3 = st.columns(3)
        lm1.metric("Current Streak", f"{streak_l} 🔥 days")
        lm2.metric("Words Learned", len(lang_data.get("vocab_learned",[])))
        lm3.metric("Sessions Done", len([s for s in sessions if s.get("date","").startswith(today_lang.strftime("%Y-%m"))]))

        st.divider()
        gemini_lang = get_gemini_key()
        if not gemini_lang:
            st.info("🔑 Add your Gemini API key in the sidebar to unlock AI language practice.")
        else:
            practice_type = st.radio("What do you want to practice?",
                ["🆕 New Words & Phrases","💬 Useful Sentences","📚 Grammar Tip","🗣️ Conversation Starter","✍️ Writing Prompt"],
                key="practice_type", horizontal=True)

            if st.button("✨ Generate Practice", key="gen_practice", type="primary"):
                vocab_done = [v.get("word","") for v in lang_data.get("vocab_learned",[])[-20:]]
                p = f"""You are an expert {target_lang} language teacher.
The student speaks {native_lang} natively and is at {level} level in {target_lang}.
Words they already know (avoid repeating): {", ".join(vocab_done) if vocab_done else "none yet"}

Practice type requested: {practice_type.split(" ",1)[1]}

{"Provide 8-10 new vocabulary words with: the word in " + target_lang + ", its " + native_lang + " translation, pronunciation guide (romanized), and one example sentence. Format clearly." if "New Words" in practice_type else ""}
{"Provide 6-8 practical sentences for everyday use. Show: " + target_lang + " sentence, " + native_lang + " translation, and when to use it." if "Sentences" in practice_type else ""}
{"Explain one important grammar rule for " + level + " level with 3 clear examples." if "Grammar" in practice_type else ""}
{"Give 3 interesting conversation starter questions in " + target_lang + " with translations and suggested responses." if "Conversation" in practice_type else ""}
{"Give a short writing prompt for " + level + " with a model answer in " + target_lang + " and translation." if "Writing" in practice_type else ""}

Keep it engaging, practical, and appropriate for {level} level."""
                with st.spinner(f"🤖 Preparing {target_lang} practice..."):
                    practice_content = call_gemini(p, max_tokens=2500, temperature=0.75)
                st.session_state["last_practice"] = practice_content
                st.session_state["last_practice_type"] = practice_type

            if "last_practice" in st.session_state:
                st.markdown(f"""
                <div style='background:#0d1b2a;border:1px solid #3b82f644;border-radius:12px;padding:18px;margin-top:8px;'>
                    <div style='color:#60a5fa;font-size:0.8rem;font-weight:600;margin-bottom:10px;'>
                        📖 {st.session_state.get("last_practice_type","Practice")} — {target_lang} · {level}
                    </div>
                    <div style='color:#e2e8f0;font-size:0.87rem;white-space:pre-wrap;line-height:1.8;'>{st.session_state["last_practice"]}</div>
                </div>""", unsafe_allow_html=True)

                if st.button("✅ Mark as Done — Add to Session", key="mark_practice_done"):
                    sessions.append({
                        "date": today_lang_s,
                        "type": st.session_state.get("last_practice_type",""),
                        "language": target_lang,
                        "level": level,
                        "content_preview": st.session_state["last_practice"][:100]
                    })
                    data["language"]["sessions"] = sessions
                    save_data(data)
                    del st.session_state["last_practice"]
                    st.success("✅ Session logged!")
                    st.rerun()

    # ── TAB 2: VOCABULARY ──
    with lang_t2:
        st.markdown(f"### 📖 {target_lang} Vocabulary Tracker")

        vc1, vc2 = st.columns([3, 2])
        with vc1:
            # Add word manually
            with st.form("add_vocab_form"):
                av1, av2, av3 = st.columns(3)
                with av1:
                    v_word = st.text_input(f"{target_lang} Word", key="v_word")
                    v_trans = st.text_input(f"{native_lang} Translation", key="v_trans")
                with av2:
                    v_pronun = st.text_input("Pronunciation", key="v_pronun", placeholder="romanized")
                    v_category = st.selectbox("Category",
                        ["Vocabulary","Phrase","Verb","Adjective","Number","Expression","Other"], key="v_cat")
                with av3:
                    v_example = st.text_area("Example Sentence", height=80, key="v_example")
                add_v = st.form_submit_button("➕ Add Word", use_container_width=True)
                if add_v and v_word.strip():
                    data["language"]["vocab_learned"].append({
                        "date": today_lang_s,
                        "word": v_word.strip(),
                        "translation": v_trans,
                        "pronunciation": v_pronun,
                        "category": v_category,
                        "example": v_example,
                        "review_count": 0
                    })
                    save_data(data); st.success(f"✅ '{v_word}' added!"); st.rerun()

            # AI-generate a word set
            if get_gemini_key():
                ai_topic = st.text_input(f"AI: Generate vocabulary about...",
                    placeholder="e.g. food, travel, work, family", key="ai_vocab_topic")
                if st.button("🤖 Generate Word Set", key="gen_vocab_btn") and ai_topic:
                    p = (f"Generate 10 essential {target_lang} vocabulary words about '{ai_topic}' for a {level} student. "
                         f"For each: {target_lang} word | {native_lang} translation | pronunciation (romanized) | one short example sentence. "
                         f"Format as a clean list, one word per line with | separator.")
                    with st.spinner("Generating words..."):
                        word_set = call_gemini(p, max_tokens=1500)
                    # Parse and auto-add
                    added = 0
                    for line in word_set.strip().split("\n"):
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 2 and parts[0]:
                            existing = [v.get("word","") for v in data["language"]["vocab_learned"]]
                            if parts[0] not in existing:
                                data["language"]["vocab_learned"].append({
                                    "date": today_lang_s,
                                    "word": parts[0],
                                    "translation": parts[1] if len(parts)>1 else "",
                                    "pronunciation": parts[2] if len(parts)>2 else "",
                                    "category": "Vocabulary",
                                    "example": parts[3] if len(parts)>3 else "",
                                    "review_count": 0
                                })
                                added += 1
                    save_data(data)
                    st.success(f"✅ Added {added} new words!")
                    st.rerun()

        with vc2:
            # Flashcard review
            vocab = data.get("language",{}).get("vocab_learned",[])
            if vocab:
                st.markdown("**🃏 Flashcard Review**")
                if "fc_idx" not in st.session_state:
                    st.session_state["fc_idx"] = _rnd2.randint(0, len(vocab)-1)
                if "fc_show" not in st.session_state:
                    st.session_state["fc_show"] = False

                idx = st.session_state["fc_idx"] % len(vocab)
                card = vocab[idx]
                flip_color = "#052e16" if st.session_state["fc_show"] else "#0d1b2a"
                flip_border = "#22c55e" if st.session_state["fc_show"] else "#1e3a5f"
                st.markdown(
                    f"<div style='background:{flip_color};border:2px solid {flip_border};"
                    f"border-radius:14px;padding:24px;text-align:center;min-height:140px;'>"
                    f"<div style='font-size:1.6rem;font-weight:700;color:#e2e8f0;'>{card.get('word','')}</div>"
                    f"{'<div style="color:#22c55e;font-size:1.1rem;margin-top:10px;">'+card.get('translation','')+"</div><div style='color:#475569;font-size:0.8rem;margin-top:4px;'>🗣 "+card.get('pronunciation','')+"</div><div style='color:#94a3b8;font-size:0.78rem;margin-top:8px;'>" + card.get('example','') + "</div>" if st.session_state['fc_show'] else "<div style='color:#475569;margin-top:14px;'>Tap to reveal translation</div>"}"
                    f"</div>", unsafe_allow_html=True)

                fc1, fc2, fc3 = st.columns(3)
                with fc1:
                    if st.button("👁️ Flip", key="flip_card", use_container_width=True):
                        st.session_state["fc_show"] = not st.session_state["fc_show"]
                        st.rerun()
                with fc2:
                    if st.button("⬅️ Prev", key="fc_prev", use_container_width=True):
                        st.session_state["fc_idx"] = (idx - 1) % len(vocab)
                        st.session_state["fc_show"] = False; st.rerun()
                with fc3:
                    if st.button("Next ➡️", key="fc_next", use_container_width=True):
                        st.session_state["fc_idx"] = (idx + 1) % len(vocab)
                        st.session_state["fc_show"] = False; st.rerun()

        # Full vocabulary table
        vocab = data.get("language",{}).get("vocab_learned",[])
        if vocab:
            st.divider()
            st.markdown(f"**📚 My {target_lang} Vocabulary Bank ({len(vocab)} words)**")
            df_vocab = pd.DataFrame(vocab)
            show_vcols = [c for c in ["date","word","translation","pronunciation","category","example"] if c in df_vocab.columns]
            st.dataframe(df_vocab[show_vcols].sort_values("date", ascending=False), use_container_width=True, hide_index=True)

    # ── TAB 3: QUIZ ──
    with lang_t3:
        st.markdown(f"### 🧠 {target_lang} Quiz")
        vocab_for_quiz = data.get("language",{}).get("vocab_learned",[])

        if not get_gemini_key():
            st.info("🔑 Add your Gemini API key for AI-powered quizzes.")
        elif len(vocab_for_quiz) < 3:
            st.info(f"Add at least 3 words to your vocabulary bank first, then come back to quiz yourself!")
        else:
            quiz_type = st.radio("Quiz Type",
                ["Translation (→ target)","Translation (→ native)","Fill in the Blank","Multiple Choice"],
                key="quiz_type", horizontal=True)

            if st.button("🎲 Generate New Quiz", key="gen_quiz", type="primary"):
                sample = _rnd2.sample(vocab_for_quiz, min(5, len(vocab_for_quiz)))
                words_str = "\n".join([f"- {w.get('word','')} = {w.get('translation','')}" for w in sample])
                p = (
                    f"Create a {quiz_type} quiz in {target_lang} for a {level} student. "
                    f"Use these words from their vocabulary bank:\n{words_str}\n\n"
                    f"Generate 5 questions. For each question: show the question clearly, "
                    f"provide 4 answer choices (A, B, C, D) for multiple choice, "
                    f"and put the correct answer at the end as: ANSWER: [letter or word]. "
                    f"Keep it encouraging and appropriate for {level} level."
                )
                with st.spinner("Generating quiz..."):
                    quiz_content = call_gemini(p, max_tokens=2000)
                st.session_state["active_quiz"] = quiz_content
                st.session_state["quiz_answers_shown"] = False

            if "active_quiz" in st.session_state:
                st.markdown(f"""
                <div style='background:#0d1b2a;border:1px solid #a78bfa44;border-radius:12px;padding:18px;margin-top:8px;'>
                    <div style='color:#a78bfa;font-size:0.8rem;font-weight:600;margin-bottom:10px;'>
                        🧠 {quiz_type} Quiz — {target_lang} · {level}
                    </div>
                    <div style='color:#e2e8f0;font-size:0.87rem;white-space:pre-wrap;line-height:1.8;'>{st.session_state["active_quiz"]}</div>
                </div>""", unsafe_allow_html=True)

                q1, q2 = st.columns(2)
                with q1:
                    if st.button("✅ Mark Quiz Done", key="done_quiz"):
                        data["language"]["quiz_history"].append({
                            "date": today_lang_s, "type": quiz_type,
                            "language": target_lang, "level": level
                        })
                        save_data(data)
                        del st.session_state["active_quiz"]
                        st.success("🎉 Quiz done! Great practice!")
                        st.rerun()
                with q2:
                    if st.button("🔄 New Quiz", key="new_quiz"):
                        del st.session_state["active_quiz"]
                        st.rerun()

    # ── TAB 4: SESSION LOG ──
    with lang_t4:
        st.markdown(f"### 📝 Log Today's Lesson")
        with st.form("lang_session_form"):
            ls1, ls2 = st.columns(2)
            with ls1:
                ls_date    = st.date_input("Date", value=today_lang, key="ls_date")
                ls_type    = st.selectbox("Activity Type",
                    ["Course Lesson","Self Study","AI Practice","Flashcards","Podcast/Video",
                     "Speaking Practice","Reading","Writing","Other"], key="ls_type")
                ls_duration= st.number_input("Duration (minutes)", min_value=1, max_value=300, value=30, key="ls_dur")
            with ls2:
                ls_topic   = st.text_input("Topic / Chapter", placeholder="e.g. Chapter 3: Greetings", key="ls_topic")
                ls_words   = st.number_input("New words learned", min_value=0, value=0, key="ls_words")
                ls_notes   = st.text_area("Notes / Key takeaways", height=80, key="ls_notes",
                    placeholder="What did you learn today?")
            ls_mood = st.select_slider("Session difficulty",
                ["Very Easy","Easy","Just Right","Challenging","Very Hard"],
                value="Just Right", key="ls_mood")
            save_ls = st.form_submit_button("💾 Log Session", use_container_width=True)
            if save_ls:
                data["language"]["sessions"].append({
                    "date": ls_date.strftime("%Y-%m-%d"),
                    "language": target_lang,
                    "level": level,
                    "type": ls_type,
                    "duration_min": ls_duration,
                    "topic": ls_topic,
                    "words_learned": ls_words,
                    "notes": ls_notes,
                    "difficulty": ls_mood
                })
                save_data(data); st.success("✅ Session logged!"); st.rerun()

        # Session history + analytics
        sessions_all = data.get("language",{}).get("sessions",[])
        if sessions_all:
            st.divider()
            df_sess = pd.DataFrame(sessions_all)
            df_sess["date"] = pd.to_datetime(df_sess["date"])
            df_sess = df_sess.sort_values("date", ascending=False)

            # Weekly minutes chart
            df_week = df_sess.copy()
            df_week["week"] = df_week["date"].dt.strftime("W%U")
            if "duration_min" in df_week.columns:
                wk_mins = df_week.groupby("week")["duration_min"].sum().reset_index()
                fig_lang = go.Figure(go.Bar(x=wk_mins["week"], y=wk_mins["duration_min"],
                    marker_color="#3b82f6", opacity=0.85))
                fig_lang.update_layout(title="Minutes Studied per Week", height=200,
                    plot_bgcolor="#080c14", paper_bgcolor="#080c14",
                    xaxis=dict(color="#475569", showgrid=False),
                    yaxis=dict(color="#3b82f6", gridcolor="#1e293b"),
                    font=dict(color="#94a3b8"), margin=dict(l=0,r=0,t=30,b=0))
                st.plotly_chart(fig_lang, use_container_width=True)

            # Show only the columns that actually exist on this user's session data —
            # older sessions or fresh users may not have every field. We define the
            # preferred order and filter to whatever's present.
            preferred_cols = ["date", "type", "topic", "duration_min", "words_learned", "difficulty"]
            available_cols = [c for c in preferred_cols if c in df_sess.columns]
            if available_cols:
                st.dataframe(df_sess[available_cols].head(20),
                    use_container_width=True, hide_index=True)
            else:
                st.info("No structured session data yet. Log a session above to see your history table.")

    # ── TAB 5: SETTINGS ──
    with lang_t5:
        st.markdown("### ⚙️ Language Lab Settings")
        with st.form("lang_settings_form"):
            LANGUAGES = ["German","English","Spanish","French","Italian","Portuguese",
                         "Arabic","Turkish","Japanese","Chinese (Mandarin)","Korean",
                         "Russian","Dutch","Swedish","Greek","Hebrew","Persian","Hindi"]
            LEVELS = ["Beginner (A1)","Elementary (A2)","Intermediate (B1)",
                      "Upper-Intermediate (B2)","Advanced (C1)","Mastery (C2)"]
            NATIVES = ["Arabic","English","German","Spanish","French","Turkish","Other"]

            set1, set2 = st.columns(2)
            with set1:
                s_target = st.selectbox("Language I'm Learning", LANGUAGES,
                    index=LANGUAGES.index(target_lang) if target_lang in LANGUAGES else 0, key="s_target_lang")
                s_level  = st.selectbox("My Current Level", LEVELS,
                    index=LEVELS.index(level) if level in LEVELS else 0, key="s_level")
            with set2:
                s_native = st.selectbox("My Native Language", NATIVES,
                    index=NATIVES.index(native_lang) if native_lang in NATIVES else 0, key="s_native")
                s_goal   = st.text_input("Learning Goal",
                    value=settings.get("goal",""), key="s_goal",
                    placeholder="e.g. Pass B2 exam in 6 months, Travel to Germany")
                s_daily  = st.number_input("Daily study goal (minutes)", min_value=5, max_value=480,
                    value=settings.get("daily_goal_min", 30), key="s_daily")
            save_sett = st.form_submit_button("💾 Save Settings", use_container_width=True)
            if save_sett:
                data["language"]["settings"] = {
                    "target_language": s_target, "native_language": s_native,
                    "level": s_level, "goal": s_goal, "daily_goal_min": s_daily
                }
                save_data(data); st.success("✅ Settings saved!"); st.rerun()

        # AI book reading assistant
        if get_gemini_key():
            st.divider()
            st.markdown("#### 📖 Book Reading Assistant")
            st.caption(f"Paste a paragraph from a {target_lang} book and get help understanding it.")
            book_text = st.text_area(f"Paste {target_lang} text here", height=120, key="book_text",
                placeholder=f"Paste any {target_lang} text — a book paragraph, article, or dialogue...")
            if st.button("🤖 Analyse & Explain", key="analyse_text") and book_text.strip():
                p = f"""A {level} {target_lang} student pasted this text:

"{book_text}"

Please:
1. List all difficult/advanced words with {native_lang} translations and brief explanations
2. Explain any tricky grammar structures
3. Provide a natural {native_lang} translation of the whole text
4. Give one question to check understanding

Keep it educational and encouraging."""
                with st.spinner("Analysing text..."):
                    analysis = call_gemini(p, max_tokens=2500)
                st.markdown(f"<div style='background:#0d1b2a;border:1px solid #3b82f644;border-radius:10px;padding:16px;white-space:pre-wrap;color:#e2e8f0;font-size:0.86rem;line-height:1.7;'>{analysis}</div>", unsafe_allow_html=True)



# ==========================================
# LIBRARY PAGE
# ==========================================
elif st.session_state['page'] == 'Library':
    st.title("📚 Knowledge Library")

    # Stats overview
    total_books = len(data["library"]["books"])
    done_books = sum(1 for b in data["library"]["books"] if b.get("Status") == "Done")
    total_courses = len(data["library"]["courses"])
    done_courses = sum(1 for c in data["library"]["courses"] if c.get("Status") == "Done")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Books Read", f"{done_books}/{total_books}")
    c2.metric("Courses Done", f"{done_courses}/{total_courses}")
    c3.metric("Skills Tracked", len(data["library"]["skills"]))
    c4.metric("Media Watched", sum(1 for m in data["library"]["media"] if m.get("Status") == "Watched"))

    st.divider()
    t1, t2, t3, t4 = st.tabs(["📖 Books", "🎓 Courses", "🛠️ Skills", "🎬 Media"])

    def render_lib(key, opts):
        df = pd.DataFrame(data["library"][key])
        if df.empty:
            df = pd.DataFrame([{"Title": "", "Status": opts[0], "Rating": 0, "Notes": ""}])
        if "Notes" not in df.columns:
            df["Notes"] = ""
        ed = st.data_editor(
            df, num_rows="dynamic",
            column_config={
                "Status": st.column_config.SelectboxColumn(options=opts),
                "Rating": st.column_config.NumberColumn(min_value=0, max_value=5, format="%d ⭐"),
                "Notes": st.column_config.TextColumn("Notes", width="medium")
            },
            key=f"lib_{key}", use_container_width=True
        )
        if not df.equals(ed):
            data["library"][key] = ed.to_dict("records")
            save_data(data)

    with t1:
        render_lib("books", ["To Read", "Reading", "Done"])
    with t2:
        render_lib("courses", ["Not Started", "In Progress", "Done"])
    with t3:
        render_lib("skills", ["Beginner", "Intermediate", "Advanced"])
    with t4:
        render_lib("media", ["Watchlist", "Watching", "Watched"])


# ==========================================
# PAGE 5: NOTES (NEW)
# ==========================================
elif st.session_state['page'] == 'Notes':
    st.title("📝 Daily Notes")
    st.markdown("<p style='color:#64748b;'>Quick log of thoughts, wins, and reflections by day.</p>", unsafe_allow_html=True)

    # Show notes for last 14 days
    days_with_notes = []
    for i in range(30):
        d_key = (datetime.date.today() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        note = data["history"].get(d_key, {}).get("note", "")
        if note and note.strip():
            days_with_notes.append((d_key, note))

    if days_with_notes:
        for d_key, note in days_with_notes:
            d_obj = datetime.datetime.strptime(d_key, "%Y-%m-%d")
            tasks_done = sum(1 for k in ["t1_ds", "t2_de", "t3_gym", "t4_life"]
                             if data["history"].get(d_key, {}).get(k, False))
            st.markdown(f"""
            <div style='background:#0d1b2a; border:1px solid #1e3a5f; border-radius:12px; padding:16px 20px; margin-bottom:12px;'>
                <div style='display:flex; justify-content:space-between; margin-bottom:8px;'>
                    <span style='color:#60a5fa; font-weight:600; font-size:0.95rem;'>{d_obj.strftime('%A, %b %d')}</span>
                    <span style='color:#475569; font-size:0.8rem; font-family:JetBrains Mono,monospace;'>{tasks_done}/4 tasks</span>
                </div>
                <p style='color:#cbd5e1; margin:0; font-size:0.88rem; line-height:1.6;'>{note}</p>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("No notes yet. Add quick notes from the Daily Tracker → Work Block tab.")

    st.divider()
    st.subheader("✍️ Write for Today")
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    if today_str not in data["history"]:
        data["history"][today_str] = {}
    current_today_note = data["history"][today_str].get("note", "")
    new_today_note = st.text_area("Today's note", value=current_today_note, height=120,
                                   placeholder="Write your wins, reflections, blockers...")
    if st.button("💾 Save Note"):
        data["history"][today_str]["note"] = new_today_note
        save_data(data)
        st.success("Note saved!")
        st.rerun()


# ==========================================
# PAGE 6: REPORTS
# ==========================================
elif st.session_state['page'] == 'Reports':
    st.title("🏆 Reports")

    tab_daily, tab_weekly, tab_export = st.tabs(["📄 Daily Card", "📊 Weekly Summary", "📥 Export"])

    with tab_daily:
        st.subheader(f"Report for {selected_date.strftime('%B %d, %Y')}")

        def gen_report():
            # ── Brighter, friendlier palette ──
            # Soft warm gradient background, vibrant accent colors per metric.
            BG_TOP    = "#1e293b"
            BG_BOT    = "#0f172a"
            CARD_BG   = "#1e2740"
            CARD_BG2  = "#252e4a"

            ACCENT    = "#22c55e"   # brand green
            BLUE      = "#60a5fa"
            PURPLE    = "#a78bfa"
            PINK      = "#f472b6"
            ORANGE    = "#fb923c"
            YELLOW    = "#fcd34d"
            RED       = "#f87171"
            TEAL      = "#5eead4"

            TEXT_HI   = "#f1f5f9"
            TEXT_MID  = "#cbd5e1"
            TEXT_LO   = "#94a3b8"
            TEXT_DIM  = "#64748b"

            fig, ax = plt.subplots(figsize=(8.5, 11), dpi=150)
            ax.set_xlim(0, 100); ax.set_ylim(0, 130)
            ax.axis("off")

            # ── Vertical gradient background ──
            for i in range(130):
                t = i / 130
                # Mix between BG_TOP and BG_BOT
                r = int(int(BG_TOP[1:3], 16) * (1 - t) + int(BG_BOT[1:3], 16) * t)
                g = int(int(BG_TOP[3:5], 16) * (1 - t) + int(BG_BOT[3:5], 16) * t)
                b = int(int(BG_TOP[5:7], 16) * (1 - t) + int(BG_BOT[5:7], 16) * t)
                ax.add_patch(mpatches.Rectangle((0, i), 100, 1.05,
                                                 facecolor=f"#{r:02x}{g:02x}{b:02x}",
                                                 edgecolor="none", zorder=0))
            fig.patch.set_facecolor(BG_BOT)

            d = data["history"].get(current_day_str, {})
            done_count = sum(1 for k in ["t1_ds", "t2_de", "t3_gym", "t4_life"] if d.get(k))
            pct       = int((done_count / 4) * 100)
            p_done    = sum(1 for v in d.get("prayers", {}).values() if v)
            mood      = d.get("mood", 3)
            energy    = d.get("energy", 3)

            # Compute current streak
            streak = 0
            d_check = selected_date
            while True:
                key = d_check.strftime("%Y-%m-%d")
                day_h = data["history"].get(key, {})
                if any(day_h.get(t) for t in ["t1_ds", "t2_de", "t3_gym", "t4_life"]):
                    streak += 1
                    d_check -= datetime.timedelta(days=1)
                else:
                    break

            # ── Header ──
            ax.text(6, 122, "THRIVO", fontsize=10, color=ACCENT,
                    weight="bold")
            ax.text(6, 117, "Daily Report", fontsize=24, color=TEXT_HI, weight="bold")
            ax.text(6, 113, selected_date.strftime("%A · %B %d, %Y"),
                    fontsize=11, color=TEXT_LO)

            # Streak badge in top-right
            if streak > 0:
                streak_x, streak_y = 78, 117
                ax.add_patch(mpatches.FancyBboxPatch(
                    (streak_x, streak_y), 16, 6,
                    boxstyle="round,pad=0.1,rounding_size=2",
                    facecolor=ORANGE, edgecolor="none", alpha=0.95))
                ax.text(streak_x + 8, streak_y + 3, f"{streak} day streak",
                        ha="center", va="center", fontsize=9.5,
                        color="#0f172a", weight="bold")

            # ── 3 Hero Cards: Tasks / Prayers / Mood ──
            card_y     = 92
            card_h     = 16
            card_w     = 28
            card_gap   = 4
            cards_x    = 6

            # Card 1: TASKS
            task_color = ACCENT if pct >= 75 else YELLOW if pct >= 50 else RED
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x, card_y), card_w, card_h,
                boxstyle="round,pad=0.1,rounding_size=2.5",
                facecolor=CARD_BG, edgecolor=task_color, linewidth=2))
            # Colored side accent
            ax.add_patch(mpatches.Rectangle(
                (cards_x, card_y), 1.2, card_h, facecolor=task_color, edgecolor="none"))
            ax.text(cards_x + 3, card_y + card_h - 2.5, "TASKS",
                    fontsize=8, color=TEXT_DIM, weight="bold")
            ax.text(cards_x + 3, card_y + 6, f"{pct}%",
                    fontsize=28, color=task_color, weight="bold",
                    family="monospace")
            ax.text(cards_x + 3, card_y + 2.5, f"{done_count} of 4 protocols",
                    fontsize=8, color=TEXT_LO)

            # Card 2: PRAYERS
            cards_x2 = cards_x + card_w + card_gap
            pray_color = ACCENT if p_done == 5 else YELLOW if p_done >= 3 else RED
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x2, card_y), card_w, card_h,
                boxstyle="round,pad=0.1,rounding_size=2.5",
                facecolor=CARD_BG, edgecolor=pray_color, linewidth=2))
            ax.add_patch(mpatches.Rectangle(
                (cards_x2, card_y), 1.2, card_h, facecolor=pray_color, edgecolor="none"))
            ax.text(cards_x2 + 3, card_y + card_h - 2.5, "PRAYERS",
                    fontsize=8, color=TEXT_DIM, weight="bold")
            ax.text(cards_x2 + 3, card_y + 6, f"{p_done}/5",
                    fontsize=28, color=pray_color, weight="bold",
                    family="monospace")
            label = "Complete" if p_done == 5 else "In progress"
            ax.text(cards_x2 + 3, card_y + 2.5, label, fontsize=8, color=TEXT_LO)

            # Card 3: MOOD — colored gradient based on mood level
            cards_x3 = cards_x2 + card_w + card_gap
            mood_palette = {
                1: (RED,    "#7f1d1d", "Tough day"),
                2: (ORANGE, "#7c2d12", "Meh"),
                3: (YELLOW, "#713f12", "Neutral"),
                4: (TEAL,   "#134e4a", "Good"),
                5: (PURPLE, "#3b0764", "Excellent"),
            }
            mood_main, mood_dark, mood_label = mood_palette.get(mood, mood_palette[3])
            mood_emoji_map = {1: "low", 2: "down", 3: "ok", 4: "up", 5: "great"}
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x3, card_y), card_w, card_h,
                boxstyle="round,pad=0.1,rounding_size=2.5",
                facecolor=mood_dark, edgecolor=mood_main, linewidth=2))
            ax.add_patch(mpatches.Rectangle(
                (cards_x3, card_y), 1.2, card_h, facecolor=mood_main, edgecolor="none"))
            ax.text(cards_x3 + 3, card_y + card_h - 2.5, "MOOD",
                    fontsize=8, color=TEXT_DIM, weight="bold")
            # Big circle showing mood level on a 1-5 dial
            circle_cx, circle_cy = cards_x3 + card_w - 7.5, card_y + card_h / 2
            ax.add_patch(mpatches.Circle((circle_cx, circle_cy), 4.5,
                                          facecolor=mood_main, edgecolor="none", alpha=0.95))
            ax.text(circle_cx, circle_cy, str(mood),
                    ha="center", va="center", fontsize=22,
                    color="#0f172a", weight="bold", family="monospace")
            ax.text(cards_x3 + 3, card_y + 7, mood_label,
                    fontsize=14, color=mood_main, weight="bold")
            ax.text(cards_x3 + 3, card_y + 3.5, f"{mood}/5 — {mood_emoji_map.get(mood, 'ok')}",
                    fontsize=8, color=TEXT_LO)

            # ── Energy Meter ──
            energy_y = 82
            ax.text(cards_x, energy_y + 2, "ENERGY", fontsize=8,
                    color=TEXT_DIM, weight="bold")
            # Background bar
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x, energy_y - 2.5), 88, 3.5,
                boxstyle="round,pad=0,rounding_size=1.5",
                facecolor="#1e293b", edgecolor="none"))
            # Fill bar
            energy_pct = (energy / 5)
            energy_color = (RED if energy <= 2 else
                            YELLOW if energy <= 3 else
                            ACCENT if energy >= 4 else BLUE)
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x, energy_y - 2.5), 88 * energy_pct, 3.5,
                boxstyle="round,pad=0,rounding_size=1.5",
                facecolor=energy_color, edgecolor="none", alpha=0.95))
            ax.text(cards_x + 88 * energy_pct + 1.5, energy_y - 0.8,
                    f"{energy}/5",
                    fontsize=10, color=TEXT_HI, weight="bold", family="monospace")

            # ── Two columns: PROTOCOLS + PRAYER LOG ──
            col_y    = 70
            col_h    = 33
            col_w    = 42

            # PROTOCOLS card
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x, col_y - col_h + 5), col_w, col_h,
                boxstyle="round,pad=0.1,rounding_size=2",
                facecolor=CARD_BG2, edgecolor="#334155", linewidth=1))
            ax.text(cards_x + 2, col_y + 2, "🎯  PROTOCOLS", fontsize=10,
                    color=BLUE, weight="bold")
            task_labels = [
                ("Deep Work",     "t1_ds",  "📚"),
                ("German Study",  "t2_de",  "🇩🇪"),
                ("Gym",           "t3_gym", "💪"),
                ("Family",        "t4_life","❤️"),
            ]
            ty = col_y - 3
            for lbl, k, em in task_labels:
                is_done = d.get(k, False)
                # Status pill
                status_color = ACCENT if is_done else "#475569"
                status_text  = "DONE" if is_done else "skip"
                ax.add_patch(mpatches.Circle((cards_x + 4, ty + 1), 1.2,
                                              facecolor=status_color, edgecolor="none"))
                if is_done:
                    ax.text(cards_x + 4, ty + 1, "✓",
                            ha="center", va="center", fontsize=8,
                            color="#0f172a", weight="bold")
                ax.text(cards_x + 8, ty + 1, lbl, fontsize=11,
                        color=TEXT_HI if is_done else TEXT_DIM,
                        weight="bold" if is_done else "normal",
                        va="center")
                # Status text on right
                ax.text(cards_x + col_w - 3, ty + 1, status_text,
                        fontsize=8, color=status_color,
                        weight="bold", ha="right", va="center")
                ty -= 6

            # PRAYER LOG card
            cards_x_pr = cards_x + col_w + 4
            ax.add_patch(mpatches.FancyBboxPatch(
                (cards_x_pr, col_y - col_h + 5), col_w, col_h,
                boxstyle="round,pad=0.1,rounding_size=2",
                facecolor=CARD_BG2, edgecolor="#334155", linewidth=1))
            ax.text(cards_x_pr + 2, col_y + 2, "🕌  PRAYERS", fontsize=10,
                    color=PURPLE, weight="bold")
            ty = col_y - 3
            prayer_colors = [TEAL, BLUE, YELLOW, ORANGE, PURPLE]
            for i, p in enumerate(["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]):
                done_p = d.get("prayers", {}).get(p, False)
                pc = prayer_colors[i] if done_p else "#475569"
                ax.add_patch(mpatches.Circle((cards_x_pr + 4, ty + 1), 1.2,
                                              facecolor=pc, edgecolor="none"))
                if done_p:
                    ax.text(cards_x_pr + 4, ty + 1, "✓",
                            ha="center", va="center", fontsize=8,
                            color="#0f172a", weight="bold")
                ax.text(cards_x_pr + 8, ty + 1, p, fontsize=11,
                        color=TEXT_HI if done_p else TEXT_DIM,
                        weight="bold" if done_p else "normal",
                        va="center")
                ty -= 5

            # ── Note card (colorful) ──
            note = d.get("note", "")
            if note:
                note_y = 28
                note_h = 12
                ax.add_patch(mpatches.FancyBboxPatch(
                    (cards_x, note_y - note_h + 5), 88, note_h,
                    boxstyle="round,pad=0.1,rounding_size=2",
                    facecolor=CARD_BG, edgecolor=PINK, linewidth=2))
                ax.add_patch(mpatches.Rectangle(
                    (cards_x, note_y - note_h + 5), 1.2, note_h,
                    facecolor=PINK, edgecolor="none"))
                ax.text(cards_x + 3, note_y + 2.5, "✍️  NOTE", fontsize=9,
                        color=PINK, weight="bold")
                # Word-wrap manually — split into ~70 char lines
                note_clean = note.replace("\n", " ").strip()
                if len(note_clean) > 220:
                    note_clean = note_clean[:217] + "..."
                # Simple wrap
                words = note_clean.split()
                lines, line = [], ""
                for w in words:
                    if len(line) + len(w) + 1 <= 75:
                        line = (line + " " + w).strip()
                    else:
                        lines.append(line)
                        line = w
                if line:
                    lines.append(line)
                lines = lines[:3]  # max 3 lines
                ny = note_y - 2
                for ln in lines:
                    ax.text(cards_x + 3, ny, ln, fontsize=9.5, color=TEXT_MID,
                            family="sans-serif")
                    ny -= 3.2

            # ── Footer ──
            ax.text(50, 6, f"Generated {datetime.date.today().strftime('%Y-%m-%d')}",
                    ha="center", fontsize=8, color=TEXT_DIM)
            ax.text(50, 3, "thrivo.app · Grow with intention",
                    ha="center", fontsize=7, color=TEXT_DIM, alpha=0.7)

            return fig

        fig = gen_report()
        st.pyplot(fig)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches='tight', facecolor="#0f172a")
        st.download_button("📥 Download Card", data=buf.getvalue(),
                           file_name=f"report_{current_day_str}.png", mime="image/png",
                           use_container_width=True, type="primary")

    with tab_weekly:
        st.subheader("Weekly Performance Summary")

        week_data = []
        for i in range(7):
            d_obj = datetime.date.today() - datetime.timedelta(days=6 - i)
            d_key = d_obj.strftime("%Y-%m-%d")
            d_hist = data["history"].get(d_key, {})
            tasks = sum(1 for k in ["t1_ds", "t2_de", "t3_gym", "t4_life"] if d_hist.get(k, False))
            prayers = sum(1 for v in d_hist.get("prayers", {}).values() if v)
            week_data.append({
                "Day": d_obj.strftime("%a"),
                "Tasks": tasks,
                "Prayers": prayers,
                "Mood": d_hist.get("mood", 0),
                "Energy": d_hist.get("energy", 0)
            })

        df_week = pd.DataFrame(week_data)

        fig_w = go.Figure()
        fig_w.add_trace(go.Bar(name="Tasks", x=df_week["Day"], y=df_week["Tasks"],
            marker_color="#3b82f6", opacity=0.85))
        fig_w.add_trace(go.Bar(name="Prayers", x=df_week["Day"], y=df_week["Prayers"],
            marker_color="#22c55e", opacity=0.85))
        fig_w.update_layout(
            barmode="group", height=300,
            plot_bgcolor="#080c14", paper_bgcolor="#080c14",
            xaxis=dict(color="#475569", showgrid=False),
            yaxis=dict(color="#475569", showgrid=True, gridcolor="#1e293b"),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8")),
            margin=dict(l=0, r=0, t=10, b=0)
        )
        st.plotly_chart(fig_w, use_container_width=True)

        # Weekly stats
        cw1, cw2, cw3, cw4 = st.columns(4)
        cw1.metric("Avg Tasks/Day", f"{df_week['Tasks'].mean():.1f}/4")
        cw2.metric("Avg Prayers/Day", f"{df_week['Prayers'].mean():.1f}/5")
        cw3.metric("Best Day", df_week.loc[df_week['Tasks'].idxmax(), 'Day'])
        cw4.metric("Total Tasks Done", f"{df_week['Tasks'].sum()}/28")

    with tab_export:
        st.subheader("📤 Export Your Data")
        st.markdown("Export your full Growth OS data as JSON for backup or analysis.")

        today_iso = datetime.date.today().strftime("%Y-%m-%d")

        export_json = json.dumps(data, indent=2)
        st.download_button(
            "📥 Download Full Data (JSON)",
            data=export_json,
            file_name=f"thrivo_backup_{today_iso}.json",
            mime="application/json"
        )

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**History Summary Table**")
        history_rows = []
        for d_key, v in sorted(data["history"].items(), reverse=True):
            history_rows.append({
                "Date": d_key,
                "Deep Work": "✅" if v.get("t1_ds") else "❌",
                "German": "✅" if v.get("t2_de") else "❌",
                "Gym": "✅" if v.get("t3_gym") else "❌",
                "Family": "✅" if v.get("t4_life") else "❌",
                "Prayers": sum(1 for p in v.get("prayers", {}).values() if p),
                "Mood": v.get("mood", "-"),
                "Energy": v.get("energy", "-"),
            })
        if history_rows:
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True)

# ==========================================
# PAGE: HABIT TRACKER (v9 new)
# ==========================================
elif st.session_state['page'] == 'Habits':
    st.title("✅ Habit Tracker")
    st.caption("Build lasting habits with 21-day streaks and a visual consistency grid.")

    habits_data = data.setdefault("habits", {"list": [], "log": {}})
    hlist = habits_data.setdefault("list", [])
    hlog = habits_data.setdefault("log", {})

    tab_today, tab_grid, tab_manage = st.tabs(["📅 Today", "📊 Consistency Grid", "⚙️ Manage Habits"])

    # ── TODAY TAB ──
    with tab_today:
        if not hlist:
            st.info("👋 You have no habits yet. Go to the **Manage Habits** tab to add your first one.")
        else:
            today_key = datetime.date.today().strftime("%Y-%m-%d")
            today_done = hlog.setdefault(today_key, [])

            st.markdown(f"### {datetime.date.today().strftime('%A, %B %d')}")
            done_count = len([h for h in hlist if h["id"] in today_done])
            total_count = len(hlist)
            pct = int((done_count / total_count * 100)) if total_count else 0

            c1, c2, c3 = st.columns(3)
            c1.metric("Habits Today", f"{done_count}/{total_count}")
            c2.metric("Completion", f"{pct}%")
            # Calculate longest current streak
            max_streak = 0
            for h in hlist:
                s = 0
                d_check = datetime.date.today()
                while True:
                    key = d_check.strftime("%Y-%m-%d")
                    if h["id"] in hlog.get(key, []):
                        s += 1
                        d_check -= datetime.timedelta(days=1)
                    else:
                        break
                max_streak = max(max_streak, s)
            c3.metric("Longest Active Streak", f"{max_streak} 🔥" if max_streak else "—")

            st.markdown("---")

            for h in hlist:
                hid = h["id"]
                name = h.get("name", "Untitled")
                icon = h.get("icon", "✓")
                target = h.get("target_days", 21)

                # Compute current streak
                cur_streak = 0
                d_check = datetime.date.today()
                while True:
                    key = d_check.strftime("%Y-%m-%d")
                    if hid in hlog.get(key, []):
                        cur_streak += 1
                        d_check -= datetime.timedelta(days=1)
                    else:
                        break

                streak_progress = min(cur_streak / target, 1.0) if target else 0
                is_today_done = hid in today_done

                hc1, hc2, hc3 = st.columns([4, 2, 1])
                with hc1:
                    label = f"{icon}  **{name}**"
                    progress_bar = f"""
                    <div style='background:var(--bg-inset);border-radius:999px;height:6px;overflow:hidden;margin-top:6px;'>
                        <div style='background:linear-gradient(90deg,var(--accent),var(--info));width:{streak_progress*100:.0f}%;height:100%;'></div>
                    </div>
                    <div style='font-size:0.72rem;color:var(--text-dim);margin-top:4px;'>
                        Streak: <b>{cur_streak}</b> / {target} days ({streak_progress*100:.0f}%)
                    </div>
                    """
                    st.markdown(f"{label}{progress_bar}", unsafe_allow_html=True)
                with hc2:
                    if cur_streak >= target:
                        st.markdown(f"<div style='text-align:center;color:var(--accent);font-weight:700;'>🏆 Built!</div>", unsafe_allow_html=True)
                    elif cur_streak >= 7:
                        st.markdown(f"<div style='text-align:center;color:var(--warn);font-weight:600;'>🔥 {cur_streak} days</div>", unsafe_allow_html=True)
                    else:
                        st.markdown(f"<div style='text-align:center;color:var(--text-dim);'>{cur_streak} days</div>", unsafe_allow_html=True)
                with hc3:
                    btn_label = "✓" if is_today_done else "○"
                    btn_type = "primary" if is_today_done else "secondary"
                    if st.button(btn_label, key=f"habit_toggle_{hid}", use_container_width=True, type=btn_type):
                        if is_today_done:
                            hlog[today_key] = [x for x in today_done if x != hid]
                        else:
                            hlog.setdefault(today_key, []).append(hid)
                        data["habits"] = habits_data
                        save_data(data)
                        st.rerun()

    # ── GRID TAB ──
    with tab_grid:
        if not hlist:
            st.info("Add a habit first to see your consistency grid.")
        else:
            st.markdown("### Last 10 Weeks")
            st.caption("Green = done, grey = missed. 70 days back → today.")

            # Build 10x7 grid for each habit
            for h in hlist:
                hid = h["id"]
                st.markdown(f"**{h.get('icon','✓')} {h.get('name','')}**")
                grid_html = "<div style='display:flex; gap:3px; flex-wrap:wrap; margin-bottom:16px;'>"
                for i in range(70):
                    day = datetime.date.today() - datetime.timedelta(days=69 - i)
                    key = day.strftime("%Y-%m-%d")
                    done = hid in hlog.get(key, [])
                    is_future = day > datetime.date.today()
                    if is_future:
                        color = "transparent"
                    elif done:
                        color = "var(--accent)"
                    else:
                        color = "var(--bg-inset)"
                    grid_html += (
                        f"<div title='{key}' "
                        f"style='width:18px;height:18px;background:{color};"
                        f"border-radius:4px;border:1px solid var(--border-soft);'></div>"
                    )
                grid_html += "</div>"
                st.markdown(grid_html, unsafe_allow_html=True)

                # Per-habit stats
                total_done = sum(1 for k, v in hlog.items() if hid in v)
                # Consistency over last 30 days
                last_30 = [datetime.date.today() - datetime.timedelta(days=i) for i in range(30)]
                done_30 = sum(1 for d in last_30 if hid in hlog.get(d.strftime("%Y-%m-%d"), []))
                pct_30 = (done_30 / 30) * 100

                gc1, gc2, gc3 = st.columns(3)
                gc1.metric("Total Check-ins", total_done)
                gc2.metric("30-Day Rate", f"{pct_30:.0f}%")
                gc3.metric("Target", f"{h.get('target_days',21)} days")
                st.markdown("---")

    # ── MANAGE TAB ──
    with tab_manage:
        st.subheader("Your Habits")

        with st.expander("➕ Add New Habit", expanded=not hlist):
            col_n, col_i, col_t = st.columns([3, 1, 1])
            with col_n:
                new_name = st.text_input("Habit name", key="new_habit_name", placeholder="e.g. Read 20 pages")
            with col_i:
                new_icon = st.text_input("Emoji", value="📖", key="new_habit_icon", max_chars=2)
            with col_t:
                new_target = st.number_input("Target days", value=21, min_value=1, max_value=365, key="new_habit_target")
            if st.button("➕ Add Habit", type="primary", key="add_habit_btn"):
                if new_name.strip():
                    new_id = f"h_{int(time.time() * 1000)}"
                    hlist.append({
                        "id": new_id,
                        "name": new_name.strip(),
                        "icon": new_icon.strip() or "✓",
                        "target_days": int(new_target),
                        "created": datetime.date.today().isoformat(),
                    })
                    data["habits"] = habits_data
                    save_data(data)
                    st.success(f"✅ Added: {new_name}")
                    st.rerun()
                else:
                    st.error("Please enter a habit name.")

        st.markdown("---")
        for h in hlist:
            mc1, mc2, mc3 = st.columns([5, 2, 1])
            mc1.markdown(f"**{h.get('icon','✓')} {h.get('name','')}**")
            mc2.markdown(f"<span style='color:var(--text-dim);'>Target: {h.get('target_days', 21)} days</span>", unsafe_allow_html=True)
            with mc3:
                if st.button("🗑️", key=f"del_habit_{h['id']}"):
                    habits_data["list"] = [x for x in hlist if x["id"] != h["id"]]
                    data["habits"] = habits_data
                    save_data(data)
                    st.rerun()


# ==========================================
# PAGE: POMODORO FOCUS TIMER (v9 new)
# ==========================================
elif st.session_state['page'] == 'Pomodoro':
    st.title("⏱️ Focus Timer")
    st.caption("Pomodoro technique: focused work in sprints, with structured breaks. Track your deep-work hours.")

    pomo_data = data.setdefault("pomodoro", {"sessions": [], "settings": {"focus_min": 25, "break_min": 5, "long_break_min": 15, "long_every": 4}})
    settings = pomo_data.setdefault("settings", {"focus_min": 25, "break_min": 5, "long_break_min": 15, "long_every": 4})
    sessions = pomo_data.setdefault("sessions", [])

    tab_timer, tab_history, tab_settings = st.tabs(["🎯 Timer", "📊 History", "⚙️ Settings"])

    with tab_timer:
        # Session labels state
        cur_label = st.text_input("What are you working on?",
                                  value=st.session_state.get("pomo_label", ""),
                                  placeholder="e.g. Writing Q3 report, Learning Python OOP...",
                                  key="pomo_label_input")

        # Streamlit can't run real-time timers server-side, so we log COMPLETED sessions.
        # The UI offers: start-timestamp logging with duration, or quick-log buttons.
        st.markdown("### Log a completed session")

        preset_col1, preset_col2, preset_col3, preset_col4 = st.columns(4)

        def _log_session(minutes: int, kind: str):
            sessions.append({
                "id":       f"p_{int(time.time() * 1000)}",
                "date":     datetime.date.today().isoformat(),
                "time":     datetime.datetime.now().strftime("%H:%M"),
                "label":    cur_label.strip() or "(untitled)",
                "minutes":  minutes,
                "kind":     kind,
            })
            data["pomodoro"] = pomo_data
            save_data(data)
            st.success(f"✅ Logged {minutes} min {kind}!")
            st.rerun()

        with preset_col1:
            if st.button(f"🎯 Focus ({settings['focus_min']} min)", use_container_width=True, type="primary"):
                _log_session(settings["focus_min"], "focus")
        with preset_col2:
            if st.button(f"☕ Break ({settings['break_min']} min)", use_container_width=True):
                _log_session(settings["break_min"], "break")
        with preset_col3:
            if st.button(f"🛌 Long break ({settings['long_break_min']} min)", use_container_width=True):
                _log_session(settings["long_break_min"], "long_break")
        with preset_col4:
            custom_min = st.number_input("Custom", min_value=1, max_value=240, value=30, key="pomo_custom_min", label_visibility="collapsed")
            if st.button(f"Log {custom_min} min", use_container_width=True):
                _log_session(int(custom_min), "focus")

        st.markdown("---")

        # Today's summary
        today_iso = datetime.date.today().isoformat()
        today_focus = sum(s["minutes"] for s in sessions if s["date"] == today_iso and s["kind"] == "focus")
        today_count = sum(1 for s in sessions if s["date"] == today_iso and s["kind"] == "focus")

        t1, t2, t3 = st.columns(3)
        t1.metric("Focus Today", f"{today_focus} min")
        t2.metric("Sessions", today_count)
        # Current streak (consecutive days with ≥1 focus session)
        streak = 0
        d_check = datetime.date.today()
        while True:
            di = d_check.isoformat()
            if any(s["date"] == di and s["kind"] == "focus" for s in sessions):
                streak += 1
                d_check -= datetime.timedelta(days=1)
            else:
                break
        t3.metric("Focus Streak", f"{streak} 🔥" if streak else "0")

        # Today's sessions timeline
        st.markdown("### Today's sessions")
        today_sess = [s for s in sessions if s["date"] == today_iso]
        if not today_sess:
            st.info("No sessions logged today yet. Start your first focus sprint above!")
        else:
            for s in reversed(today_sess):
                icon = {"focus": "🎯", "break": "☕", "long_break": "🛌"}.get(s["kind"], "⏱️")
                color = {"focus": "var(--accent)", "break": "var(--info)", "long_break": "var(--warn)"}.get(s["kind"], "var(--text-muted)")
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                    f"border-left:3px solid {color};border-radius:8px;padding:10px 14px;margin-bottom:6px;"
                    f"display:flex;justify-content:space-between;align-items:center;'>"
                    f"<div><span style='color:{color};font-weight:600;'>{icon} {s['kind'].replace('_',' ').title()}</span>"
                    f"<span style='color:var(--text);margin-left:10px;'>{s['label']}</span></div>"
                    f"<span style='color:var(--text-dim);font-family:JetBrains Mono,monospace;font-size:0.85rem;'>"
                    f"{s['time']} · {s['minutes']} min</span></div>",
                    unsafe_allow_html=True,
                )

    with tab_history:
        if not sessions:
            st.info("No focus sessions yet. Start logging sessions in the Timer tab.")
        else:
            # Last 14 days bar chart
            last14 = []
            for i in range(14):
                d = datetime.date.today() - datetime.timedelta(days=13 - i)
                di = d.isoformat()
                focus_min = sum(s["minutes"] for s in sessions if s["date"] == di and s["kind"] == "focus")
                last14.append({"date": d.strftime("%a %d"), "focus": focus_min})
            df14 = pd.DataFrame(last14)
            fig_h = go.Figure()
            fig_h.add_trace(go.Bar(x=df14["date"], y=df14["focus"], marker_color="#22c55e", opacity=0.85))
            fig_h.update_layout(
                height=260,
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(color="#94a3b8", showgrid=False),
                yaxis=dict(color="#94a3b8", showgrid=True, gridcolor="#1e293b", title="Focus minutes"),
                margin=dict(l=0, r=0, t=10, b=0),
                font=dict(color="#94a3b8"),
            )
            st.plotly_chart(fig_h, use_container_width=True)

            # All sessions table
            st.markdown("### All sessions")
            df_all = pd.DataFrame(sessions)
            if not df_all.empty:
                df_all = df_all.sort_values(["date", "time"], ascending=False).head(100)
                st.dataframe(df_all[["date", "time", "kind", "label", "minutes"]],
                             use_container_width=True, hide_index=True)

            # Total stats
            total_focus = sum(s["minutes"] for s in sessions if s["kind"] == "focus")
            total_sessions = sum(1 for s in sessions if s["kind"] == "focus")
            s1, s2, s3 = st.columns(3)
            s1.metric("Total Focus Hours", f"{total_focus/60:.1f}")
            s2.metric("Total Sessions", total_sessions)
            s3.metric("Avg Session", f"{total_focus/max(total_sessions,1):.0f} min")

    with tab_settings:
        st.subheader("Timer Settings")
        st.caption("Customize your focus sprint lengths. Changes apply to future logged sessions.")

        s1, s2 = st.columns(2)
        with s1:
            new_focus = st.number_input("Focus duration (min)", min_value=5, max_value=120,
                                        value=int(settings.get("focus_min", 25)), step=5)
            new_break = st.number_input("Short break (min)", min_value=1, max_value=60,
                                        value=int(settings.get("break_min", 5)), step=1)
        with s2:
            new_long = st.number_input("Long break (min)", min_value=5, max_value=120,
                                       value=int(settings.get("long_break_min", 15)), step=5)
            new_every = st.number_input("Long break after every N focus sessions",
                                        min_value=2, max_value=10,
                                        value=int(settings.get("long_every", 4)))

        if st.button("💾 Save Settings", type="primary"):
            settings["focus_min"]      = int(new_focus)
            settings["break_min"]      = int(new_break)
            settings["long_break_min"] = int(new_long)
            settings["long_every"]     = int(new_every)
            data["pomodoro"] = pomo_data
            save_data(data)
            st.success("✅ Settings saved!")
            st.rerun()


# ==========================================
# PAGE: GOAL OS — OKR Framework (v9 new)
# ==========================================
elif st.session_state['page'] == 'GoalOS':
    st.title("🎯 Goal OS")
    st.caption("Objectives & Key Results — set quarterly objectives, measure what matters, ship what counts.")

    okr_data = data.setdefault("okr", {"objectives": [], "checkins": []})
    objectives = okr_data.setdefault("objectives", [])
    checkins = okr_data.setdefault("checkins", [])

    # Determine current quarter
    today = datetime.date.today()
    cur_q = (today.month - 1) // 3 + 1
    cur_year = today.year
    quarter_label = f"Q{cur_q} {cur_year}"

    tab_obj, tab_checkins, tab_archive = st.tabs([f"📍 {quarter_label} Objectives", "📈 Weekly Check-ins", "📦 Archive"])

    with tab_obj:
        # Filter to active objectives in current quarter
        active_objs = [o for o in objectives if o.get("status", "active") == "active"
                       and o.get("quarter") == quarter_label]

        if not active_objs:
            st.info(f"You have no active objectives for {quarter_label}. Add your first one below.")

        for obj in active_objs:
            krs = obj.get("key_results", [])
            # Compute overall progress as avg of KR progress
            if krs:
                avg_prog = sum(
                    min(
                        (kr.get("current", 0) / kr["target"] * 100) if kr.get("target") else 0,
                        100
                    )
                    for kr in krs
                ) / len(krs)
            else:
                avg_prog = 0

            status_color = "var(--accent)" if avg_prog >= 70 else "var(--warn)" if avg_prog >= 40 else "var(--danger)"
            emoji = "🟢" if avg_prog >= 70 else "🟡" if avg_prog >= 40 else "🔴"

            with st.container():
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                    f"border-left:4px solid {status_color};border-radius:12px;padding:16px 20px;margin-bottom:12px;'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                    f"<h3 style='margin:0;color:var(--text-heading);'>{emoji} {obj.get('title','(untitled)')}</h3>"
                    f"<span style='color:{status_color};font-weight:700;font-size:1.2rem;font-family:JetBrains Mono,monospace;'>"
                    f"{avg_prog:.0f}%</span></div>"
                    f"<div style='color:var(--text-muted);font-size:0.88rem;margin-top:4px;'>{obj.get('why','')}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # KR editor
                for kr_idx, kr in enumerate(krs):
                    kr_prog = min((kr.get("current", 0) / kr["target"] * 100) if kr.get("target") else 0, 100)
                    kr_col1, kr_col2, kr_col3 = st.columns([4, 2, 1])
                    with kr_col1:
                        st.markdown(f"**📌 {kr.get('title','(KR)')}**")
                        st.markdown(
                            f"<div style='background:var(--bg-inset);border-radius:999px;height:6px;overflow:hidden;'>"
                            f"<div style='background:linear-gradient(90deg,var(--accent),var(--info));"
                            f"width:{kr_prog:.0f}%;height:100%;'></div></div>"
                            f"<div style='font-size:0.72rem;color:var(--text-dim);margin-top:4px;'>"
                            f"{kr.get('current',0)} / {kr.get('target',0)} {kr.get('unit','')}</div>",
                            unsafe_allow_html=True,
                        )
                    with kr_col2:
                        new_current = st.number_input(
                            "Update progress",
                            value=float(kr.get("current", 0)),
                            key=f"kr_upd_{obj['id']}_{kr_idx}",
                            label_visibility="collapsed",
                        )
                    with kr_col3:
                        if st.button("💾", key=f"kr_save_{obj['id']}_{kr_idx}"):
                            kr["current"] = float(new_current)
                            save_data(data)
                            st.rerun()

                # Actions on the objective
                ac1, ac2, ac3 = st.columns([1, 1, 4])
                with ac1:
                    if st.button("✅ Complete", key=f"obj_done_{obj['id']}"):
                        obj["status"] = "completed"
                        obj["completed_on"] = today.isoformat()
                        save_data(data)
                        st.rerun()
                with ac2:
                    if st.button("🗑️ Archive", key=f"obj_arch_{obj['id']}"):
                        obj["status"] = "archived"
                        save_data(data)
                        st.rerun()

        st.markdown("---")
        st.subheader("➕ New Objective")
        with st.form("new_obj_form", clear_on_submit=True):
            new_title = st.text_input("Objective *", placeholder="e.g. Launch Thrivo v10 to paying customers")
            new_why = st.text_area("Why it matters", placeholder="The deeper reason behind this goal...", height=60)
            st.markdown("**Key Results** (how you'll measure success — add 2-5)")

            kr_inputs = []
            for i in range(3):
                kc1, kc2, kc3 = st.columns([4, 1, 1])
                with kc1:
                    ktitle = st.text_input(f"KR #{i+1}", key=f"new_kr_title_{i}",
                                           placeholder="e.g. Reach 100 signups")
                with kc2:
                    ktarget = st.number_input("Target", value=0.0, key=f"new_kr_target_{i}")
                with kc3:
                    kunit = st.text_input("Unit", value="", key=f"new_kr_unit_{i}", placeholder="users")
                kr_inputs.append((ktitle, ktarget, kunit))

            submitted = st.form_submit_button("🎯 Create Objective", type="primary", use_container_width=True)
            if submitted and new_title.strip():
                valid_krs = [
                    {"title": t.strip(), "target": float(tgt), "current": 0.0, "unit": u.strip()}
                    for (t, tgt, u) in kr_inputs if t.strip() and tgt > 0
                ]
                if not valid_krs:
                    st.error("Add at least one Key Result with a target > 0")
                else:
                    objectives.append({
                        "id":          f"o_{int(time.time() * 1000)}",
                        "title":       new_title.strip(),
                        "why":         new_why.strip(),
                        "quarter":     quarter_label,
                        "created":     today.isoformat(),
                        "status":      "active",
                        "key_results": valid_krs,
                    })
                    save_data(data)
                    st.success(f"✅ Created: {new_title}")
                    st.rerun()

    with tab_checkins:
        st.subheader("Weekly Check-in")
        st.caption("A short reflection each week keeps your OKRs alive.")

        today_iso = today.isoformat()
        recent_checkin = next((c for c in checkins if c["date"] == today_iso), None)

        with st.form("checkin_form", clear_on_submit=False):
            wins = st.text_area("🟢 Wins this week",
                                value=recent_checkin.get("wins", "") if recent_checkin else "",
                                height=80,
                                placeholder="What did you ship? What's working?")
            blockers = st.text_area("🟡 Blockers & risks",
                                    value=recent_checkin.get("blockers", "") if recent_checkin else "",
                                    height=80,
                                    placeholder="What's stuck? What worries you?")
            next_focus = st.text_area("🎯 Focus for next week",
                                      value=recent_checkin.get("next_focus", "") if recent_checkin else "",
                                      height=80,
                                      placeholder="Top 1-3 things to move forward")
            confidence = st.slider("Confidence in hitting this quarter's OKRs",
                                   min_value=1, max_value=10,
                                   value=recent_checkin.get("confidence", 7) if recent_checkin else 7)
            if st.form_submit_button("💾 Save Check-in", type="primary", use_container_width=True):
                new_checkin = {
                    "date":       today_iso,
                    "quarter":    quarter_label,
                    "wins":       wins.strip(),
                    "blockers":   blockers.strip(),
                    "next_focus": next_focus.strip(),
                    "confidence": int(confidence),
                }
                # Replace if exists, else append
                existing_idx = next((i for i, c in enumerate(checkins) if c["date"] == today_iso), None)
                if existing_idx is not None:
                    checkins[existing_idx] = new_checkin
                else:
                    checkins.append(new_checkin)
                save_data(data)
                st.success("✅ Check-in saved!")
                st.rerun()

        st.markdown("---")
        st.subheader("Recent check-ins")
        recent = sorted(checkins, key=lambda c: c["date"], reverse=True)[:6]
        if not recent:
            st.info("No check-ins yet. Write your first one above.")
        for c in recent:
            conf = c.get("confidence", 5)
            conf_color = "var(--accent)" if conf >= 7 else "var(--warn)" if conf >= 4 else "var(--danger)"
            st.markdown(
                f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                f"border-left:3px solid {conf_color};border-radius:10px;padding:12px 16px;margin-bottom:8px;'>"
                f"<div style='display:flex;justify-content:space-between;'>"
                f"<b style='color:var(--text-subhead);'>{c.get('date','')}</b>"
                f"<span style='color:{conf_color};font-weight:600;'>Confidence {conf}/10</span></div>"
                f"<div style='color:var(--text);margin-top:6px;font-size:0.88rem;'>"
                f"<b>🟢 Wins:</b> {c.get('wins','—')[:160]}<br>"
                f"<b>🟡 Blockers:</b> {c.get('blockers','—')[:160]}<br>"
                f"<b>🎯 Next:</b> {c.get('next_focus','—')[:160]}</div></div>",
                unsafe_allow_html=True,
            )

    with tab_archive:
        archived = [o for o in objectives if o.get("status") in ("completed", "archived")]
        if not archived:
            st.info("No archived objectives yet.")
        else:
            for obj in sorted(archived, key=lambda o: o.get("created", ""), reverse=True):
                krs = obj.get("key_results", [])
                final_prog = (sum(
                    min((kr.get("current", 0) / kr["target"] * 100) if kr.get("target") else 0, 100)
                    for kr in krs
                ) / len(krs)) if krs else 0
                icon = "✅" if obj.get("status") == "completed" else "📦"
                color = "var(--accent)" if obj.get("status") == "completed" else "var(--text-dim)"
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--border);"
                    f"border-radius:10px;padding:10px 14px;margin-bottom:6px;"
                    f"display:flex;justify-content:space-between;'>"
                    f"<span style='color:{color};'>{icon} <b>{obj.get('title','')}</b> "
                    f"<span style='color:var(--text-dim);font-size:0.8rem;'>· {obj.get('quarter','')}</span></span>"
                    f"<span style='color:{color};font-family:JetBrains Mono,monospace;'>{final_prog:.0f}%</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )


# ==========================================
# PAGE: FINANCE HUB PRO — Unified Dashboard (v9 new)
# ==========================================
elif st.session_state['page'] == 'FinanceDash':
    st.title("💎 Finance Hub Pro")
    st.caption("Your financial command center — net worth, cashflow, credit, investments, all in one view.")

    # ── Gather data from existing modules ──
    finance = data.get("finance", {})
    credit = data.get("credit", {})
    stocks = data.get("stocks", {})

    # Cash assets
    assets = finance.get("assets", [])
    total_cash_assets = sum(float(a.get("amount", 0) or 0) for a in assets)

    # Credit exposure — sum legacy QNB/EGBank balances PLUS user-added accounts
    credit_balances = credit.get("balances", {})
    legacy_balance = sum(float(v or 0) for v in credit_balances.values())
    credit_limits = credit.get("limits", {})
    legacy_limit = sum(float(v or 0) for v in credit_limits.values())

    # User-added accounts (Valu, Souhoola, other cards) — convert to EGP
    user_accounts = credit.get("accounts", [])
    _usd_rate_for_credit = _resolve_usd_rate(data)
    accounts_balance = sum(
        amount_to_egp(a.get("balance", 0), a.get("currency", "EGP"), _usd_rate_for_credit)
        for a in user_accounts
    )
    accounts_limit = sum(
        amount_to_egp(a.get("limit", 0), a.get("currency", "EGP"), _usd_rate_for_credit)
        for a in user_accounts
    )

    total_credit_used = legacy_balance + accounts_balance
    total_credit_limit = legacy_limit + accounts_limit
    credit_util = (total_credit_used / total_credit_limit * 100) if total_credit_limit > 0 else 0

    # Stock value (from saved price history)
    ph = stocks.get("price_history", {})
    latest_prices = {}
    if ph:
        latest_date = sorted(ph.keys())[-1]
        latest_prices = ph[latest_date]
    watchlist = stocks.get("watchlist", [])
    total_stock_value = sum(
        float(s.get("shares", 0) or 0) * float(latest_prices.get(s.get("symbol", ""), 0) or 0)
        for s in watchlist
    )
    total_stock_invested = sum(
        float(s.get("shares", 0) or 0) * float(s.get("avg_price", 0) or 0)
        for s in watchlist
    )
    stock_pnl = total_stock_value - total_stock_invested

    # Net worth
    net_worth = total_cash_assets + total_stock_value - total_credit_used

    # Monthly cash flow — convert USD income to EGP using live rate.
    # FIX: use _resolve_usd_rate(data) so we pick up the SAME rate the
    # Finance tab uses — including the user's own price_history fallback.
    # Previously the bare get_usd_egp_rate_global() would land on 50.0 if
    # the cron hadn't run, even when the Finance tab had a fresh live rate.
    usd_rate = _resolve_usd_rate(data)
    incomes = finance.get("income", [])
    total_monthly_income = sum(
        amount_to_egp(i.get("amount", 0), i.get("currency", "EGP"), usd_rate)
        for i in incomes
    )
    monthly_exp = finance.get("expenses_monthly", [])
    total_monthly_exp = sum(
        amount_to_egp(e.get("amount", 0), e.get("currency", "EGP"), usd_rate)
        for e in monthly_exp
    )
    monthly_cashflow = total_monthly_income - total_monthly_exp

    # Show the rate used so users see why a USD salary became a different EGP number
    has_usd = any((i.get("currency", "EGP") or "").upper() == "USD"
                  for i in incomes + monthly_exp)
    if has_usd:
        rate_source = "live" if usd_rate != 50.0 else "fallback (live source unreachable)"
        st.markdown(
            f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
            f"border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:0.82rem;'>"
            f"💱 USD income/expenses converted to EGP at "
            f"<b style='color:var(--info);font-family:JetBrains Mono,monospace;'>"
            f"{usd_rate:,.2f}</b> EGP/USD ({rate_source})"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Top row: 4 hero metrics ──
    st.markdown("### 📊 Overview")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Net Worth", f"{net_worth:,.0f} EGP",
              f"{(monthly_cashflow):+,.0f} /mo" if monthly_cashflow != 0 else None)
    m2.metric("Cash & Assets", f"{total_cash_assets:,.0f} EGP")
    m3.metric("Investments", f"{total_stock_value:,.0f} EGP",
              f"{stock_pnl:+,.0f}" if stock_pnl != 0 else None)
    m4.metric("Credit Debt", f"{total_credit_used:,.0f} EGP",
              f"{credit_util:.1f}% of limit" if total_credit_limit > 0 else None,
              delta_color="inverse")

    st.markdown("---")

    # ── Row 2: Net worth composition pie + monthly cashflow ──
    cL, cR = st.columns(2)

    with cL:
        st.markdown("#### Net Worth Composition")
        pie_data = []
        if total_cash_assets > 0:
            pie_data.append({"Category": "Cash & Assets", "Value": total_cash_assets})
        if total_stock_value > 0:
            pie_data.append({"Category": "Investments",   "Value": total_stock_value})
        if total_credit_used > 0:
            pie_data.append({"Category": "Credit Debt",   "Value": total_credit_used})
        if pie_data:
            df_pie = pd.DataFrame(pie_data)
            fig_pie = go.Figure(data=[go.Pie(
                labels=df_pie["Category"], values=df_pie["Value"],
                hole=0.55,
                marker=dict(colors=["#22c55e", "#3b82f6", "#ef4444"]),
                textfont=dict(color="#e2e8f0", size=12),
            )])
            fig_pie.update_layout(
                height=280,
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                showlegend=True,
                legend=dict(font=dict(color="#94a3b8")),
                margin=dict(l=0, r=0, t=0, b=0),
            )
            st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("Add some data in Finance, Stocks, or Credit tabs to see composition.")

    with cR:
        st.markdown("#### Monthly Cash Flow")
        cf_data = {
            "Stream": ["Income", "Expenses", "Net"],
            "Amount": [total_monthly_income, -total_monthly_exp, monthly_cashflow],
        }
        df_cf = pd.DataFrame(cf_data)
        colors_cf = ["#22c55e", "#ef4444", "#3b82f6"]
        fig_cf = go.Figure()
        fig_cf.add_trace(go.Bar(
            x=df_cf["Stream"], y=df_cf["Amount"],
            marker_color=colors_cf,
            text=[f"{abs(v):,.0f}" for v in df_cf["Amount"]],
            textposition="outside",
        ))
        fig_cf.update_layout(
            height=280,
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(color="#94a3b8", showgrid=False),
            yaxis=dict(color="#94a3b8", showgrid=True, gridcolor="#1e293b", title="EGP"),
            showlegend=False,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_cf, use_container_width=True)

    # ── Row 3: 6-month cashflow forecast ──
    st.markdown("---")
    st.markdown("#### 📈 6-Month Cashflow Forecast")
    st.caption("Straight-line projection based on current monthly income/expenses. Actuals will diverge — use as a planning baseline.")
    forecast_months = []
    running = net_worth
    for i in range(6):
        m_date = today + datetime.timedelta(days=30 * (i + 1))
        running += monthly_cashflow
        forecast_months.append({
            "Month":  m_date.strftime("%b %Y"),
            "Projected Net Worth": running,
        })
    df_fc = pd.DataFrame(forecast_months)
    fig_fc = go.Figure()
    fig_fc.add_trace(go.Scatter(
        x=["Today"] + df_fc["Month"].tolist(),
        y=[net_worth] + df_fc["Projected Net Worth"].tolist(),
        mode="lines+markers",
        line=dict(color="#22c55e", width=3),
        marker=dict(size=8),
        fill="tozeroy",
        fillcolor="rgba(34,197,94,0.08)",
    ))
    fig_fc.update_layout(
        height=240,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(color="#94a3b8", showgrid=False),
        yaxis=dict(color="#94a3b8", showgrid=True, gridcolor="#1e293b", title="EGP"),
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(fig_fc, use_container_width=True)

    # ── Row 4: Health signals ──
    st.markdown("---")
    st.markdown("#### 🩺 Financial Health Signals")

    signals = []

    # Emergency fund check (cash >= 3x monthly expenses)
    if total_monthly_exp > 0:
        ef_months = total_cash_assets / total_monthly_exp
        if ef_months >= 6:
            signals.append(("🟢", "Emergency Fund",
                            f"{ef_months:.1f} months of expenses covered — excellent buffer.",
                            "var(--accent)"))
        elif ef_months >= 3:
            signals.append(("🟡", "Emergency Fund",
                            f"{ef_months:.1f} months covered — aim for 6+ months.",
                            "var(--warn)"))
        else:
            signals.append(("🔴", "Emergency Fund",
                            f"Only {ef_months:.1f} months covered — build this up ASAP.",
                            "var(--danger)"))

    # Credit utilization
    if total_credit_limit > 0:
        if credit_util <= 30:
            signals.append(("🟢", "Credit Utilization",
                            f"{credit_util:.1f}% — healthy usage.",
                            "var(--accent)"))
        elif credit_util <= 60:
            signals.append(("🟡", "Credit Utilization",
                            f"{credit_util:.1f}% — try to keep under 30%.",
                            "var(--warn)"))
        else:
            signals.append(("🔴", "Credit Utilization",
                            f"{credit_util:.1f}% — reduce debt to protect credit score.",
                            "var(--danger)"))

    # Savings rate
    if total_monthly_income > 0:
        sav_rate = (monthly_cashflow / total_monthly_income) * 100
        if sav_rate >= 20:
            signals.append(("🟢", "Savings Rate",
                            f"Saving {sav_rate:.0f}% of income — you're building wealth.",
                            "var(--accent)"))
        elif sav_rate >= 5:
            signals.append(("🟡", "Savings Rate",
                            f"Saving {sav_rate:.0f}% — aim for 20%+.",
                            "var(--warn)"))
        else:
            signals.append(("🔴", "Savings Rate",
                            f"Saving only {sav_rate:.0f}% — review expenses.",
                            "var(--danger)"))

    # Investment allocation
    invested_of_networth = (total_stock_value / net_worth * 100) if net_worth > 0 else 0
    if total_stock_value > 0:
        if invested_of_networth <= 40:
            signals.append(("🟢", "Investment Allocation",
                            f"{invested_of_networth:.0f}% in stocks — balanced.",
                            "var(--accent)"))
        else:
            signals.append(("🟡", "Investment Allocation",
                            f"{invested_of_networth:.0f}% in stocks — check risk tolerance.",
                            "var(--warn)"))

    if not signals:
        st.info("Add income, expenses, and assets in the Finance tab to see your health signals.")
    else:
        for emoji, title, msg, color in signals:
            st.markdown(
                f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                f"border-left:4px solid {color};border-radius:10px;padding:12px 16px;margin-bottom:8px;'>"
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<span style='font-size:1.3rem;'>{emoji}</span>"
                f"<div><b style='color:var(--text-heading);'>{title}</b><br>"
                f"<span style='color:var(--text-muted);font-size:0.88rem;'>{msg}</span></div></div></div>",
                unsafe_allow_html=True,
            )

    # ── Row 5: Quick links ──
    st.markdown("---")
    st.markdown("#### 🔗 Jump to details")
    jl1, jl2, jl3, jl4 = st.columns(4)
    with jl1:
        if st.button("💸 Finance Hub", use_container_width=True):
            st.session_state["page"] = "Finance"; st.rerun()
    with jl2:
        if st.button("💳 Credit Tracker", use_container_width=True):
            st.session_state["page"] = "Credit"; st.rerun()
    with jl3:
        if st.button("📈 EGX Stocks", use_container_width=True):
            st.session_state["page"] = "Stocks"; st.rerun()
    with jl4:
        if st.button("💰 Gold & Dollar", use_container_width=True):
            st.session_state["page"] = "Gold"; st.rerun()



# ==========================================
# PAGE: SMART BUYING CALENDAR (v10 new — Pro+ feature)


# ==========================================
# PAGE: SMART BUYING CALENDAR (v10.1 — UX redesign)
# ==========================================
elif st.session_state['page'] == 'BuyTime':
    # ──────────────────────────────────────────────────────────────────
    #  HERO — answers "should I buy something now?" without scrolling
    # ──────────────────────────────────────────────────────────────────
    today = datetime.date.today()
    all_windows = buy_calendar.upcoming_windows(today=today, within_days=400)
    active_now  = [w for w in all_windows if w["is_active"]]
    next_30     = [w for w in all_windows if 0 < w["days_until"] <= 30]
    next_90     = [w for w in all_windows if 0 < w["days_until"] <= 90]

    # Hero header
    st.markdown(
        f"""<div style='padding:8px 0 4px;'>
            <h1 style='margin:0;display:flex;align-items:center;gap:10px;font-size:1.9rem;'>
                🛒 Smart Buying
                <span style='font-size:0.65rem;color:var(--text-faint);
                    font-weight:500;background:var(--bg-surface);
                    padding:3px 9px;border-radius:999px;border:1px solid var(--border-2);
                    text-transform:uppercase;letter-spacing:0.08em;'>Pro</span>
            </h1>
            <p style='color:var(--text-muted);margin:4px 0 18px;font-size:0.95rem;'>
                When to buy what in Egypt — backed by retail data, not guesses.
            </p>
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Hero card: ONE big actionable signal ──
    if active_now:
        # Pick the highest-confidence active window for the hero
        hero = sorted(active_now, key=lambda w: (w["confidence"] != "HIGH", w["category_title"]))[0]
        days_left = (hero["end"] - today).days
        st.markdown(
            f"""<div style='background:linear-gradient(135deg, rgba(34,197,94,0.12) 0%, rgba(34,197,94,0.04) 100%);
                border:1px solid var(--accent);border-radius:16px;padding:20px 24px;margin-bottom:16px;
                position:relative;overflow:hidden;'>
                <div style='position:absolute;top:14px;right:18px;background:var(--accent);
                    color:white;padding:4px 12px;border-radius:999px;font-size:0.72rem;
                    font-weight:700;letter-spacing:0.05em;text-transform:uppercase;'>🔥 ACTIVE NOW</div>
                <div style='font-size:0.78rem;color:var(--accent);font-weight:600;
                    text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;'>Buy in this window</div>
                <h2 style='margin:0;font-size:1.5rem;color:var(--text-heading);'>
                    {hero['category_icon']} {hero['category_title']}
                </h2>
                <div style='color:var(--text);font-size:0.96rem;margin:6px 0 10px;'>
                    {hero['name']}
                </div>
                <div style='display:flex;gap:18px;flex-wrap:wrap;font-size:0.85rem;'>
                    <span><b style='color:var(--accent);'>💸 {hero['discount_range']}</b></span>
                    <span style='color:var(--text-muted);'>📅 {hero['label']}</span>
                    <span style='color:var(--warn);font-weight:600;'>⏳ {days_left} day{'s' if days_left != 1 else ''} left</span>
                </div>
            </div>""",
            unsafe_allow_html=True,
        )
    elif next_30:
        soonest = next_30[0]
        st.markdown(
            f"""<div style='background:linear-gradient(135deg, rgba(59,130,246,0.10) 0%, rgba(59,130,246,0.04) 100%);
                border:1px solid var(--info);border-radius:16px;padding:20px 24px;margin-bottom:16px;'>
                <div style='font-size:0.78rem;color:var(--info);font-weight:600;
                    text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;'>⏰ Coming up soon</div>
                <h2 style='margin:0;font-size:1.5rem;color:var(--text-heading);'>
                    {soonest['category_icon']} {soonest['category_title']}
                </h2>
                <div style='color:var(--text);font-size:0.96rem;margin:6px 0 10px;'>
                    {soonest['name']}
                </div>
                <div style='display:flex;gap:18px;flex-wrap:wrap;font-size:0.85rem;'>
                    <span><b style='color:var(--info);'>💸 {soonest['discount_range']}</b></span>
                    <span style='color:var(--text-muted);'>📅 {soonest['label']}</span>
                    <span style='color:var(--accent);font-weight:600;'>🗓️ Starts in {soonest['days_until']} days</span>
                </div>
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.info("No active or imminent shopping windows. Best to use the **Plan a purchase** tab below to find your category's next window.")

    # Quick stats row
    qc1, qc2, qc3, qc4 = st.columns(4)
    qc1.metric("🔥 Active now",    len(active_now))
    qc2.metric("📆 Next 30 days",  len(next_30))
    qc3.metric("🗓️ Next 90 days",  len(next_90))
    qc4.metric("📂 Categories",    len(buy_calendar.all_categories()))

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    # ── Inline help expander (instead of buried docs) ──
    with st.expander("💡 How to use this — and how much to trust it", expanded=False):
        st.markdown("""
        **What this is:** An Egypt-specific calendar of when retailers historically run discount campaigns,
        plus statistical analysis of YOUR scraped price history. We cite every source so you can verify.

        **Confidence levels — what they mean:**

        - 🟢 **HIGH** — universally observed across major Egyptian retailers (Noon, Jumia, Amazon EG,
          Sephora, Carrefour) every year. Nearly certain to repeat.
        - 🟡 **MEDIUM** — strong general pattern, but discount depth or product mix varies by retailer/brand.
        - 🔴 **LOW** — anecdotal or niche; we say so honestly so you decide.

        **What we do NOT do:**

        - We do NOT invent specific dates like "buy on Oct 23rd" — windows are real, days inside them aren't.
        - We do NOT predict prices with AI. The Trends tab uses statistics on YOUR scraped data.
        - We do NOT promise the discount range — it's the historical pattern, not a guarantee.

        **Tabs at a glance:**

        - 📅 **Calendar** — visual 12-month view of all windows.
        - 🎯 **Plan a purchase** — pick what you want to buy, get personalized timing.
        - ⭐ **My watchlist** — save planned purchases, see countdowns.
        - 📊 **Price trends** — analysis of YOUR scraped data (gold, USD, BTC).
        - 💰 **Savings log** — track money saved by buying in-window.
        """)

    # ──────────────────────────────────────────────────────────────────
    #  TABS (renamed for clarity, fewer clicks deep)
    # ──────────────────────────────────────────────────────────────────
    tab_calendar, tab_plan, tab_watchlist, tab_trends, tab_savings = st.tabs([
        "📅 Calendar",
        "🎯 Plan a purchase",
        "⭐ My watchlist",
        "📊 Price trends",
        "💰 Savings log",
    ])

    # Helpers shared across tabs
    def _conf_pill(conf: str) -> str:
        if conf == "HIGH":
            return ("<span style='background:rgba(34,197,94,0.15);color:var(--accent);"
                    "border:1px solid var(--accent);padding:2px 9px;border-radius:999px;"
                    "font-size:0.68rem;font-weight:600;'>● HIGH</span>")
        if conf == "MEDIUM":
            return ("<span style='background:rgba(245,158,11,0.15);color:var(--warn);"
                    "border:1px solid var(--warn);padding:2px 9px;border-radius:999px;"
                    "font-size:0.68rem;font-weight:600;'>● MEDIUM</span>")
        return ("<span style='background:rgba(100,116,139,0.15);color:var(--text-dim);"
                "border:1px solid var(--text-dim);padding:2px 9px;border-radius:999px;"
                "font-size:0.68rem;font-weight:600;'>● LOW</span>")

    def _category_pick():
        """Build options list — used in multiple tabs."""
        keys = buy_calendar.all_categories()
        return keys, {k: f"{buy_calendar.get_category(k)['icon']} {buy_calendar.get_category(k)['title']}"
                      for k in keys}

    # ──────────────────────────────────────────────────────────────────
    #  TAB 1 — CALENDAR (visual 12-month)
    # ──────────────────────────────────────────────────────────────────
    with tab_calendar:
        st.markdown("### 📅 Visual Calendar — Next 12 Months")
        st.caption("Each band = one shopping window. Hover to see what's discounted.")

        # Build Plotly Gantt-style horizontal bar chart
        gantt_rows = []
        cat_colors = {
            "iphone":               "#3b82f6",
            "laptop":               "#8b5cf6",
            "car":                  "#ef4444",
            "summer_clothes":       "#f59e0b",
            "winter_clothes":       "#06b6d4",
            "gold":                 "#eab308",
            "appliances":           "#22c55e",
            "smart_home":           "#14b8a6",
            "personal_care_beauty": "#ec4899",
            "furniture":            "#a855f7",
        }

        for w in all_windows:
            # Skip windows that are basically year-round (year-round oral care, gold tracker)
            span_days = (w["end"] - w["start"]).days
            if span_days > 200:
                continue
            gantt_rows.append({
                "Category":   f"{w['category_icon']} {w['category_title']}",
                "Window":     w["name"],
                "Start":      w["start"],
                "End":        w["end"],
                "Confidence": w["confidence"],
                "Discount":   w["discount_range"],
                "color":      cat_colors.get(w["category_key"], "#64748b"),
                "Active":     w["is_active"],
            })

        if gantt_rows:
            df_g = pd.DataFrame(gantt_rows)
            df_g = df_g.sort_values(["Category", "Start"])

            fig = go.Figure()
            for _, row in df_g.iterrows():
                fig.add_trace(go.Scatter(
                    x=[row["Start"], row["End"]],
                    y=[row["Category"], row["Category"]],
                    mode="lines",
                    line=dict(color=row["color"],
                              width=18 if row["Active"] else 14),
                    opacity=1.0 if row["Active"] else 0.7,
                    hovertemplate=(
                        f"<b>{row['Window']}</b><br>"
                        f"📅 {row['Start'].strftime('%b %d')} → {row['End'].strftime('%b %d')}<br>"
                        f"💸 {row['Discount']}<br>"
                        f"🎯 Confidence: {row['Confidence']}<extra></extra>"
                    ),
                    showlegend=False,
                ))

            # Today's vertical line
            # NOTE: Plotly's add_vline annotation positioning calls float(sum(x))/len(x)
            # on x-axis dates, which fails on plain datetime.date. We pass a datetime
            # AND skip the auto-annotation, drawing the "Today" label as a separate
            # add_annotation call so we control how the position is computed.
            today_dt = datetime.datetime.combine(today, datetime.time(12, 0))
            fig.add_vline(
                x=today_dt,
                line_dash="dash", line_color="#22c55e", line_width=2,
            )
            fig.add_annotation(
                x=today_dt, y=1, yref="paper", yanchor="bottom",
                text="<b>Today</b>", showarrow=False,
                font=dict(color="#22c55e", size=11),
                bgcolor="rgba(8,12,20,0.85)",
                bordercolor="#22c55e", borderwidth=1, borderpad=3,
            )

            # Style
            n_cats = df_g["Category"].nunique()
            fig.update_layout(
                height=max(280, 38 * n_cats + 80),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(
                    color="#94a3b8", showgrid=True, gridcolor="rgba(148,163,184,0.12)",
                    type="date",
                    tickformat="%b %Y",
                    range=[today - datetime.timedelta(days=10),
                           today + datetime.timedelta(days=380)],
                ),
                yaxis=dict(
                    color="#cbd5e1", showgrid=False,
                    autorange="reversed",
                ),
                margin=dict(l=0, r=20, t=20, b=20),
                showlegend=False,
                hoverlabel=dict(bgcolor="#0d1b2a", bordercolor="#1e3a5f",
                                font_size=12, font_color="#e2e8f0"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Legend below chart
            legend_items = " ".join(
                f"<span style='display:inline-flex;align-items:center;gap:5px;margin:0 12px 6px 0;font-size:0.75rem;color:var(--text-muted);'>"
                f"<span style='display:inline-block;width:10px;height:10px;background:{c};border-radius:2px;'></span>"
                f"{buy_calendar.get_category(k)['icon']} {buy_calendar.get_category(k)['title']}</span>"
                for k, c in cat_colors.items() if buy_calendar.get_category(k)
            )
            st.markdown(f"<div style='margin-top:8px;'>{legend_items}</div>", unsafe_allow_html=True)
        else:
            st.info("No upcoming windows to display.")

        st.markdown("---")

        # ── Active windows list (deep dive) ──
        st.markdown("### 🔥 Active Right Now")
        if not active_now:
            st.info("No discount windows are active today. Check the **Plan a purchase** tab to see what's coming for the category you care about.")
        else:
            for w in active_now:
                days_left = (w["end"] - today).days
                st.markdown(
                    f"<div style='background:var(--bg-surface);border:1px solid var(--accent);"
                    f"border-left:4px solid var(--accent);border-radius:12px;padding:14px 18px;margin-bottom:10px;'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
                    f"<div><h4 style='margin:0;color:var(--text-heading);font-size:1.05rem;'>"
                    f"{w['category_icon']} {w['category_title']} · {w['name']}</h4>"
                    f"<div style='color:var(--text-muted);font-size:0.82rem;margin-top:4px;'>"
                    f"📅 {w['label']} · ⏳ {days_left} days left</div></div>"
                    f"<div style='text-align:right;'>{_conf_pill(w['confidence'])}<br>"
                    f"<span style='color:var(--accent);font-weight:600;font-size:0.86rem;margin-top:6px;display:inline-block;'>"
                    f"💸 {w['discount_range']}</span></div></div>"
                    f"<div style='color:var(--text);font-size:0.86rem;margin-top:8px;line-height:1.55;'>"
                    f"{w['rationale']}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if w["sources"]:
                    src_html = " · ".join(
                        f"<a href='{u}' target='_blank' style='color:var(--text-muted);font-size:0.72rem;'>{n}</a>"
                        for n, u in w["sources"]
                    )
                    st.markdown(
                        f"<div style='margin:-4px 0 14px 18px;font-size:0.72rem;'>"
                        f"<span style='color:var(--text-faint);'>📚 Sources:</span> {src_html}</div>",
                        unsafe_allow_html=True,
                    )

    # ──────────────────────────────────────────────────────────────────
    #  TAB 2 — PLAN A PURCHASE (personalized)
    # ──────────────────────────────────────────────────────────────────
    with tab_plan:
        st.markdown("### 🎯 What are you planning to buy?")
        st.caption("Pick a category — we'll show you the best window, runner-ups, and let you save it to your watchlist.")

        cat_keys, cat_titles = _category_pick()

        # Big visual category picker — buttons in a grid instead of a tiny dropdown
        cols = st.columns(4)
        for i, k in enumerate(cat_keys):
            cat = buy_calendar.get_category(k)
            with cols[i % 4]:
                is_selected = st.session_state.get("buytime_picked_cat") == k
                btn_label = f"{cat['icon']}\n{cat['title']}"
                if st.button(
                    btn_label,
                    key=f"buytime_pick_{k}",
                    use_container_width=True,
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state["buytime_picked_cat"] = k
                    st.rerun()

        picked = st.session_state.get("buytime_picked_cat")
        if not picked:
            st.markdown(
                "<div style='text-align:center;color:var(--text-faint);padding:24px 0;font-size:0.9rem;'>"
                "👆 Pick a category above to see your buying plan.</div>",
                unsafe_allow_html=True,
            )
        else:
            cat = buy_calendar.get_category(picked)
            st.markdown(f"## {cat['icon']} {cat['title']}")
            if cat.get("subtitle"):
                st.markdown(f"<p style='color:var(--text-muted);margin:-8px 0 12px;font-size:0.88rem;'>"
                            f"{cat['subtitle']}</p>", unsafe_allow_html=True)

            # Honest reality-check note if present
            if cat.get("honest_note"):
                st.markdown(
                    f"<div style='background:var(--fill-warn);border-left:3px solid var(--warn);"
                    f"border-radius:0 10px 10px 0;padding:12px 16px;margin:8px 0 16px;font-size:0.88rem;'>"
                    f"⚠️ <b>Reality check:</b> {cat['honest_note']}</div>",
                    unsafe_allow_html=True,
                )

            # Compute upcoming windows for this category, sorted by start
            upcoming_for_cat = [w for w in all_windows if w["category_key"] == picked]
            if not upcoming_for_cat:
                st.info("No upcoming windows known for this category in the next year.")
            else:
                # ── Best recommendation: highest-confidence within the next 6 months ──
                horizon_6mo = today + datetime.timedelta(days=180)
                six_month = [w for w in upcoming_for_cat if w["start"] <= horizon_6mo]
                if six_month:
                    # Prioritize: HIGH-confidence active > HIGH-confidence soon > MED-confidence soon
                    def _rank(w):
                        conf_score = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(w["confidence"], 3)
                        active_bonus = -1 if w["is_active"] else 0
                        return (conf_score + active_bonus, w["start"])
                    best = sorted(six_month, key=_rank)[0]

                    is_active = best["is_active"]
                    icon = "🔥" if is_active else "🎯"
                    headline = "Buy now — active window" if is_active else "Best window coming up"

                    st.markdown(
                        f"""<div style='background:linear-gradient(135deg,
                            {'rgba(34,197,94,0.12)' if is_active else 'rgba(59,130,246,0.10)'} 0%,
                            transparent 100%);
                            border:1px solid {'var(--accent)' if is_active else 'var(--info)'};
                            border-left:4px solid {'var(--accent)' if is_active else 'var(--info)'};
                            border-radius:14px;padding:18px 22px;margin-bottom:16px;'>
                            <div style='font-size:0.78rem;color:{'var(--accent)' if is_active else 'var(--info)'};
                                font-weight:700;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:4px;'>
                                {icon} {headline}
                            </div>
                            <h3 style='margin:0;color:var(--text-heading);font-size:1.2rem;'>
                                {best['name']}
                            </h3>
                            <div style='display:flex;gap:16px;flex-wrap:wrap;margin:8px 0;font-size:0.86rem;'>
                                <span style='color:var(--accent);font-weight:600;'>💸 {best['discount_range']}</span>
                                <span style='color:var(--text-muted);'>📅 {best['label']}</span>
                                <span style='color:var(--warn);font-weight:600;'>
                                    {('⏳ ' + str((best['end']-today).days) + ' days left') if is_active
                                     else ('🗓️ Starts in ' + str(best['days_until']) + ' days')}
                                </span>
                                <span>{_conf_pill(best['confidence'])}</span>
                            </div>
                            <div style='color:var(--text);font-size:0.88rem;line-height:1.6;margin-top:6px;'>
                                {best['rationale']}
                            </div>
                        </div>""",
                        unsafe_allow_html=True,
                    )

                    # Sources for the best window
                    if best["sources"]:
                        src_html = " · ".join(
                            f"<a href='{u}' target='_blank' style='color:var(--text-muted);'>{n}</a>"
                            for n, u in best["sources"]
                        )
                        st.markdown(
                            f"<div style='margin:-12px 0 14px 4px;font-size:0.75rem;'>"
                            f"<span style='color:var(--text-faint);'>📚 Sources:</span> {src_html}</div>",
                            unsafe_allow_html=True,
                        )

                    # ── Add to watchlist button ──
                    bc1, bc2 = st.columns([1, 4])
                    with bc1:
                        if st.button("⭐ Add to watchlist", key=f"buytime_watch_{picked}",
                                     use_container_width=True, type="primary"):
                            wl = data["buytime"].setdefault("watchlist", [])
                            # Avoid duplicates
                            existing = next((x for x in wl if x["category_key"] == picked
                                             and x["window_name"] == best["name"]), None)
                            if existing:
                                st.warning("Already in your watchlist.")
                            else:
                                wl.append({
                                    "id":           f"wl_{int(time.time() * 1000)}",
                                    "category_key": picked,
                                    "category_title": cat["title"],
                                    "category_icon":  cat["icon"],
                                    "window_name":  best["name"],
                                    "added_on":     today.isoformat(),
                                    "notes":        "",
                                })
                                save_data(data)
                                st.success(f"✅ Added {cat['title']} to your watchlist!")
                                st.rerun()

                # ── Other windows for this category (timeline) ──
                runner_ups = [w for w in upcoming_for_cat
                              if not (six_month and w["start"] == best["start"]
                                      and w["name"] == best["name"])][:6]
                if runner_ups:
                    st.markdown("#### 🗓️ Other windows for this category")
                    for w in runner_ups:
                        days_text = (
                            f"⏳ {(w['end']-today).days}d left" if w["is_active"]
                            else f"in {w['days_until']}d"
                        )
                        color = "var(--accent)" if w["is_active"] else "var(--text-dim)"
                        st.markdown(
                            f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                            f"border-radius:10px;padding:10px 14px;margin-bottom:6px;"
                            f"display:flex;justify-content:space-between;align-items:center;'>"
                            f"<div><b>{w['name']}</b><br>"
                            f"<span style='font-size:0.78rem;color:var(--text-muted);'>"
                            f"📅 {w['label']} · 💸 {w['discount_range']}</span></div>"
                            f"<div style='text-align:right;'>{_conf_pill(w['confidence'])}<br>"
                            f"<span style='color:{color};font-weight:600;font-size:0.78rem;'>"
                            f"{days_text}</span></div></div>",
                            unsafe_allow_html=True,
                        )

    # ──────────────────────────────────────────────────────────────────
    #  TAB 3 — WATCHLIST
    # ──────────────────────────────────────────────────────────────────
    with tab_watchlist:
        st.markdown("### ⭐ Your Watchlist")
        st.caption("Things you're planning to buy. We'll show you the next window for each.")

        wl = data["buytime"].setdefault("watchlist", [])
        if not wl:
            st.markdown(
                "<div style='background:var(--bg-surface);border:1px dashed var(--border-2);"
                "border-radius:12px;padding:32px 18px;text-align:center;color:var(--text-muted);'>"
                "<div style='font-size:2.4rem;margin-bottom:8px;'>📭</div>"
                "<div style='font-size:0.95rem;'>Your watchlist is empty.</div>"
                "<div style='font-size:0.82rem;margin-top:6px;'>"
                "Pick a category in the <b>Plan a purchase</b> tab and tap ⭐ to save it here.</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        else:
            for item in wl[:]:
                # Re-resolve the next window for this watchlist entry
                cat_windows = [w for w in all_windows if w["category_key"] == item["category_key"]]
                # Try to find the same window we saved; if not found (date passed), grab the next one
                matching = [w for w in cat_windows if w["name"] == item["window_name"]]
                next_w = matching[0] if matching else (cat_windows[0] if cat_windows else None)

                if next_w:
                    is_active = next_w["is_active"]
                    if is_active:
                        accent = "var(--accent)"
                        status = f"🔥 Active now — {(next_w['end']-today).days} days left"
                    elif next_w["days_until"] <= 30:
                        accent = "var(--warn)"
                        status = f"⏰ Starts in {next_w['days_until']} days"
                    else:
                        accent = "var(--info)"
                        status = f"🗓️ {next_w['start'].strftime('%b %d, %Y')}"

                    cols_w = st.columns([5, 2, 1])
                    with cols_w[0]:
                        st.markdown(
                            f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                            f"border-left:4px solid {accent};border-radius:10px;padding:12px 16px;'>"
                            f"<b style='font-size:1rem;'>{item['category_icon']} {item['category_title']}</b><br>"
                            f"<span style='color:var(--text-muted);font-size:0.82rem;'>"
                            f"{next_w['name']} · 💸 {next_w['discount_range']}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    with cols_w[1]:
                        st.markdown(
                            f"<div style='padding:14px 0;text-align:center;'>"
                            f"<span style='color:{accent};font-weight:600;font-size:0.85rem;'>{status}</span></div>",
                            unsafe_allow_html=True,
                        )
                    with cols_w[2]:
                        if st.button("🗑️", key=f"buytime_wl_del_{item['id']}", help="Remove from watchlist"):
                            data["buytime"]["watchlist"] = [x for x in wl if x["id"] != item["id"]]
                            save_data(data)
                            st.rerun()
                else:
                    cols_w = st.columns([6, 1])
                    cols_w[0].markdown(
                        f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                        f"border-radius:10px;padding:12px 16px;'>"
                        f"<b>{item['category_icon']} {item['category_title']}</b> "
                        f"<span style='color:var(--text-faint);font-size:0.78rem;'>(no upcoming windows)</span></div>",
                        unsafe_allow_html=True,
                    )
                    with cols_w[1]:
                        if st.button("🗑️", key=f"buytime_wl_del_{item['id']}"):
                            data["buytime"]["watchlist"] = [x for x in wl if x["id"] != item["id"]]
                            save_data(data)
                            st.rerun()

    # ──────────────────────────────────────────────────────────────────
    #  TAB 4 — PRICE TRENDS (data analysis on scraped history)
    # ──────────────────────────────────────────────────────────────────
    with tab_trends:
        st.markdown("### 📊 Trends from Thrivo's Scraped Data")
        st.caption(
            "Statistical analysis of YOUR price history — gold, USD/EGP, BTC. "
            "Recommendations are honest about what the data does and doesn't say."
        )

        try:
            assets_to_check = [
                ("gold_k21", "🥇 Gold 21k (EGP/gram)"),
                ("gold_k24", "🥇 Gold 24k (EGP/gram)"),
                ("usd_egp",  "💵 USD/EGP"),
                ("btc",      "₿ Bitcoin (USD)"),
            ]
            histories = {}
            for asset_key, label in assets_to_check:
                try:
                    h = db.load_price_history(asset_key, days=365)
                    if h and len(h) >= 14:
                        histories[asset_key] = (label, h)
                except Exception:
                    continue

            if not histories:
                st.markdown(
                    "<div style='background:var(--bg-surface);border:1px dashed var(--border-2);"
                    "border-radius:12px;padding:28px 18px;text-align:center;color:var(--text-muted);'>"
                    "<div style='font-size:2.2rem;margin-bottom:8px;'>📡</div>"
                    "<div style='font-size:0.95rem;'>No price history yet.</div>"
                    "<div style='font-size:0.82rem;margin-top:6px;'>"
                    "The daily scraper needs ~14 days to populate data. Trigger it manually from "
                    "your GitHub Actions tab to seed faster.</div></div>",
                    unsafe_allow_html=True,
                )
            else:
                for asset_key, (label, history) in histories.items():
                    analysis = buy_calendar.analyze_price_history(history, asset_name=label)

                    verdict = analysis["verdict"]
                    if verdict == "BUY_NOW":
                        verdict_color = "var(--accent)";  verdict_emoji = "✅"
                    elif verdict == "WAIT":
                        verdict_color = "var(--danger)";  verdict_emoji = "⏸️"
                    elif verdict == "NEUTRAL":
                        verdict_color = "var(--info)";    verdict_emoji = "↔️"
                    else:
                        verdict_color = "var(--text-dim)"; verdict_emoji = "❓"

                    with st.container():
                        st.markdown(
                            f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                            f"border-left:4px solid {verdict_color};border-radius:12px;padding:16px 20px;margin-bottom:12px;'>"
                            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
                            f"<h4 style='margin:0;color:var(--text-heading);font-size:1.05rem;'>{label}</h4>"
                            f"<span style='color:{verdict_color};font-weight:700;font-size:1.05rem;'>"
                            f"{verdict_emoji} {verdict.replace('_', ' ')}</span></div>"
                            f"<div style='color:var(--text);font-size:0.88rem;margin-top:8px;line-height:1.55;'>"
                            f"{analysis['explanation']}</div>"
                            f"<div style='color:var(--text-dim);font-size:0.75rem;margin-top:6px;'>"
                            f"📈 {analysis['n_days']} days of data · Confidence: {_conf_pill(analysis['confidence'])}</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                        if analysis.get("avg"):
                            sc1, sc2, sc3, sc4 = st.columns(4)
                            sc1.metric("Current", f"{analysis['current']:,.2f}")
                            sc2.metric("Average", f"{analysis['avg']:,.2f}")
                            sc3.metric("Min",     f"{analysis['min']:,.2f}")
                            sc4.metric("Max",     f"{analysis['max']:,.2f}")

                        if len(history) >= 14:
                            df_h = pd.DataFrame([{"date": h["date"], "value": h["value"]} for h in history])
                            df_h["date"] = pd.to_datetime(df_h["date"])
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(
                                x=df_h["date"], y=df_h["value"],
                                mode="lines",
                                line=dict(color="#22c55e", width=2),
                                fill="tozeroy",
                                fillcolor="rgba(34,197,94,0.06)",
                            ))
                            if analysis.get("avg"):
                                fig.add_hline(
                                    y=analysis["avg"], line_dash="dash", line_color="#94a3b8",
                                    annotation_text=f"avg {analysis['avg']:,.2f}",
                                    annotation_position="top right",
                                    annotation_font_color="#94a3b8",
                                )
                            fig.update_layout(
                                height=240,
                                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                xaxis=dict(color="#94a3b8", showgrid=False),
                                yaxis=dict(color="#94a3b8", showgrid=True, gridcolor="#1e293b"),
                                margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                            )
                            st.plotly_chart(fig, use_container_width=True)

                        if analysis.get("best_months"):
                            month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                            best_str  = " · ".join(f"{month_names[m]} ({v:,.2f})"
                                                   for m, v in analysis["best_months"])
                            worst_str = " · ".join(f"{month_names[m]} ({v:,.2f})"
                                                   for m, v in analysis["worst_months"])
                            st.markdown(
                                f"<div style='font-size:0.85rem;color:var(--text-muted);margin-top:6px;'>"
                                f"📉 <b>Cheapest months historically:</b> {best_str}<br>"
                                f"📈 <b>Priciest months historically:</b> {worst_str}</div>",
                                unsafe_allow_html=True,
                            )
                        st.markdown("<br>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Could not load price history: {e}")
            st.caption("This is normal on a fresh deploy. Wait for the scraper to populate data.")

    # ──────────────────────────────────────────────────────────────────
    #  TAB 5 — SAVINGS LOG
    # ──────────────────────────────────────────────────────────────────
    with tab_savings:
        st.markdown("### 💰 Savings Log")
        st.caption("Track money saved by buying in-window. Builds visible value of timing your purchases.")

        savings_log = data["buytime"].setdefault("savings_log", [])

        # Form to add a new entry
        with st.expander("➕ Log a purchase you timed well", expanded=not savings_log):
            with st.form("buytime_savings_form", clear_on_submit=True):
                cat_keys, cat_titles = _category_pick()
                c1, c2 = st.columns(2)
                with c1:
                    s_cat = st.selectbox("Category", options=cat_keys,
                                         format_func=lambda k: cat_titles[k])
                    s_item = st.text_input("What did you buy?", placeholder="e.g. iPhone 15 Pro 128GB")
                    s_paid = st.number_input("Price you paid (EGP)", min_value=0.0, step=100.0, value=0.0)
                with c2:
                    s_full = st.number_input("Full retail price (EGP)", min_value=0.0, step=100.0, value=0.0,
                                             help="The price before the discount window")
                    s_when = st.date_input("Purchase date", value=today)
                    s_window = st.text_input("Which window?", placeholder="e.g. White Friday 2026, Pre-Eid")

                if st.form_submit_button("💾 Log savings", type="primary", use_container_width=True):
                    if s_item and s_paid > 0 and s_full > s_paid:
                        savings_log.append({
                            "id":       f"sv_{int(time.time() * 1000)}",
                            "category_key":  s_cat,
                            "category_icon": buy_calendar.get_category(s_cat)["icon"],
                            "category_title": buy_calendar.get_category(s_cat)["title"],
                            "item":     s_item,
                            "paid":     float(s_paid),
                            "full":     float(s_full),
                            "saved":    float(s_full - s_paid),
                            "saved_pct": round((s_full - s_paid) / s_full * 100, 1),
                            "date":     s_when.isoformat(),
                            "window":   s_window,
                        })
                        save_data(data)
                        st.success(f"✅ Logged {s_full - s_paid:,.0f} EGP saved!")
                        st.rerun()
                    else:
                        st.error("Please fill in all fields. Full price must be higher than paid price.")

        if not savings_log:
            st.markdown(
                "<div style='background:var(--bg-surface);border:1px dashed var(--border-2);"
                "border-radius:12px;padding:24px 18px;text-align:center;color:var(--text-muted);'>"
                "<div style='font-size:2.2rem;margin-bottom:8px;'>🪙</div>"
                "<div style='font-size:0.92rem;'>No savings logged yet.</div>"
                "<div style='font-size:0.82rem;margin-top:6px;'>"
                "Log your first well-timed purchase above to see how much you've saved.</div></div>",
                unsafe_allow_html=True,
            )
        else:
            # Top stats
            total_saved = sum(s["saved"] for s in savings_log)
            avg_pct     = sum(s["saved_pct"] for s in savings_log) / len(savings_log)
            sm1, sm2, sm3 = st.columns(3)
            sm1.metric("Total saved",      f"{total_saved:,.0f} EGP")
            sm2.metric("Avg discount",     f"{avg_pct:.1f}%")
            sm3.metric("Purchases logged", len(savings_log))

            st.markdown("---")
            for s in sorted(savings_log, key=lambda x: x["date"], reverse=True):
                cols_s = st.columns([5, 2, 1])
                with cols_s[0]:
                    st.markdown(
                        f"<div style='background:var(--bg-surface);border:1px solid var(--border-2);"
                        f"border-left:3px solid var(--accent);border-radius:10px;padding:11px 16px;'>"
                        f"<b>{s['category_icon']} {s['item']}</b><br>"
                        f"<span style='font-size:0.78rem;color:var(--text-muted);'>"
                        f"{s['date']} · {s.get('window', '—')}</span></div>",
                        unsafe_allow_html=True,
                    )
                with cols_s[1]:
                    st.markdown(
                        f"<div style='padding:8px 0;text-align:right;'>"
                        f"<span style='color:var(--accent);font-weight:700;font-family:JetBrains Mono,monospace;'>"
                        f"+{s['saved']:,.0f} EGP</span><br>"
                        f"<span style='color:var(--text-dim);font-size:0.78rem;'>"
                        f"-{s['saved_pct']:.1f}% off {s['full']:,.0f}</span></div>",
                        unsafe_allow_html=True,
                    )
                with cols_s[2]:
                    if st.button("🗑️", key=f"buytime_sv_del_{s['id']}"):
                        data["buytime"]["savings_log"] = [x for x in savings_log if x["id"] != s["id"]]
                        save_data(data)
                        st.rerun()

