"""
Read-only diagnostic for the model PDF vs Kalshi bucket coverage.

For one city + date, pulls:
  1. The GFS ensemble forecast (member highs)
  2. Every open Kalshi bucket market in that series

Then prints, for every bucket the bot would scan:
  - the raw model probability BEFORE the 0.05 clipping floor
  - the clipped value (what edge math sees today)
  - the market's implied probability
  - the implied edge under both raw and clipped model

Final summary: sum of raw bucket probabilities across the mutually-
exclusive coverage. If sum ≈ 1.0 → model is internally consistent and the
NO-side edges showing in the signals table are real. If sum < 1.0 → mass
is missing (some buckets the bot isn't scanning are absorbing probability
the model would assign there). If sum > 1.0 → buckets overlap or the
clipping floor is double-counted.

Run from project root:
    .venv/bin/python scripts/inspect_kalshi_model_pdf_2026-05-20.py
    .venv/bin/python scripts/inspect_kalshi_model_pdf_2026-05-20.py --city nyc
    .venv/bin/python scripts/inspect_kalshi_model_pdf_2026-05-20.py --city denver --date 2026-05-21
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date as _date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present  # noqa: E402
from backend.data.kalshi_markets import CITY_SERIES, CITY_NAMES  # noqa: E402
from backend.data.weather import fetch_ensemble_forecast  # noqa: E402

# Match the bot's clip range (see weather_signals.py:74).
CLIP_LO = 0.05
CLIP_HI = 0.95


def _pd(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date_suffix(d: _date) -> str:
    MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{d.year % 100:02d}{MON[d.month-1]}{d.day:02d}"


def _raw_bucket_prob(members, strike_type, floor, cap):
    """Compute the model probability under Kalshi's TRUNCATION rounding rule
    (matches the live bot's weather_signals.py patch landed 2026-05-20):
      between(floor, cap) -> [floor, cap+1)
      greater(floor)      -> h >= floor + 1
      less(cap)           -> h < cap
    """
    if not members:
        return None
    if strike_type == "between" and floor is not None and cap is not None:
        return sum(1 for h in members if floor <= h < cap + 1.0) / len(members)
    if strike_type == "greater" and floor is not None:
        return sum(1 for h in members if h > floor + 1.0) / len(members)
    if strike_type == "less" and cap is not None:
        return sum(1 for h in members if h < cap) / len(members)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="nyc",
                        help="City key: nyc, chicago, miami, los_angeles, denver")
    parser.add_argument("--date", default=None,
                        help="Resolution date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.city not in CITY_SERIES:
        print(f"ERROR: unknown city {args.city!r}. Options: {list(CITY_SERIES)}")
        return 1
    series = CITY_SERIES[args.city]
    city_name = CITY_NAMES.get(args.city, args.city)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = _date.today()

    date_prefix = f"{series}-{_date_suffix(target)}-"
    print(f"City: {city_name} ({args.city})  Target date: {target.isoformat()}  Series: {series}")
    print(f"Filter prefix: {date_prefix}*\n")

    # 1) Forecast
    forecast = await fetch_ensemble_forecast(args.city, target)
    if not forecast or not forecast.member_highs:
        print("ERROR: no forecast / no ensemble members. Bailing.")
        return 2
    members = list(forecast.member_highs)
    members_sorted = sorted(members)
    print(f"Ensemble: N={len(members)} members")
    print(f"  members (sorted): {[round(h,1) for h in members_sorted]}")
    print(f"  mean: {sum(members)/len(members):.2f}°F  "
          f"min: {min(members):.1f}°F  max: {max(members):.1f}°F")

    # 2) Markets
    if not kalshi_credentials_present():
        print("\nERROR: Kalshi creds missing.")
        return 3
    client = KalshiClient()

    raw_markets = []
    cursor = None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await client.get_markets(params)
        raw_markets.extend(data.get("markets", []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break

    rows = []
    for m in raw_markets:
        ticker = m.get("ticker", "")
        if not ticker.startswith(date_prefix):
            continue
        strike_type = (m.get("strike_type") or "").lower() or None
        floor = _pd(m.get("floor_strike"))
        cap = _pd(m.get("cap_strike"))
        yes_ask = _pd(m.get("yes_ask_dollars"))
        no_ask = _pd(m.get("no_ask_dollars"))
        if not yes_ask or not no_ask:
            continue
        implied_yes = max(0.01, min(0.99, (yes_ask + (1 - no_ask)) / 2.0))
        raw_p = _raw_bucket_prob(members, strike_type, floor, cap)
        if raw_p is None:
            continue
        clipped_p = max(CLIP_LO, min(CLIP_HI, raw_p))
        rows.append({
            "ticker": ticker, "strike_type": strike_type,
            "floor": floor, "cap": cap,
            "yes_ask": yes_ask, "no_ask": no_ask, "implied": implied_yes,
            "raw_p": raw_p, "clipped_p": clipped_p,
        })

    if not rows:
        print("\nNo open buckets matched the filter.")
        return 4

    # Order: 'less' first, then 'between' by floor, then 'greater'
    def _key(r):
        t = r["strike_type"]
        if t == "less":
            return (0, r["cap"] or 0)
        if t == "between":
            return (1, r["floor"] or 0)
        if t == "greater":
            return (2, r["floor"] or 0)
        return (3, 0)
    rows.sort(key=_key)

    print(f"\nFound {len(rows)} open markets in series.\n")

    # Tabular dump
    print("=" * 110)
    print("PER-BUCKET MODEL PROBABILITY")
    print("=" * 110)
    print(f"{'ticker':<26} {'type':>8} {'range':<14} {'raw_p':>7} {'clip_p':>7} "
          f"{'mkt_imp':>8} {'yes_edge_raw':>13} {'no_edge_raw':>12}")
    sum_raw_between = 0.0
    sum_raw_greater = 0.0
    sum_raw_less = 0.0
    n_between = 0
    for r in rows:
        if r["strike_type"] == "between":
            label = f"[{r['floor']:.0f}, {r['cap']:.0f})"
            sum_raw_between += r["raw_p"]
            n_between += 1
        elif r["strike_type"] == "greater":
            label = f"> {r['floor']:.1f}"
            sum_raw_greater += r["raw_p"]
        elif r["strike_type"] == "less":
            label = f"< {r['cap']:.1f}"
            sum_raw_less += r["raw_p"]
        else:
            label = "?"
        # raw edges before clipping
        yes_edge_raw = r["raw_p"] - r["implied"]
        no_edge_raw = r["implied"] - r["raw_p"]
        print(f"{r['ticker']:<26} {r['strike_type']:>8} {label:<14} "
              f"{r['raw_p']:>7.3f} {r['clipped_p']:>7.3f} {r['implied']:>8.3f} "
              f"{yes_edge_raw:>+13.3f} {no_edge_raw:>+12.3f}")

    print()
    print("=" * 110)
    print("COVERAGE SUMMARY")
    print("=" * 110)
    print(f"  Narrow 'between' buckets:        {n_between}")
    print(f"  Sum of raw P(between):           {sum_raw_between:.4f}")
    print(f"  Raw P(greater) max (top tail):   {sum_raw_greater:.4f}  (single market or max)")
    print(f"  Raw P(less)    max (low tail):   {sum_raw_less:.4f}  (single market or max)")

    # Best-effort coverage check: if buckets are mutually exclusive and
    # cover [low_tail_cap, top_tail_floor), then between-sum + bottom + top
    # should approach 1.0.
    between_rows = [r for r in rows if r["strike_type"] == "between"]
    if between_rows:
        b_min = min(r["floor"] for r in between_rows)
        b_max = max(r["cap"] for r in between_rows)
        below_tail = sum(1 for h in members if h < b_min) / len(members)
        above_tail = sum(1 for h in members if h >= b_max) / len(members)
        coverage = below_tail + sum_raw_between + above_tail
        print()
        print(f"  Bucket range covered by 'between' markets: "
              f"[{b_min:.1f}, {b_max:.1f})°F")
        print(f"  Ensemble mass BELOW bucket range:  {below_tail:.4f}")
        print(f"  Ensemble mass ABOVE bucket range:  {above_tail:.4f}")
        print(f"  Mutually-exclusive total:          "
              f"{coverage:.4f}  (should be ≈ 1.0)")
        if 0.95 <= coverage <= 1.05:
            print()
            print("  >>> COVERAGE IS COMPLETE. The model's bucket distribution sums to ~1.0")
            print("      across the visible market range. NO-side edges shown in the signals")
            print("      table are mathematically real — they come from the market spreading")
            print("      probability across buckets that the model thinks are near-empty.")
        else:
            print()
            print(f"  >>> COVERAGE GAP. Total = {coverage:.4f}, expected ≈ 1.0.")
            print("      Something is absorbing or duplicating probability mass.")
            print("      Investigate before relying on these edges for sizing.")

    print()
    print("=" * 110)
    print("CLIP-FLOOR IMPACT")
    print("=" * 110)
    clipped_rows = [r for r in rows if r["raw_p"] < CLIP_LO]
    print(f"  Buckets where raw_p < {CLIP_LO} (clipped UP to {CLIP_LO}):  {len(clipped_rows)} of {len(rows)}")
    if clipped_rows:
        # For NO trades on these buckets, the clipping floor INCREASES the
        # model's stated probability of YES, which DECREASES the apparent
        # NO-side edge. So the floor is actually conservative for NO bets
        # on empty buckets. But on YES side the same buckets would be
        # over-weighted. Show both interpretations.
        print(f"  On YES trades for these buckets, clipping inflates model_p by "
              f"~{CLIP_LO:.2f} - 0.0 = +{CLIP_LO:.2f}.")
        print(f"  On NO trades for these buckets, clipping DEFLATES the NO-side")
        print(f"  edge by ~{CLIP_LO:.2f}. So the observed NO edges are LOWER than")
        print(f"  what the unclipped model would suggest.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
