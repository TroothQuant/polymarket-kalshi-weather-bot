"""
Diagnostic for the Kalshi bucket-semantics question (2026-05-20).

Pulls every open market in a Kalshi KXHIGH series (default KXHIGHNY for
today's date), prints each market's threshold + question text + ask
quotes, sorts by threshold, and sums yes_ask_dollars across the series.

The sum tells us which world we're in:

  sum ≈ 1.00  → NARROW BUCKETS. Each market is "high lands in a specific
                0.5°F range". The bot's model_probability calc (currently
                P(high > threshold) ) is the WRONG question — needs to be
                P(high in this bucket). 88-94% edges across many tickers
                are fictional.

  sum >> 1    → CUMULATIVE. Each market is "high above (or below) X".
                Parser's existing B=above / T=below convention is fine.
                88-94% edges are real Kalshi mispricings worth taking.

Run from project root:
    .venv/bin/python scripts/inspect_kalshi_bucket_semantics_2026-05-20.py
    .venv/bin/python scripts/inspect_kalshi_bucket_semantics_2026-05-20.py --series KXHIGHLAX --date 26MAY20

Read-only. No DB writes, no trades.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present  # noqa: E402


def _parse_threshold(ticker: str):
    """Return (boundary_type, threshold_f) or (None, None) if unparseable."""
    m = re.match(r"^[A-Z]+-\d{2}[A-Z]{3}\d{2}-([BT])([\d.]+)$", ticker)
    if not m:
        return None, None
    return m.group(1), float(m.group(2))


def _pd(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", default="KXHIGHNY",
                        help="Kalshi series ticker (default: KXHIGHNY)")
    parser.add_argument("--date", default=None,
                        help="Date suffix to filter on, e.g. 26MAY20 (default: today)")
    args = parser.parse_args()

    if args.date is None:
        today = date.today()
        MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
        args.date = f"{today.year % 100:02d}{MON[today.month-1]}{today.day:02d}"

    date_prefix = f"{args.series}-{args.date}-"
    print(f"Inspecting {date_prefix}* ...")

    if not kalshi_credentials_present():
        print("ERROR: Kalshi credentials not configured.")
        return 1

    client = KalshiClient()

    # Pull every open market in the series, paginating just in case.
    markets = []
    cursor = None
    while True:
        params = {"series_ticker": args.series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await client.get_markets(params)
        markets.extend(data.get("markets", []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break

    # Filter to today's series + parse threshold so we can sort
    rows = []
    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker.startswith(date_prefix):
            continue
        btype, thresh = _parse_threshold(ticker)
        rows.append({
            "ticker": ticker,
            "btype": btype,
            "thresh": thresh,
            "title": m.get("title", ""),
            "subtitle": m.get("subtitle", ""),
            "rules_primary": (m.get("rules_primary") or "")[:140],
            "yes_ask": _pd(m.get("yes_ask_dollars")),
            "no_ask": _pd(m.get("no_ask_dollars")),
            "yes_bid": _pd(m.get("yes_bid_dollars")),
            "no_bid": _pd(m.get("no_bid_dollars")),
            "last": _pd(m.get("last_price_dollars")),
            "volume": _pd(m.get("volume_fp")) or 0,
        })

    if not rows:
        print(f"No open markets matching {date_prefix}*")
        return 2

    rows.sort(key=lambda r: (r["btype"] or "Z", r["thresh"] or 0))

    print(f"\nFound {len(rows)} open markets in {date_prefix}\n")

    # Show a sample of the prose first — the title / subtitle / rules_primary
    # almost always answers the bucket question on its own.
    print("=" * 80)
    print("MARKET PROSE — first 3 markets shown verbatim")
    print("=" * 80)
    for r in rows[:3]:
        print(f"\n  ticker:       {r['ticker']}")
        print(f"  title:        {r['title']}")
        print(f"  subtitle:     {r['subtitle']}")
        print(f"  rules_primary: {r['rules_primary']}")

    # Tabular dump
    print("\n" + "=" * 80)
    print("FULL SERIES — sorted by (boundary_type, threshold)")
    print("=" * 80)
    print(f"{'ticker':<32} {'yes_ask':>9} {'no_ask':>9} {'yes+no':>9} {'last':>7} {'vol':>8}")
    sum_yes_ask = 0.0
    sum_yes_no = 0.0
    n_valid = 0
    for r in rows:
        ya = r["yes_ask"] or 0
        na = r["no_ask"] or 0
        ttl = (ya + na) if (ya and na) else 0
        last = r["last"]
        last_s = f"{last:.4f}" if last is not None else "    -"
        print(f"{r['ticker']:<32} {ya:>9.4f} {na:>9.4f} {ttl:>9.4f} {last_s:>7} {r['volume']:>8.0f}")
        if ya:
            sum_yes_ask += ya
            n_valid += 1
        if ttl:
            sum_yes_no += ttl

    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    print(f"  Markets with a yes_ask:       {n_valid}")
    print(f"  Sum of yes_ask_dollars:       {sum_yes_ask:.4f}")
    print(f"  Sum of (yes_ask + no_ask):    {sum_yes_no:.4f}")
    print()

    # Decision tree
    if 0.85 <= sum_yes_ask <= 1.30:
        print("  >>> NARROW BUCKETS (sum_yes_ask ≈ 1).")
        print("      Each market is a SINGLE bucket of the high distribution.")
        print("      The bot's model_probability calc is the WRONG question.")
        print("      Do NOT re-enable Kalshi trading until the model is")
        print("      rewritten to compute P(high in this bucket).")
        print("      The 88-94% edges currently shown are fictional.")
    elif sum_yes_ask > 1.5:
        print("  >>> AMBIGUOUS — sum suggests cumulative-ish but not clean.")
        print("      Inspect the MARKET PROSE block above to disambiguate;")
        print("      the rules_primary text will state the actual question.")
    else:
        print("  >>> CUMULATIVE thresholds (sum well below or above 1).")
        print("      Parser's existing B=above / T=below convention may be fine.")
        print("      Confirm via rules_primary text above before re-enabling.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
