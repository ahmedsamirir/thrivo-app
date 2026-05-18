#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  Thrivo Public Price Scraper
  ─────────────────────────────────────────────────────────────────────
  Fetches gold (Egypt 21k/24k), USD/EGP, BTC, and EGX stock prices
  from public sources. Writes snapshots and history via db.py.

  Runs in two modes:
    1. As a GitHub Actions cron job (daily, 06:00 Cairo / 04:00 UTC)
       → writes to Postgres via DATABASE_URL secret
       → also commits public_prices.json + public_price_history.json
         back to the repo so the app can read them even without DB
    2. On-demand from the Streamlit app (via fetch_public_prices_cached)
       when the data is older than 24h (failsafe if cron stops working)

  Sources (updated v10.5 — April 2026 incident response):
    Gold      — goldpricez.com   (primary), pricegold.net (fallback)
    USD/EGP   — goldpricez.com   (primary), exchangerate.host (fallback)
    BTC       — coingecko public API (no auth needed)
    EGX top8  — stockanalysis.com (primary), mubasher.info (fallback)

  Exit code: 0 always (we don't want cron alerts on a single source flap)
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import os
import sys
import json
import datetime
import re
import traceback
from typing import Any

import requests
from bs4 import BeautifulSoup

# Make sibling modules importable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

try:
    import db  # type: ignore
except Exception as e:
    print(f"⚠️  Could not import db module: {e}")
    db = None  # we'll still write JSON files

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _to_float(s: str) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").replace("\xa0", "").strip()
    s = re.sub(r"[^\d\.\-]", "", s)
    try:
        return float(s) if s else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
#  GOLD (Egypt) — goldpricez.com primary, pricegold.net fallback
#  ─────────────────────────────────────────────────────────────────────
#  History: the original scraper used goldbullioneg.com which depends on
#  matching the Arabic word "عيار" in changing HTML. It silently failed
#  for ~30 hours in production (April 2026 incident). Switched to
#  goldpricez.com which has a stable per-karat URL pattern and renders
#  the price as plain text like "= 6,861.84 EGP".
# ──────────────────────────────────────────────────────────────────────
def _fetch_gold_from_goldpricez() -> dict | None:
    """Hit goldpricez.com/eg/{karat}k/gram for each karat we care about."""
    out: dict[str, float] = {}
    for karat in ("24", "22", "21", "18"):
        try:
            url = f"https://goldpricez.com/eg/{karat}k/gram"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            text = r.text
            # The page renders the price as: "= 6,861.84\nEGP" or in nearby HTML
            # like "<...>6,861.84</...>EGP".  Use a tolerant regex.
            m = re.search(r"=\s*([\d,]+\.\d{1,2})\s*[\r\n\s]*EGP", text)
            if not m:
                # Alt pattern — sometimes inside a strong/span before EGP
                m = re.search(r">\s*([\d,]+\.\d{1,2})\s*<[^>]+>\s*EGP", text)
            if not m:
                continue
            v = _to_float(m.group(1))
            if v and 100 < v < 100000:  # sanity range
                out[f"k{karat}"] = v
        except Exception:
            traceback.print_exc()
            continue
    return out or None


def _fetch_gold_from_pricegold() -> dict | None:
    """Fallback source: pricegold.net/eg-egypt/ — has all karats on one page."""
    try:
        r = requests.get("https://pricegold.net/eg-egypt/",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        text = r.text
        out: dict[str, float] = {}
        # Pattern: <td>21k</td>...<td>...8,124 EGP</td> or similar
        for karat in ("24", "22", "21", "18"):
            # Look for "Nk" near a price cell in EGP
            patterns = [
                rf"{karat}k[^<]*</[^>]+>\s*<[^>]+>\s*([\d,]+\.?\d*)\s*EGP",
                rf"{karat}\s*Karat[^<]*<[^>]+>\s*([\d,]+\.?\d*)",
                rf"per\s*Gram[^<]*{karat}[^<]*<[^>]+>\s*([\d,]+\.?\d*)",
            ]
            for pat in patterns:
                m = re.search(pat, text, re.S | re.I)
                if m:
                    v = _to_float(m.group(1))
                    if v and 100 < v < 100000:
                        out[f"k{karat}"] = v
                        break
        return out or None
    except Exception:
        traceback.print_exc()
        return None


def fetch_gold() -> dict | None:
    out = _fetch_gold_from_goldpricez()
    source = "goldpricez.com"
    if not out:
        out = _fetch_gold_from_pricegold()
        source = "pricegold.net"
    if not out:
        return None
    return {
        "asset":    "gold_egp",
        "currency": "EGP",
        "unit":     "gram",
        "values":   out,
        "source":   source,
    }


# ──────────────────────────────────────────────────────────────────────
#  USD / EGP — goldpricez.com primary, exchangerate.host fallback
#  ─────────────────────────────────────────────────────────────────────
#  History: investing.com aggressively blocks bots, so the scraper was
#  silently failing ~50% of the time (returning 403 or empty HTML).
#  goldpricez.com publishes the rate openly and never rate-limits.
# ──────────────────────────────────────────────────────────────────────
def _fetch_usd_egp_from_goldpricez() -> float | None:
    try:
        # The page literally shows "USD/EGP 52.79" near the top
        r = requests.get("https://goldpricez.com/currency-rates/egypt",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        # Pattern variants seen in the wild
        for pat in (
            r"USD\s*Exchange\s*Rate[^\d]{0,200}([\d]{2,3}\.\d{2,4})",
            r"USD/EGP[^\d]{0,200}([\d]{2,3}\.\d{2,4})",
            r"1\s*USD\s*=\s*([\d]{2,3}\.\d{2,4})\s*EGP",
            r"\$1\.00\s*=\s*EGP\s*([\d]{2,3}\.\d{2,4})",
        ):
            m = re.search(pat, r.text, re.S | re.I)
            if m:
                v = _to_float(m.group(1))
                if v and 5 < v < 1000:
                    return v
        return None
    except Exception:
        traceback.print_exc()
        return None


def _fetch_usd_egp_from_exchangerate_host() -> float | None:
    """exchangerate.host is a free, no-key API — used only as a fallback."""
    try:
        r = requests.get(
            "https://api.exchangerate.host/latest?base=USD&symbols=EGP",
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            return None
        rate = (r.json().get("rates") or {}).get("EGP")
        if rate and 5 < float(rate) < 1000:
            return float(rate)
    except Exception:
        traceback.print_exc()
    return None


def fetch_usd_egp() -> dict | None:
    rate = _fetch_usd_egp_from_goldpricez()
    source = "goldpricez.com"
    if not rate:
        rate = _fetch_usd_egp_from_exchangerate_host()
        source = "exchangerate.host"
    if not rate:
        return None
    # Try to fetch yesterday's rate for change_pct (best-effort, may be None)
    return {
        "asset":      "usd_egp",
        "rate":       rate,
        "prev_close": rate,        # we don't have reliable prev close anymore
        "change_pct": 0,
        "source":     source,
    }


# ──────────────────────────────────────────────────────────────────────
#  BTC — CoinGecko public API
# ──────────────────────────────────────────────────────────────────────
def fetch_btc() -> dict | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd,egp"
            "&include_24hr_change=true&include_last_updated_at=true",
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            return None
        j = r.json().get("bitcoin", {})
        usd = j.get("usd")
        egp = j.get("egp")
        if not usd:
            return None
        return {
            "asset":          "btc",
            "usd":            float(usd),
            "egp":            float(egp) if egp else None,
            "change_pct_24h": float(j.get("usd_24h_change", 0) or 0),
            "source":         "coingecko",
        }
    except Exception:
        traceback.print_exc()
        return None


# ──────────────────────────────────────────────────────────────────────
#  EGX — stockanalysis.com primary, mubasher.info fallback
#  ─────────────────────────────────────────────────────────────────────
#  History: original used investing.com slugs that were WRONG
#  (e.g. "commercial-intl-bank-(egypt)" → 404). The actual investing.com
#  URL for COMI is /equities/com-intl-bk. Rather than maintain that
#  brittle slug list, switched to stockanalysis.com which uses the
#  actual ticker symbol — much more stable.
# ──────────────────────────────────────────────────────────────────────
EGX_TICKERS = ["COMI", "ETEL", "TMGH", "FWRY", "SWDY", "HRHO", "ORHD", "MNHD"]


def _fetch_egx_one_from_stockanalysis(ticker: str) -> dict | None:
    try:
        r = requests.get(f"https://stockanalysis.com/quote/egx/{ticker}/",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        text = r.text
        # The page contains JSON-LD with price, plus visible HTML with
        # patterns like: "<span ...>123.45</span>" near "EGP"
        # Try multiple resilient patterns.
        price = None
        for pat in (
            r'"price"\s*:\s*"?([\d.]+)"?',
            rf'\b{ticker}\b[^<]*</[^>]+>[^<]*<[^>]+>\s*([\d]{{1,5}}\.\d{{1,4}})',
            r'data-symbol-last[^>]*>\s*([\d]{1,5}\.\d{1,4})',
            r'"regularMarketPrice"\s*:\s*([\d.]+)',
        ):
            m = re.search(pat, text)
            if m:
                v = _to_float(m.group(1))
                if v and 0.01 < v < 100000:  # sanity
                    price = v
                    break
        if not price:
            return None
        # Try to find prev close
        prev = price
        m = re.search(r'"previousClose"\s*:\s*([\d.]+)', text)
        if m:
            v = _to_float(m.group(1))
            if v and 0.01 < v < 100000:
                prev = v
        return {
            "ticker":     ticker,
            "price":      price,
            "prev_close": prev,
            "change_pct": round((price - prev) / prev * 100, 3) if prev else 0,
            "source":     "stockanalysis.com",
        }
    except Exception:
        return None


def _fetch_egx_one_from_mubasher(ticker: str) -> dict | None:
    """Fallback — pull price from english.mubasher.info."""
    try:
        r = requests.get(
            f"https://english.mubasher.info/markets/EGX/stocks/{ticker}",
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            return None
        text = r.text
        # Mubasher renders e.g. "<span class='last'>123.45</span>" or similar
        m = re.search(r'class=["\']last["\'][^>]*>\s*([\d.]+)\s*<', text)
        if not m:
            m = re.search(r'data-field=["\']last["\'][^>]*>\s*([\d.]+)', text)
        if not m:
            return None
        price = _to_float(m.group(1))
        if not price or price <= 0 or price > 100000:
            return None
        return {
            "ticker":     ticker,
            "price":      price,
            "prev_close": price,
            "change_pct": 0,
            "source":     "mubasher.info",
        }
    except Exception:
        return None


def fetch_egx() -> list[dict]:
    out = []
    for tk in EGX_TICKERS:
        row = _fetch_egx_one_from_stockanalysis(tk)
        if not row:
            row = _fetch_egx_one_from_mubasher(tk)
        if row:
            out.append(row)
    return out


# ──────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    today = datetime.date.today().isoformat()
    started = datetime.datetime.utcnow().isoformat()
    results: dict[str, Any] = {
        "_run_at": started,
        "_date":   today,
    }

    print("┌─ Thrivo Price Scraper")
    print(f"│  Run at: {started}")
    print(f"│  Backend: {db.get_backend_kind() if db else 'JSON only'}")
    print("├──────────────────────────")

    # Gold
    gold = fetch_gold()
    if gold:
        results["gold"] = gold
        print(f"│  ✓ Gold: {gold['values']}")
        if db:
            db.save_price("gold", gold)
            for karat, val in gold["values"].items():
                db.append_price_history(f"gold_{karat}", today, val, {"karat": karat})
    else:
        print("│  ✗ Gold: failed")

    # USD/EGP
    usd = fetch_usd_egp()
    if usd:
        results["usd_egp"] = usd
        print(f"│  ✓ USD/EGP: {usd['rate']:.4f} ({usd['change_pct']:+.2f}%)")
        if db:
            db.save_price("usd_egp", usd)
            db.append_price_history("usd_egp", today, usd["rate"])
    else:
        print("│  ✗ USD/EGP: failed")

    # BTC
    btc = fetch_btc()
    if btc:
        results["btc"] = btc
        print(f"│  ✓ BTC: ${btc['usd']:,.0f} ({btc['change_pct_24h']:+.2f}%)")
        if db:
            db.save_price("btc", btc)
            db.append_price_history("btc", today, btc["usd"])
    else:
        print("│  ✗ BTC: failed")

    # EGX
    egx = fetch_egx()
    if egx:
        results["egx"] = egx
        print(f"│  ✓ EGX: {len(egx)} stocks")
        if db:
            db.save_price("egx", {"stocks": egx, "source": "investing.com"})
            for s in egx:
                db.append_price_history(f"egx_{s['ticker']}", today, s["price"])
    else:
        print("│  ✗ EGX: 0 stocks")

    # Always write the JSON snapshots (so the cache backup works even when
    # Postgres is offline, and so the GitHub Action can commit them)
    snapshot_path = os.path.join(ROOT, "public_prices.json")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"│  → wrote {snapshot_path}")

    print("└─ Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())