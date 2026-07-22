#!/usr/bin/env python3
"""graduation_ledger.py — the REALISTIC-FILLS-ERA paper ledger (reusable, read-only).

The graduation bar for the paper book is judged ONLY on strategy-conformant
realistic-fills trades. This tool is the single query for that ledger so the
exclusion rules live in code, not in ad-hoc SQL:

  - Era start: 2026-07-20 13:57 UTC (WEATHER_PAPER_REALISTIC_FILLS regime
    break). Pre-era trades are FANTASY-FILL and never quoted (STATE.md).
  - EXCLUDED: any trade whose signal reasoning contains 'GATE-LEAK-EXCLUDED'
    (e.g. trade #94 / signal 193144 — executed despite a [FILTERED] tag under
    the 0ddd462 split-boolean bug, fixed b21cc5d). Session_log_2026-07-21
    decision: settles naturally, never counts toward the bar.

Usage:  python3 scripts/graduation_ledger.py [path/to/tradingbot.db]
"""
import sqlite3
import sys
from pathlib import Path

ERA_START = "2026-07-20 13:57"          # realistic-fills regime break (UTC)
EXCLUDE_MARK = "GATE-LEAK-EXCLUDED"     # in signals.reasoning


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "tradingbot.db")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT t.id, t.market_ticker, t.platform, t.direction, t.fill_type,
               t.entry_price, t.size, t.edge_at_entry, t.settled, t.result,
               t.pnl, substr(t.timestamp, 1, 16) AS opened,
               CASE WHEN s.reasoning LIKE '%' || ? || '%' THEN 1 ELSE 0 END AS excluded
        FROM trades t
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE t.market_type = 'weather' AND t.timestamp >= ?
        ORDER BY t.id
        """,
        (EXCLUDE_MARK, ERA_START),
    ).fetchall()

    ledger = [r for r in rows if not r[12]]
    dropped = [r for r in rows if r[12]]

    print(f"== graduation ledger (realistic-fills era, >= {ERA_START} UTC) ==")
    print(f"   db: {db_path}")
    for r in ledger:
        (tid, ticker, plat, direction, fill, entry, size, edge,
         settled, result, pnl, opened, _) = r
        status = (f"{result} {pnl:+.2f}" if settled else "OPEN")
        print(f"  #{tid:<4} {opened}  {ticker:<10} {direction.upper():<3} "
              f"{fill or '-':<9} ${size:6.2f} @ {entry:.3f} "
              f"edge {edge if edge is not None else 0:+.2f}  {status}")
    for r in dropped:
        print(f"  #{r[0]:<4} {r[11]}  {r[1]:<10} -- EXCLUDED ({EXCLUDE_MARK}) "
              f"result={r[9] or 'open'} pnl={r[10] if r[10] is not None else 0:+.2f}")

    settled = [r for r in ledger if r[8]]
    wins = [r for r in settled if r[9] == "win"]
    pnl = sum(r[10] or 0.0 for r in settled)
    edges = [r[7] for r in settled if r[7] is not None]
    print(f"-- conformant: {len(ledger)} trades ({len(settled)} settled, "
          f"{len(ledger) - len(settled)} open) | wins {len(wins)}/{len(settled)} "
          f"| realized P&L {pnl:+.2f} | mean entry edge "
          f"{(sum(edges) / len(edges)):+.3f}" if edges else
          f"-- conformant: {len(ledger)} trades, none settled yet")
    if dropped:
        print(f"-- excluded ({EXCLUDE_MARK}): {len(dropped)} trade(s): "
              f"{', '.join('#' + str(r[0]) for r in dropped)} — never count toward the bar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
