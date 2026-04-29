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

  Sources:
    Gold      — goldbullioneg.com   (21k/24k EGP per gram)
    USD/EGP   — investing.com       (interbank quote)
    BTC       — coingecko public API (no auth needed)
    EGX top5  — investing.com       (COMI, ETEL, ORHD, MNHD, COMI)

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
#  GOLD (Egypt) — goldbullioneg.com
# ──────────────────────────────────────────────────────────────────────
def fetch_gold() -> dict | None:
    try:
        url = "https://goldbullioneg.com/%D8%A3%D8%B3%D8%B9%D8%A7%D8%B1-%D8%A7%D9%84%D8%B0%D9%87%D8%A8/"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Heuristics — sites change layout often, so we look for keywords
        out: dict[str, float] = {}
        for karat, label in [("24", "24"), ("21", "21"), ("18", "18")]:
            # Pattern: "<karat>k ... <number>"
            m = re.search(rf"عيار\s*{karat}[^\d]{{0,80}}([\d,]+\.?\d*)", text)
            if m:
                v = _to_float(m.group(1))
                if v and 100 < v < 100000:  # sanity
                    out[f"k{karat}"] = v
        if not out:
            return None
        return {
            "asset":    "gold_egp",
            "currency": "EGP",
            "unit":     "gram",
            "values":   out,
            "source":   "goldbullioneg.com",
        }
    except Exception:
        traceback.print_exc()
        return None


# ──────────────────────────────────────────────────────────────────────
#  USD / EGP — investing.com
# ──────────────────────────────────────────────────────────────────────
def fetch_usd_egp() -> dict | None:
    try:
        r = requests.get("https://www.investing.com/currencies/usd-egp",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        # Pattern: trading at a price of X.XX EGP
        m = re.search(r"trading at a price of\s*([\d,.]+)\s*EGP", r.text, re.S | re.I)
        if not m:
            # Fallback — generic large-number pattern after USD/EGP
            m = re.search(r'data-test="instrument-price-last"[^>]*>\s*([\d,.]+)', r.text)
        if not m:
            return None
        rate = _to_float(m.group(1))
        if not rate or rate < 5 or rate > 1000:
            return None
        m_prev = re.search(r"previous close of\s*([\d,.]+)", r.text, re.S | re.I)
        prev = _to_float(m_prev.group(1)) if m_prev else rate
        return {
            "asset":      "usd_egp",
            "rate":       rate,
            "prev_close": prev,
            "change_pct": round((rate - prev) / prev * 100, 3) if prev else 0,
            "source":     "investing.com",
        }
    except Exception:
        traceback.print_exc()
        return None


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
#  EGX — investing.com (top 5 most-watched)
# ──────────────────────────────────────────────────────────────────────
EGX_SLUGS = {
    "COMI": "commercial-intl-bank-(egypt)",
    "ETEL": "telecom-egypt",
    "ORHD": "orascom-development-egypt",
    "MNHD": "madinet-nasr-for-housing-and-development",
    "TMGH": "t-m-g-holding",
    "FWRY": "fawry-banking-and-payment",
    "SWDY": "elsewedy-cable",
    "HRHO": "ef-hermes-hold",
}

def fetch_egx() -> list[dict]:
    out = []
    for ticker, slug in EGX_SLUGS.items():
        try:
            r = requests.get(f"https://www.investing.com/equities/{slug}",
                             headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            m = re.search(
                r"trading at a price of\s*([\d,.]+)\s*EGP.*?previous close of\s*([\d,.]+)\s*EGP",
                r.text, re.S | re.I)
            if not m:
                continue
            price = _to_float(m.group(1))
            prev  = _to_float(m.group(2))
            if not price or price <= 0:
                continue
            out.append({
                "ticker":     ticker,
                "price":      price,
                "prev_close": prev or price,
                "change_pct": round((price - prev) / prev * 100, 3) if prev else 0,
                "source":     "investing.com",
            })
        except Exception:
            traceback.print_exc()
            continue
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
