"""
End-of-day calibration check for today's Kalshi weather signals.

For each city in the bot's universe, pulls every resolved KXHIGH market
for the target date, identifies which bucket actually paid YES, and
reports for that bucket:

  - what the model said its probability was at scan time
  - what the market priced it at
  - what bucket the bot would have traded
  - whether the model was right (highest-mass bucket = winning bucket)
    or the market was right

Use this AFTER markets resolve tonight (≈04:59 UTC) to decide whether
the model's pre-resolution concentration of probability is calibrated.
If the model consistently picks the winning bucket across NYC, Denver,
Miami, LA → model is well-calibrated, KALSHI_TRADING_ENABLED can flip
to true. If the model picks losing buckets ≥ half the time → model is
stale or biased and trading should stay gated until that's fixed.

Run from project root (after end-of-day resolutions):
    .venv/bin/python scripts/kalshi_eod_calibration_2026-05-20.py
    .venv/bin/python scripts/kalshi_eod_calibration_2026-05-20.py --date 2026-05-20

Read-only. No DB writes, no trades.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import date as _date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present  # noqa: E402
from backend.data.kalshi_markets import CITY_SERIES, CITY_NAMES  # noqa: E402


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


async def _city_calibration(client, db_con, city_key: str, target: _date):
    series = CITY_SERIES[city_key]
    name = CITY_NAMES.get(city_key, city_key)
    date_prefix = f"{series}-{_date_suffix(target)}-"

    # Pull all markets in series for the target date
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
    today_markets = [m for m in all_markets if (m.get("ticker") or "").startswith(date_prefix)]

    # Find the winner: status finalized AND result == "yes"
    winner = None
    for m in today_markets:
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        if status in ("finalized", "determined", "settled") and result in ("yes", "yes_win"):
            winner = m
            break

    print()
    print("=" * 80)
    print(f"{name}  ({city_key})  date={target}  series={series}")
    print("=" * 80)
    if winner is None:
        unresolved = [m for m in today_markets if (m.get("status") or "").lower() not in ("finalized","determined","settled")]
        print(f"  No resolution yet. {len(unresolved)} of {len(today_markets)} markets still pending.")
        return None
    print(f"  Winner: {winner.get('ticker')}")
    print(f"  Title:  {winner.get('title')}")
    print(f"  Range:  floor={winner.get('floor_strike')}  cap={winner.get('cap_strike')}  "
          f"type={winner.get('strike_type')}")

    # Pull the bot's signals on this winner from the local DB to see what
    # the model said pre-resolution.
    winner_ticker = winner.get("ticker") or ""
    rows = db_con.execute("""
        SELECT id, direction, model_probability, market_price, edge,
               datetime(timestamp) AS ts
          FROM signals
         WHERE market_ticker = ?
      ORDER BY id DESC
         LIMIT 5
    """, (winner_ticker,)).fetchall()
    if rows:
        print(f"  Bot's model on winner (last {len(rows)} signals):")
        for r in rows:
            sid, direction, mp, mkt, edge, ts = r
            print(f"    sig#{sid} dir={direction:>3} model_p={mp:.3f} mkt_p={mkt:.3f} "
                  f"edge={edge:+.3f}  {ts}")
        last_model = rows[0][2]
    else:
        print("  Bot did not log any signal for the winning ticker.")
        last_model = None

    # Find the bucket the model concentrated the most mass on (per the
    # latest signals across the whole series for this date)
    series_signals = db_con.execute("""
        SELECT market_ticker, MAX(model_probability)
          FROM signals
         WHERE market_ticker LIKE ?
         GROUP BY market_ticker
      ORDER BY MAX(model_probability) DESC
         LIMIT 3
    """, (date_prefix + "%",)).fetchall()
    if series_signals:
        print(f"  Top-3 buckets by model probability today:")
        for tk, mp in series_signals:
            mark = "  <-- WINNER" if tk == winner_ticker else ""
            print(f"    {tk:<28} model_p_max={mp:.3f}{mark}")

    # Verdict for this city
    if last_model is not None and series_signals:
        top_bucket = series_signals[0][0]
        verdict = "MODEL RIGHT" if top_bucket == winner_ticker else "MODEL MISSED"
        print(f"  Verdict: {verdict}")
        return verdict
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None,
                        help="Resolution date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else _date.today()

    if not kalshi_credentials_present():
        print("ERROR: Kalshi creds missing.")
        return 1
    client = KalshiClient()
    db_path = ROOT / "tradingbot.db"
    db_con = sqlite3.connect(db_path)

    results = []
    for city_key in CITY_SERIES.keys():
        v = await _city_calibration(client, db_con, city_key, target)
        if v is not None:
            results.append((city_key, v))

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    if not results:
        print("  No resolved cities yet. Re-run after end-of-day.")
        return 0
    right = sum(1 for _, v in results if v == "MODEL RIGHT")
    total = len(results)
    print(f"  Model picked the winning bucket: {right}/{total} cities")
    for c, v in results:
        print(f"    {c:>14}: {v}")
    print()
    if right == total:
        print("  >>> Model is calibrated across all resolved cities today.")
        print("      Reasonable next step: flip KALSHI_TRADING_ENABLED=true.")
    elif right * 2 >= total:
        print("  >>> Mixed result. More data needed before re-enabling Kalshi trading.")
    else:
        print("  >>> Model is losing the calibration test. Investigate before")
        print("      re-enabling -- likely stale GFS forecasts vs market nowcast.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
