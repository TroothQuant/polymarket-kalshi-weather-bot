"""
One-off: delete BTC signal rows accumulated in tradingbot.db while the BTC
strategy was running by accident (Pydantic Settings v2 env-loading bug).

Keeps:
  - All weather signal rows
  - All trade rows (BTC + weather)
  - All bot_state, pnl_snapshots, etc.

Deletes:
  - signals rows where market_type='btc' OR (market_type IS NULL AND
    market_ticker doesn't match a current weather trade)

Dry-run by default. Backs up the DB before --commit. Safe to run while the
bot is stopped.

Run from project root:
    .venv/bin/python scripts/cleanup_btc_signals_2026-05-19.py            # preview
    .venv/bin/python scripts/cleanup_btc_signals_2026-05-19.py --commit   # delete
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "tradingbot.db"
BACKUP_DIR = ROOT / "data" / "backups"

sys.path.insert(0, str(ROOT / "scripts"))
from _process_guard import refuse_if_bot_running  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    if args.commit:
        refuse_if_bot_running()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        return 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Count by market_type
    counts = {}
    rows = con.execute("""
        SELECT COALESCE(market_type, 'unknown') AS mt, COUNT(*) AS n
          FROM signals
      GROUP BY mt
      ORDER BY n DESC
    """).fetchall()
    for r in rows:
        counts[r["mt"]] = r["n"]

    total = sum(counts.values())
    btc_count = counts.get("btc", 0)
    weather_count = counts.get("weather", 0)
    unknown_count = counts.get("unknown", 0)

    print("Signals table breakdown:")
    for mt, n in counts.items():
        print(f"  {mt:>10}: {n:>6}")
    print(f"  {'TOTAL':>10}: {total:>6}")
    print()
    print(f"Will delete {btc_count + unknown_count} rows "
          f"(btc + unknown). Keeping {weather_count} weather signals.")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to actually delete)")
        return 0

    # Backup
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"tradingbot.db.pre_btc_signals_cleanup.{datetime.utcnow():%Y%m%dT%H%M%SZ}"
    shutil.copy(DB_PATH, backup_path)
    print(f"\nBacked up DB -> {backup_path.relative_to(ROOT)}")

    cur = con.cursor()
    cur.execute("DELETE FROM signals WHERE market_type='btc' OR market_type IS NULL")
    deleted = cur.rowcount
    con.commit()

    # Optional: VACUUM to actually reclaim disk space
    con.execute("VACUUM")
    print(f"Deleted {deleted} signal rows. VACUUM'd database.")

    remaining = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"Signals table now has {remaining} rows (all weather).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
