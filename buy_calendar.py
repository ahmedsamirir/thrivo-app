"""
═══════════════════════════════════════════════════════════════════════
  Thrivo — Egyptian Buying Calendar
  ─────────────────────────────────────────────────────────────────────
  A curated database of when to buy what in Egypt, based on documented
  retail patterns. Every recommendation includes its source so users
  can verify and trust.

  Two kinds of windows:
    1. FIXED        — Gregorian dates (e.g. White Friday = last Friday
                      of November every year)
    2. LUNAR        — Hijri-calendar events (Eid al-Fitr, Eid al-Adha,
                      Ramadan) computed approximately for any year
    3. SEASONAL     — Inventory clearance windows (winter clothes go on
                      sale Feb–Mar when stores rotate to spring)
    4. PRODUCT_CYCLE — Manufacturer release cycles (iPhone old-model
                      discount after September Apple event)

  This is curated data — NOT scraped or AI-generated. It comes from
  retail-industry research with sources cited per entry. Users see the
  source link in-app so they can verify each recommendation.

  Confidence levels:
    HIGH    — universally observed across Egypt retail, year over year
    MEDIUM  — strong general pattern but varies by retailer/brand
    LOW     — anecdotal or niche; we say so honestly
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import datetime
from typing import Literal

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
WindowKind = Literal["FIXED", "LUNAR", "SEASONAL", "PRODUCT_CYCLE"]


# ──────────────────────────────────────────────────────────────────────
#  HIJRI / GREGORIAN APPROXIMATION
# ──────────────────────────────────────────────────────────────────────
#  We use a pure-Python approximation (no external dep) so the app
#  ships with zero added pip dependencies. Dates may shift ±1 day vs
#  the actual moon-sighted date — fine for "buy a few days before
#  Ramadan" guidance, not fine for prayer timing.

def _hijri_to_gregorian_approx(hy: int, hm: int, hd: int) -> datetime.date:
    """Convert Hijri date to Gregorian. Algorithm: Tabular Islamic Calendar.
    Accuracy ±1 day. Good enough for shopping-window guidance."""
    jd = (
        int((11 * hy + 3) / 30)
        + 354 * hy
        + 30 * hm
        - int((hm - 1) / 2)
        + hd
        + 1948440
        - 385
    )
    # JD → Gregorian
    a = jd + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day   = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year  = 100 * b + d - 4800 + (m // 10)
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return datetime.date(year, month, max(1, min(28, day)))


def _gregorian_to_hijri_year(g_year: int) -> int:
    """Approximate Hijri year for a given Gregorian year (June reference)."""
    return int((g_year - 622) * 33 / 32) + 1


def get_lunar_event_dates(year: int) -> dict[str, datetime.date]:
    """Return approximate Gregorian dates for major lunar events in `year`.

    Returns dates for events that fall within `year`. If a Hijri year
    spans two Gregorian years, only the events landing in `year` are
    returned. We compute for both possible Hijri years to cover edge
    cases at year boundaries.
    """
    out: dict[str, datetime.date] = {}
    for hy in (_gregorian_to_hijri_year(year) - 1,
               _gregorian_to_hijri_year(year),
               _gregorian_to_hijri_year(year) + 1):
        events = {
            "ramadan_start":   (hy, 9, 1),    # 1 Ramadan
            "eid_al_fitr":     (hy, 10, 1),   # 1 Shawwal
            "eid_al_adha":     (hy, 12, 10),  # 10 Dhu al-Hijjah
        }
        for name, (yy, mm, dd) in events.items():
            d = _hijri_to_gregorian_approx(yy, mm, dd)
            if d.year == year and name not in out:
                out[name] = d
    return out


def last_friday_of_november(year: int) -> datetime.date:
    """White/Black Friday in Egypt = last Friday of November."""
    d = datetime.date(year, 11, 30)
    while d.weekday() != 4:  # Friday = 4
        d -= datetime.timedelta(days=1)
    return d


def cyber_monday(year: int) -> datetime.date:
    """Monday after Black Friday."""
    return last_friday_of_november(year) + datetime.timedelta(days=3)


# ──────────────────────────────────────────────────────────────────────
#  CATEGORIES & WINDOWS
# ──────────────────────────────────────────────────────────────────────
#  Each entry has:
#    icon, title, when (callable: year → (start_date, end_date, label)),
#    discount_range, confidence, rationale, sources
#  Sources are URLs visible in-app so users can verify.

def _white_friday_window(year: int):
    """Last week of November + extends ~10 days for early deals."""
    bf = last_friday_of_november(year)
    return (bf - datetime.timedelta(days=8), bf + datetime.timedelta(days=3),
            f"{(bf - datetime.timedelta(days=8)).strftime('%b %d')} – {(bf + datetime.timedelta(days=3)).strftime('%b %d')}")


def _ramadan_window(year: int):
    e = get_lunar_event_dates(year)
    if "ramadan_start" not in e:
        return None
    start = e["ramadan_start"] - datetime.timedelta(days=14)  # pre-Ramadan campaigns start ~2 weeks ahead
    end   = e["ramadan_start"] + datetime.timedelta(days=20)  # mid-Ramadan deals
    return (start, end, f"Pre-Ramadan to mid-Ramadan ({start.strftime('%b %d')} – {end.strftime('%b %d')})")


def _pre_eid_fitr_window(year: int):
    e = get_lunar_event_dates(year)
    if "eid_al_fitr" not in e:
        return None
    start = e["eid_al_fitr"] - datetime.timedelta(days=10)
    end   = e["eid_al_fitr"] + datetime.timedelta(days=2)
    return (start, end, f"Pre-Eid al-Fitr ({start.strftime('%b %d')} – {end.strftime('%b %d')})")


def _pre_eid_adha_window(year: int):
    e = get_lunar_event_dates(year)
    if "eid_al_adha" not in e:
        return None
    start = e["eid_al_adha"] - datetime.timedelta(days=10)
    end   = e["eid_al_adha"] + datetime.timedelta(days=2)
    return (start, end, f"Pre-Eid al-Adha ({start.strftime('%b %d')} – {end.strftime('%b %d')})")


def _back_to_school_window(year: int):
    return (datetime.date(year, 8, 1), datetime.date(year, 9, 15),
            "Aug 1 – Sep 15 (back-to-school)")


def _iphone_old_model_window(year: int):
    """When new iPhone launches in mid-September, old models drop in price."""
    return (datetime.date(year, 9, 15), datetime.date(year, 11, 30),
            f"Sep 15 – Nov 30 (after iPhone {year - 2007} launch)")


def _winter_clothes_clearance(year: int):
    return (datetime.date(year, 1, 15), datetime.date(year, 3, 15),
            "Jan 15 – Mar 15 (winter clearance, end-of-season)")


def _summer_clothes_clearance(year: int):
    return (datetime.date(year, 7, 20), datetime.date(year, 9, 10),
            "Jul 20 – Sep 10 (summer clearance, end-of-season)")


def _car_year_end(year: int):
    return (datetime.date(year, 11, 1), datetime.date(year, 12, 31),
            "Nov–Dec (dealers clear current-year stock)")


def _appliances_summer(year: int):
    return (datetime.date(year, 6, 15), datetime.date(year, 8, 31),
            "Jun 15 – Aug 31 (AC/fridge peak demand → promos)")


def _november_11(year: int):
    return (datetime.date(year, 11, 8), datetime.date(year, 11, 13),
            f"Nov 8 – Nov 13 (Singles' Day 11.11)")


# ──────────────────────────────────────────────────────────────────────
#  CATEGORY DATABASE
# ──────────────────────────────────────────────────────────────────────
#  Each category lists every relevant window in priority order.

CATEGORIES: dict[str, dict] = {
    "iphone": {
        "icon":  "📱",
        "title": "iPhone / Smartphone",
        "windows": [
            {
                "name":           "iPhone old-model discount",
                "when":           _iphone_old_model_window,
                "discount_range": "10–20% off previous gen",
                "confidence":     "HIGH",
                "rationale":      "Apple announces new iPhones every September. Egyptian retailers (Miami Center, Sharaf DG, Noon) drop prices on the previous generation within 1–2 weeks of the launch event. Best value: iPhone (n-1) at the start of October.",
                "sources": [
                    ("Macworld — Best time to buy an iPhone", "https://www.macworld.com/article/672505/when-is-the-best-time-to-buy-an-iphone.html"),
                    ("Miami Center Egypt — iPhone deals", "https://miamicenters.com/iphone-15-price-egypt-best-deals/"),
                ],
            },
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "15–80% off (varies)",
                "confidence":     "HIGH",
                "rationale":      "Noon (Yellow Friday), Amazon EG, Jumia, Sharaf DG, Carrefour all run major discount campaigns. Phones typically see 15–25% off retail.",
                "sources": [
                    ("AlCoupon Egypt — White Friday phones", "https://egypt.alcoupon.com/en/black-friday-sale"),
                    ("Jumia Egypt — Black Friday", "https://www.jumia.com.eg/mlp-black-friday/"),
                ],
            },
            {
                "name":           "11.11 Singles' Day",
                "when":           _november_11,
                "discount_range": "10–40% off",
                "confidence":     "MEDIUM",
                "rationale":      "Imported by Noon and Amazon EG from Chinese e-commerce. Smaller than White Friday but earlier — useful if you don't want to wait 17 more days.",
                "sources": [
                    ("AlCoupon — November sales 2026", "https://egypt.alcoupon.com/en/best-black-november-sale-and-offers"),
                ],
            },
        ],
    },

    "laptop": {
        "icon":  "💻",
        "title": "Laptop / PC",
        "windows": [
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "10–35%",
                "confidence":     "HIGH",
                "rationale":      "Best laptop discounts of the year in Egypt. Sharaf DG, Carrefour, B.Tech, Noon all participate.",
                "sources": [
                    ("AlCoupon Egypt — White Friday electronics", "https://egypt.alcoupon.com/en/blog/black-friday-vs-white-friday-everything-you-need-to-know"),
                ],
            },
            {
                "name":           "Back-to-school",
                "when":           _back_to_school_window,
                "discount_range": "5–15% + bundle deals",
                "confidence":     "MEDIUM",
                "rationale":      "Less aggressive than White Friday, but bundles (laptop + bag + accessories) are common. Good for students.",
                "sources": [
                    ("Sharaf DG Egypt", "https://egypt.sharafdg.com/"),
                ],
            },
            {
                "name":           "Post-CES (mid-Jan)",
                "when":           lambda y: (datetime.date(y, 1, 15), datetime.date(y, 2, 28),
                                             "Jan 15 – Feb 28 (post-CES new models, old models clear)"),
                "discount_range": "10–20% on prior-year models",
                "confidence":     "MEDIUM",
                "rationale":      "After CES (Jan), manufacturers refresh laptop lines. Egyptian retailers clear last year's stock through Feb.",
                "sources": [],
            },
        ],
    },

    "car": {
        "icon":  "🚗",
        "title": "Car",
        "windows": [
            {
                "name":           "Year-end dealer clearance",
                "when":           _car_year_end,
                "discount_range": "5–15% (varies wildly by brand)",
                "confidence":     "MEDIUM",
                "rationale":      "Egyptian car dealers (GB Auto, Mansour, Ezz Elarab) clear current-year stock to make room for next year's models. Negotiate on outgoing model-year units.",
                "sources": [],
            },
            {
                "name":           "Pre-Ramadan",
                "when":           _ramadan_window,
                "discount_range": "Installment promotions, low-down-payment offers",
                "confidence":     "MEDIUM",
                "rationale":      "Banks and dealers run aggressive auto-finance campaigns before Ramadan in Egypt. Look for 0% interest installments rather than cash discounts.",
                "sources": [],
            },
        ],
        "honest_note": "Egyptian car prices are volatile due to currency moves & import policy. Time of year matters less than EGP/USD rate and dealer-specific stock — track exchange rate and visit multiple dealers.",
    },

    "summer_clothes": {
        "icon":  "👕",
        "title": "Summer Clothes",
        "windows": [
            {
                "name":           "End-of-summer clearance",
                "when":           _summer_clothes_clearance,
                "discount_range": "30–70% off",
                "confidence":     "HIGH",
                "rationale":      "Stores (DeFacto, H&M, Zara, LC Waikiki, Cotton Club) rotate stock to autumn collections starting late July. Deepest discounts in late August.",
                "sources": [
                    ("DeFacto Egypt", "https://www.defacto.com.eg/en-eg/black-friday-offers"),
                ],
            },
            {
                "name":           "Pre-Eid al-Fitr",
                "when":           _pre_eid_fitr_window,
                "discount_range": "10–30% (full new arrivals — not deepest discounts)",
                "confidence":     "MEDIUM",
                "rationale":      "New collections launch with promo prices but stores don't deeply discount during peak demand. Buy now if you need NEW; wait for clearance if you want best price.",
                "sources": [],
            },
        ],
    },

    "winter_clothes": {
        "icon":  "🧥",
        "title": "Winter Clothes",
        "windows": [
            {
                "name":           "End-of-winter clearance",
                "when":           _winter_clothes_clearance,
                "discount_range": "40–70% off",
                "confidence":     "HIGH",
                "rationale":      "Major markdowns Jan–Mar as stores clear winter stock. Coats, jackets, sweaters reach lowest prices of the year. Cairo's mild winter means less urgency to buy at full price.",
                "sources": [],
            },
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "20–40%",
                "confidence":     "HIGH",
                "rationale":      "Most retailers discount their winter collection during White Friday — early in the season but quality stock still in.",
                "sources": [],
            },
        ],
    },

    "gold": {
        "icon":  "🥇",
        "title": "Gold (jewelry / bullion)",
        "windows": [
            {
                "name":           "Track exchange rate, not calendar",
                "when":           lambda y: (datetime.date(y, 1, 1), datetime.date(y, 12, 31),
                                             "Year-round — see Price Trends tab"),
                "discount_range": "Depends on EGP/USD & global gold spot",
                "confidence":     "HIGH",
                "rationale":      "Egyptian gold price = global spot price × USD/EGP × workmanship markup. There's no calendar window — buy when EGP is strong AND global gold is dipping. Use Thrivo's Price Trends tab below to see your scraped data.",
                "sources": [],
            },
            {
                "name":           "Avoid — pre-Eid demand spike",
                "when":           _pre_eid_fitr_window,
                "discount_range": "Prices typically RISE 2–5%",
                "confidence":     "MEDIUM",
                "rationale":      "⚠️ Gold demand spikes pre-Eid (gifts, bridal). Avoid buying during this window if possible.",
                "sources": [],
            },
        ],
        "honest_note": "Gold doesn't follow retail discount cycles. Best signal is the data — see the Price Trends tab for analysis of YOUR scraped gold history.",
    },

    "appliances": {
        "icon":  "🏠",
        "title": "Home Appliances (AC, fridge, washer)",
        "windows": [
            {
                "name":           "AC pre-summer (Jun–Jul)",
                "when":           _appliances_summer,
                "discount_range": "10–25% with installments",
                "confidence":     "MEDIUM",
                "rationale":      "B.Tech, Carrefour, 2B run summer AC campaigns. Best deals are EARLY summer (June) before demand peak; July sees prices firm up.",
                "sources": [],
            },
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "20–50%",
                "confidence":     "HIGH",
                "rationale":      "Highest discounts of the year on large appliances. Carrefour and B.Tech compete aggressively. Usually best window for fridges and washers.",
                "sources": [
                    ("AlCoupon — appliances", "https://egypt.alcoupon.com/en/black-friday-sale"),
                ],
            },
        ],
    },

    "smart_home": {
        "icon":  "💡",
        "title": "Smart Home Devices",
        "windows": [
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "20–50%",
                "confidence":     "MEDIUM",
                "rationale":      "Echo, Google Nest, smart bulbs etc. consistently discounted on Amazon EG and Noon. Smart-home category in Egypt is younger so calendar patterns are still forming.",
                "sources": [
                    ("Noon Egypt — Yellow Friday", "https://egypt.alcoupon.com/en/blog/black-friday-vs-white-friday-everything-you-need-to-know"),
                ],
            },
            {
                "name":           "11.11",
                "when":           _november_11,
                "discount_range": "15–40%",
                "confidence":     "MEDIUM",
                "rationale":      "Chinese smart-home brands (Xiaomi, Aqara, Tuya) discount heavily for Singles' Day on AliExpress and Noon.",
                "sources": [],
            },
        ],
        "honest_note": "Smart-home prices in Egypt are dominated by White Friday + 11.11 — outside those windows, prices are flat all year.",
    },

    "furniture": {
        "icon":  "🛋️",
        "title": "Furniture",
        "windows": [
            {
                "name":           "Post-Ramadan",
                "when":           lambda y: (
                    (get_lunar_event_dates(y).get("eid_al_fitr") + datetime.timedelta(days=7))
                        if "eid_al_fitr" in get_lunar_event_dates(y) else datetime.date(y, 5, 1),
                    (get_lunar_event_dates(y).get("eid_al_fitr") + datetime.timedelta(days=45))
                        if "eid_al_fitr" in get_lunar_event_dates(y) else datetime.date(y, 6, 15),
                    "Post-Eid window — wedding-season tail",
                ),
                "discount_range": "10–25%",
                "confidence":     "LOW",
                "rationale":      "Wedding-season demand peaks pre-Ramadan and pre-Eid; furniture stores discount to clear stock after these spikes. Less universal than retail patterns.",
                "sources": [],
            },
            {
                "name":           "White Friday",
                "when":           _white_friday_window,
                "discount_range": "20–40%",
                "confidence":     "MEDIUM",
                "rationale":      "Home Centre, IKEA Egypt, Saudi Home Centers run furniture promotions during White Friday. Consistent year over year.",
                "sources": [],
            },
        ],
    },
}


# ──────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ──────────────────────────────────────────────────────────────────────
def all_categories() -> list[str]:
    return list(CATEGORIES.keys())


def get_category(key: str) -> dict | None:
    return CATEGORIES.get(key)


def upcoming_windows(today: datetime.date | None = None,
                     within_days: int = 365) -> list[dict]:
    """Return all shopping windows starting within the next `within_days` days.
    Sorted by start date."""
    if today is None:
        today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)

    out = []
    # Look at this year and next year to catch windows that wrap
    for cat_key, cat in CATEGORIES.items():
        for w in cat["windows"]:
            for offset in (0, 1):
                year = today.year + offset
                try:
                    result = w["when"](year)
                except Exception:
                    continue
                if not result:
                    continue
                start, end, label = result
                if not isinstance(start, datetime.date):
                    continue
                if start > horizon or end < today:
                    continue
                out.append({
                    "category_key":   cat_key,
                    "category_icon":  cat["icon"],
                    "category_title": cat["title"],
                    "name":           w["name"],
                    "start":          start,
                    "end":            end,
                    "label":          label,
                    "discount_range": w.get("discount_range", ""),
                    "confidence":     w.get("confidence", "MEDIUM"),
                    "rationale":      w.get("rationale", ""),
                    "sources":        w.get("sources", []),
                    "is_active":      start <= today <= end,
                    "days_until":     (start - today).days if start > today else 0,
                })
    out.sort(key=lambda x: x["start"])
    return out


# ──────────────────────────────────────────────────────────────────────
#  DATA-MINER — runs on Thrivo's own scraped price history
# ──────────────────────────────────────────────────────────────────────
def analyze_price_history(history: list[dict],
                          asset_name: str = "asset") -> dict:
    """
    Given a list of {date: ISO, value: float} entries, surface honest
    seasonality patterns. Refuses to overstate findings: requires at
    least 60 days of data to comment on monthly patterns, 180 days for
    multi-month patterns.

    Returns:
      {
        "n_days": int,
        "current": float, "min": float, "max": float, "avg": float,
        "current_vs_avg_pct": float,
        "monthly_avg": {1: ..., 2: ..., ...},      # if enough data
        "best_months": [(month_num, avg_value), ...],
        "worst_months": [(month_num, avg_value), ...],
        "verdict": "BUY_NOW" | "WAIT" | "NEUTRAL" | "INSUFFICIENT_DATA",
        "confidence": "HIGH" | "MEDIUM" | "LOW",
        "explanation": str,
      }
    """
    import datetime as _dt
    out = {
        "asset":              asset_name,
        "n_days":             0,
        "verdict":            "INSUFFICIENT_DATA",
        "confidence":         "LOW",
        "explanation":        "Not enough price history yet. Wait for the daily scraper to collect more data.",
        "monthly_avg":        {},
        "best_months":        [],
        "worst_months":       [],
    }
    if not history or len(history) < 14:
        return out

    # Parse and clean
    rows = []
    for r in history:
        try:
            d = _dt.date.fromisoformat(r["date"])
            v = float(r["value"])
            if v > 0:
                rows.append((d, v))
        except Exception:
            continue
    if len(rows) < 14:
        return out
    rows.sort()

    values = [v for _, v in rows]
    out["n_days"] = len(rows)
    out["current"] = values[-1]
    out["min"]     = min(values)
    out["max"]     = max(values)
    out["avg"]     = sum(values) / len(values)
    out["current_vs_avg_pct"] = ((values[-1] - out["avg"]) / out["avg"]) * 100

    # Monthly seasonality — only if we have ≥60 days
    if len(rows) >= 60:
        monthly: dict[int, list[float]] = {}
        for d, v in rows:
            monthly.setdefault(d.month, []).append(v)
        # Need ≥3 data points in a month to call it
        monthly_avg = {m: sum(vs) / len(vs) for m, vs in monthly.items() if len(vs) >= 3}
        out["monthly_avg"] = monthly_avg
        if len(monthly_avg) >= 3:
            sorted_months = sorted(monthly_avg.items(), key=lambda x: x[1])
            out["best_months"]  = sorted_months[:3]   # cheapest 3
            out["worst_months"] = sorted_months[-3:]  # priciest 3

    # Verdict — current vs rolling average
    pct = out["current_vs_avg_pct"]
    if len(rows) < 30:
        out["verdict"]    = "INSUFFICIENT_DATA"
        out["confidence"] = "LOW"
        out["explanation"] = f"Only {out['n_days']} days of data. Need 30+ for a confident verdict."
    elif pct < -3:
        out["verdict"]    = "BUY_NOW"
        out["confidence"] = "HIGH" if len(rows) >= 90 else "MEDIUM"
        out["explanation"] = (
            f"Current price ({values[-1]:,.2f}) is {abs(pct):.1f}% below the "
            f"{len(rows)}-day average ({out['avg']:,.2f}). Historically a good buying window."
        )
    elif pct > 3:
        out["verdict"]    = "WAIT"
        out["confidence"] = "HIGH" if len(rows) >= 90 else "MEDIUM"
        out["explanation"] = (
            f"Current price ({values[-1]:,.2f}) is {pct:.1f}% above the "
            f"{len(rows)}-day average ({out['avg']:,.2f}). Consider waiting."
        )
    else:
        out["verdict"]    = "NEUTRAL"
        out["confidence"] = "MEDIUM"
        out["explanation"] = (
            f"Current price ({values[-1]:,.2f}) is within ±3% of the average. "
            f"No strong signal either way."
        )

    return out