#!/usr/bin/env python3
"""NYC + Denver settlement-station coordinate validation (READ-ONLY).

Same method as the 2026-05-29 LA analysis (weather_source_mismatch_analysis):
for every SETTLED NYC/Denver Polymarket weather market, pull the ERA5 reanalyzed
daily-max at (a) the bot's CURRENT coords and (b) the candidate SETTLEMENT-station
coords, compare each to the market threshold, and score against Polymarket's
actual settlement_value. Reports per-city match-rate at each coord pair + the
markets where the two coords disagree on the side.

Threshold/direction come from signals.reasoning ('high above 76F on ...'),
exactly as backtest_weather_harness does. Read-only: opens the bot DB and only
hits the free open-meteo ERA5 archive. Never writes bot state.
"""
import datetime as dt
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

DB = "/home/trooth/Projects/trooth-weather-bot/tradingbot.db"

# coord pairs: city -> (current_lat, current_lon, cand_label, cand_lat, cand_lon)
COORDS = {
    "nyc":    (40.7128, -74.0060, "KLGA", 40.7794, -73.8803),
    "denver": (39.7392, -104.9903, "KBKF", 39.7017, -104.7517),
}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

_era5_cache = {}


def fetch_era5_max(lat, lon, target_date):
    key = (round(lat, 4), round(lon, 4), target_date.isoformat())
    if key in _era5_cache:
        return _era5_cache[key]
    base = "https://archive-api.open-meteo.com/v1/archive"
    params = {"latitude": lat, "longitude": lon,
              "start_date": target_date.isoformat(), "end_date": target_date.isoformat(),
              "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
              "timezone": "auto"}
    url = f"{base}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            j = json.loads(r.read())
        val = float(j["daily"]["temperature_2m_max"][0])
    except Exception as e:
        print(f"  ERA5 fetch failed {key}: {e}", file=sys.stderr)
        val = None
    _era5_cache[key] = val
    return val


def city_from_slug(slug):
    if not slug:
        return None
    s = slug.lower()
    if "in-nyc-on" in s or "new-york" in s:
        return "nyc"
    if "in-denver-on" in s or "-denver-" in s:
        return "denver"
    return None


def date_from_slug(slug):
    # 'highest-temperature-in-nyc-on-may-19-2026'
    m = re.search(r"on-([a-z]+)-(\d{1,2})-(\d{4})", (slug or "").lower())
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    try:
        return dt.date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


def strike_and_direction(reasoning):
    if not reasoning:
        return None, None
    m = re.search(r"high\s+(above|below)\s+(\d+)F", reasoning)
    if not m:
        return None, None
    return int(m.group(2)), m.group(1)


def predicted_yes(reading, threshold, direction):
    """YES if the reading lands on the YES side of the threshold.
    above: high >= threshold ; below: high <= threshold (integer strikes,
    .1F ERA5 readings rarely hit the boundary exactly)."""
    if reading is None:
        return None
    if direction == "above":
        return 1.0 if reading >= threshold else 0.0
    if direction == "below":
        return 1.0 if reading <= threshold else 0.0
    return None


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("""
        SELECT t.market_ticker, t.event_slug, t.direction, t.settlement_value,
               s.reasoning AS reasoning
        FROM trades t
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE t.market_type='weather' AND t.platform='polymarket'
          AND t.settled=1 AND t.settlement_value IS NOT NULL
          AND (LOWER(t.event_slug) LIKE '%nyc%' OR LOWER(t.event_slug) LIKE '%denver%')
        ORDER BY t.id
    """).fetchall()
    con.close()

    # Dedupe to distinct markets (market_ticker = one binary market = one threshold
    # + one settlement). Multiple trades on the same ticker are the same comparison.
    markets = {}
    skipped = []
    for ticker, slug, direction, settle, reasoning in rows:
        city = city_from_slug(slug)
        if city not in COORDS:
            continue
        thr, dirn = strike_and_direction(reasoning)
        tdate = date_from_slug(slug)
        if thr is None or tdate is None:
            skipped.append((ticker, slug, "no threshold/date in reasoning/slug"))
            continue
        markets.setdefault(ticker, {
            "city": city, "date": tdate, "threshold": thr, "direction": dirn,
            "settled_yes": float(settle), "slug": slug,
        })

    # Score each distinct market at both coord pairs.
    per_city = {c: {"n": 0, "cur_hits": 0, "cand_hits": 0, "disagree": []} for c in COORDS}
    for ticker, mk in markets.items():
        city = mk["city"]
        cur_lat, cur_lon, lbl, cand_lat, cand_lon = COORDS[city]
        cur_read = fetch_era5_max(cur_lat, cur_lon, mk["date"])
        cand_read = fetch_era5_max(cand_lat, cand_lon, mk["date"])
        cur_pred = predicted_yes(cur_read, mk["threshold"], mk["direction"])
        cand_pred = predicted_yes(cand_read, mk["threshold"], mk["direction"])
        if cur_read is None or cand_read is None:
            skipped.append((ticker, mk["slug"], "ERA5 fetch failed"))
            continue
        d = per_city[city]
        d["n"] += 1
        settled = mk["settled_yes"]
        if cur_pred == settled:
            d["cur_hits"] += 1
        if cand_pred == settled:
            d["cand_hits"] += 1
        if cur_pred != cand_pred:
            d["disagree"].append({
                "date": mk["date"].isoformat(), "dir": mk["direction"],
                "thr": mk["threshold"], "cur": cur_read, "cand": cand_read,
                "cand_lbl": lbl,
                "settled": "YES" if settled == 1.0 else "NO",
                "cur_pred": "YES" if cur_pred == 1.0 else "NO",
                "cand_pred": "YES" if cand_pred == 1.0 else "NO",
                "right": ("current" if cur_pred == settled else
                          ("candidate" if cand_pred == settled else "neither")),
            })

    # ---- report ----
    print("=" * 70)
    print("NYC + DENVER SETTLEMENT-STATION COORDINATE VALIDATION (ERA5 replay)")
    print("=" * 70)
    print(f"  distinct settled markets scored: {sum(d['n'] for d in per_city.values())}"
          f"  (from {len(rows)} settled Polymarket NYC/Denver trade rows)")
    if skipped:
        print(f"  skipped: {len(skipped)} (no parseable threshold/date or ERA5 miss)")
    print()
    print(f"  {'city':<8}{'n':>4}{'current coords':>22}{'candidate (station)':>24}")
    for city, d in per_city.items():
        cur_lat, cur_lon, lbl, cand_lat, cand_lon = COORDS[city]
        n = d["n"]
        cur = f"{d['cur_hits']}/{n} = {100*d['cur_hits']/n:.0f}%" if n else "n/a"
        cand = f"{d['cand_hits']}/{n} = {100*d['cand_hits']/n:.0f}%" if n else "n/a"
        print(f"  {city.upper():<8}{n:>4}{cur:>22}{(lbl + ' ' + cand):>24}")

    print()
    print("DISAGREEMENTS (current vs candidate predict different sides):")
    any_dis = False
    for city, d in per_city.items():
        for x in d["disagree"]:
            any_dis = True
            print(f"  {city.upper():<7} {x['date']}  {x['dir']} {x['thr']}F | "
                  f"cur={x['cur']:.1f}->{x['cur_pred']}  {x['cand_lbl']}={x['cand']:.1f}->{x['cand_pred']}  "
                  f"| PM={x['settled']}  -> {x['right']} correct")
    if not any_dis:
        print("  (none — the two coord sets never crossed the threshold differently)")


if __name__ == "__main__":
    main()
