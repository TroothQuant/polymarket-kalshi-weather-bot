"""
Preview the proposed −50% stop-loss against the open weather positions, using
each market's CURRENT mid mark from Polymarket Gamma. Read-only.

What it reports for each open weather trade:
  • Entry price, size, max possible loss
  • Current YES / NO marks (from Gamma)
  • Mark-to-market unrealized P&L
  • Stop-loss trigger price for this position
  • Whether the stop would fire right now (at current mark)

This is the closest thing to "would the stop-loss have spared us?" we can build
without intraday price history. To answer the historical version, we'd need to
poll Gamma on a schedule and store a price-history table going forward — flag
that as follow-up if you want it.

Run from project root:
    .venv/bin/python scripts/preview_weather_stop_loss_2026-05-19.py
    .venv/bin/python scripts/preview_weather_stop_loss_2026-05-19.py --fraction 0.40
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.settlement import (  # noqa: E402
    compute_stop_loss_threshold,
    fetch_current_weather_mark,
    mark_to_market_loss,
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.50,
                        help="Stop-loss fraction of max loss (default 0.50)")
    args = parser.parse_args()

    db_path = ROOT / "tradingbot.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, market_ticker, event_slug, direction, entry_price, size
             FROM trades
            WHERE market_type='weather' AND settled=0
         ORDER BY id"""
    ).fetchall()

    if not rows:
        print("No open weather trades.")
        return 0

    print(f"Stop-loss fraction: {args.fraction:.0%}")
    print(f"Probing {len(rows)} open weather trades against current Gamma marks:\n")
    header = (
        f"{'id':>3} {'ticker':>8} {'dir':>4} {'entry':>6} {'size':>6} "
        f"{'max_loss':>9} {'mark_yes':>9} {'mark_no':>9} "
        f"{'unr_pnl':>9} {'stop_at':>9} {'FIRE?':>6}"
    )
    print(header)
    print("-" * len(header))

    triggered = 0
    saved_dollars = 0.0
    no_mark_count = 0

    class _Stub:
        # Stand-in for Trade so we can reuse mark_to_market_loss with raw rows.
        def __init__(self, direction: str, entry_price: float, size: float):
            self.direction = direction
            self.entry_price = entry_price
            self.size = size

    for r in rows:
        marks = await fetch_current_weather_mark(r["market_ticker"], event_slug=r["event_slug"])
        max_loss = r["entry_price"] * r["size"]
        threshold_loss = compute_stop_loss_threshold(r["entry_price"], r["size"], args.fraction)
        # The mark-side that triggers the stop:
        # NO position: stop when no_mark = entry_price - threshold/size = entry*(1-fraction)
        # YES position: same shape on yes_mark
        stop_at = r["entry_price"] * (1.0 - args.fraction)

        if marks is None:
            no_mark_count += 1
            print(
                f"{r['id']:>3} {r['market_ticker']:>8} {r['direction'][:3]:>4} "
                f"{r['entry_price']:>6.3f} {r['size']:>6.1f} "
                f"${max_loss:>7.2f} {'  ?':>9} {'  ?':>9} "
                f"{'  ?':>9} {stop_at:>9.3f} {'  ?':>6}"
            )
            continue

        yes_price, no_price = marks
        loss = mark_to_market_loss(_Stub(r["direction"], r["entry_price"], r["size"]),
                                   yes_price, no_price)
        fires = loss >= threshold_loss
        if fires:
            triggered += 1
            saved_dollars += (max_loss - loss)  # difference between hold-to-zero loss and stop loss
        unrealized = -loss
        print(
            f"{r['id']:>3} {r['market_ticker']:>8} {r['direction'][:3]:>4} "
            f"{r['entry_price']:>6.3f} {r['size']:>6.1f} "
            f"${max_loss:>7.2f} {yes_price:>9.3f} {no_price:>9.3f} "
            f"${unrealized:>+7.2f} {stop_at:>9.3f} {'YES' if fires else 'no':>6}"
        )

    print()
    print(f"Stop would trigger now on:        {triggered}/{len(rows)} positions")
    print(f"Capital potentially preserved:    ${saved_dollars:.2f}  "
          f"(vs hold-to-zero on the triggered set)")
    if no_mark_count:
        print(f"Could not fetch marks for:        {no_mark_count} positions "
              f"(market may already be closed/settled)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
