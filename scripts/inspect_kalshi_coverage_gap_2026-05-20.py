"""
Read-only diagnostic for the Kalshi bucket COVERAGE GAP.

Earlier diagnostic (inspect_kalshi_model_pdf_2026-05-20.py) showed the
bot's model probabilities sum to ~0.65 across the visible bucket markets
when they should sum to ~1.0. Three possible causes:

  (a) status filter — closed / settled / determined buckets exist for the
      same date but the bot's loader uses status=open and misses them.
  (b) bucket width — bot's parser reads each market as 1°F wide
      ([floor, cap)) but Kalshi's grid is actually 2°F wide. The
      "missing" buckets would not exist because each market spans more
      degrees than we thought.
  (c) genuine grid sparsity — Kalshi lists only some buckets and the
      others simply don't exist, with the ensemble mass in those gaps
      effectively unbettable.

This script asks Kalshi for EVERY market in today's series across all
statuses, prints explicit floor/cap per market with bucket width, and
computes coverage three ways:
  1. sum of (yes_ask + (1 - no_ask)) / 2  (market's implied PMF)
  2. sum of model-PDF probabilities (count_in_bucket / N)
  3. degree-span coverage (do floor/cap values tile the relevant
     temperature range without gaps?)

Run from project root:
    .venv/bin/python scripts/inspect_kalshi_coverage_gap_2026-05-20.py
    .venv/bin/python scripts/inspect_kalshi_coverage_gap_2026-05-20.py --city nyc
    .venv/bin/python scripts/inspect_kalshi_coverage_gap_2026-05-20.py --city denver
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date as _date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present  # noqa: E402
from backend.data.kalshi_markets import CITY_SERIES, CITY_NAMES  # noqa: E402
from backend.data.weather import fetch_ensemble_forecast  # noqa: E402


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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="nyc")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    if args.city not in CITY_SERIES:
        print(f"ERROR: unknown city {args.city!r}")
        return 1
    series = CITY_SERIES[args.city]
    city_name = CITY_NAMES.get(args.city, args.city)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = _date.today()

    date_prefix = f"{series}-{_date_suffix(target)}-"
    print(f"City: {city_name}  Target: {target}  Series prefix: {date_prefix}*\n")

    if not kalshi_credentials_present():
        print("ERROR: Kalshi creds missing.")
        return 2
    client = KalshiClient()

    # Pull WITHOUT a status filter so we see every state (open, closed,
    # determined, finalized, settled, etc.).
    all_markets = []
    cursor = None
    while True:
        params = {"series_ticker": series, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await client.get_markets(params)
        all_markets.extend(data.get("markets", []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break

    # Filter to today's date
    today_markets = [m for m in all_markets if (m.get("ticker") or "").startswith(date_prefix)]
    if not today_markets:
        print(f"No markets for {date_prefix}*")
        return 3
    print(f"Total markets for today's date (all statuses): {len(today_markets)}\n")

    # Group by status
    by_status = {}
    for m in today_markets:
        s = (m.get("status") or "?").lower()
        by_status.setdefault(s, []).append(m)
    print("Status breakdown:")
    for s, ms in sorted(by_status.items()):
        print(f"  {s:>14}: {len(ms)}")
    print()

    # Forecast for model-PDF comparison
    forecast = await fetch_ensemble_forecast(args.city, target)
    members = list(forecast.member_highs) if forecast and forecast.member_highs else []
    if members:
        print(f"Ensemble: N={len(members)} mean={sum(members)/len(members):.2f}°F "
              f"min={min(members):.1f}°F max={max(members):.1f}°F\n")

    # Tabular: every market with floor/cap visible
    print("=" * 116)
    print(f"{'ticker':<28} {'status':>11} {'strike':>8} {'floor':>7} {'cap':>7} "
          f"{'width':>6} {'yes_ask':>8} {'no_ask':>8} {'impl':>6} {'raw_p':>7}")
    print("=" * 116)

    def _key(m):
        ft = (m.get("strike_type") or "").lower()
        ft_rank = {"less": 0, "between": 1, "greater": 2}.get(ft, 3)
        return (ft_rank, _pd(m.get("floor_strike")) or _pd(m.get("cap_strike")) or 0)

    today_markets.sort(key=_key)
    sum_impl = 0.0
    sum_model = 0.0
    n_with_quote = 0

    for m in today_markets:
        ticker = m.get("ticker", "")
        status = (m.get("status") or "?").lower()
        st = (m.get("strike_type") or "").lower()
        fl = _pd(m.get("floor_strike"))
        cp = _pd(m.get("cap_strike"))
        width = ""
        if fl is not None and cp is not None:
            width = f"{cp - fl:.1f}"
        elif fl is not None:
            width = "f-only"
        elif cp is not None:
            width = "c-only"
        ya = _pd(m.get("yes_ask_dollars"))
        na = _pd(m.get("no_ask_dollars"))
        ya_s = f"{ya:.4f}" if ya is not None else "   -"
        na_s = f"{na:.4f}" if na is not None else "   -"
        impl = None
        if ya is not None and na is not None and ya > 0 and na > 0:
            impl = max(0.01, min(0.99, (ya + (1 - na)) / 2.0))
            sum_impl += impl
            n_with_quote += 1
        impl_s = f"{impl:.3f}" if impl is not None else "  -"

        raw_p = None
        if members:
            # Truncation-rounding rule (matches bot weather_signals.py):
            # between -> [floor, cap+1), greater -> h >= floor+1.
            if st == "between" and fl is not None and cp is not None:
                raw_p = sum(1 for h in members if fl <= h < cp + 1.0) / len(members)
            elif st == "greater" and fl is not None:
                raw_p = sum(1 for h in members if h > fl + 1.0) / len(members)
            elif st == "less" and cp is not None:
                raw_p = sum(1 for h in members if h < cp) / len(members)
        if raw_p is not None:
            sum_model += raw_p
        raw_s = f"{raw_p:.3f}" if raw_p is not None else "  -"

        fl_s = f"{fl:.1f}" if fl is not None else "  -"
        cp_s = f"{cp:.1f}" if cp is not None else "  -"
        print(f"{ticker:<28} {status:>11} {st:>8} {fl_s:>7} {cp_s:>7} "
              f"{width:>6} {ya_s:>8} {na_s:>8} {impl_s:>6} {raw_s:>7}")

    print()
    print("=" * 116)
    print("COVERAGE SUMMARY")
    print("=" * 116)
    print(f"  Markets with a quote:              {n_with_quote}")
    print(f"  Sum of implied prob (yes_ask/no_ask midpoint):  {sum_impl:.4f}")
    print(f"  Sum of model probabilities (count_in_bucket/N): {sum_model:.4f}")
    print()
    if abs(sum_impl - 1.0) < 0.10 and abs(sum_model - 1.0) > 0.20:
        print("  >>> Market sums to ~1.0 but model sums to ~0.65.")
        print("      This means the market list DOES cover the full distribution")
        print("      but the bot's bucket-width parse is missing degrees the")
        print("      market is actually covering. Likely fix: re-check that we")
        print("      read floor_strike / cap_strike off the raw market payload,")
        print("      not from a derived field.")
    if abs(sum_impl - 1.0) > 0.30:
        print("  >>> Market itself doesn't sum to 1.0 either. The 'between'")
        print("      markets we're seeing genuinely don't cover the full")
        print("      distribution -- there are gaps the platform won't fill.")
        print("      Fix: cap exposure to markets the model has high mass in,")
        print("      or skip trades on buckets where neighbouring degree ranges")
        print("      don't have a corresponding market.")

    # Degree-span coverage check — only meaningful if there are between markets
    btw = [m for m in today_markets if (m.get("strike_type") or "").lower() == "between"]
    if btw:
        btw.sort(key=lambda m: _pd(m.get("floor_strike")) or 0)
        print()
        print("Bucket grid (between markets in floor-order):")
        prev_cap = None
        for m in btw:
            fl = _pd(m.get("floor_strike"))
            cp = _pd(m.get("cap_strike"))
            gap_note = ""
            if prev_cap is not None and fl is not None and abs(prev_cap - fl) > 0.01:
                gap_note = f"  <-- GAP of {fl - prev_cap:.1f}°F since previous market"
            print(f"  [{fl:.1f}, {cp:.1f})  width {cp-fl:.1f}°F  {m.get('ticker')}{gap_note}")
            prev_cap = cp
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
