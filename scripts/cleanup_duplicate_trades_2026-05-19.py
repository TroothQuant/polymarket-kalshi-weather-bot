"""
One-off cleanup for the duplicate weather trades created by the per-day dedup
gap (bug fixed in backend/core/scheduler.py on 2026-05-19).

Effect on the trades table:
  - 2265993 (Denver 5/17): keep trade #2  (NO @ 0.125), delete #3, #8
  - 2274465 (Denver 5/18): keep trade #6  (NO @ 0.050), delete #10
  - 2274497 (LA     5/18): keep trade #4  (NO @ 0.480), delete #9      ← see note

Notes
  - The 2274497 pair is the "LA opposite-direction" case: trade #4 is NO @ 0.48
    on 5/16 and trade #9 is YES @ 0.06 on 5/17. Default kept = the original
    NO. Override with --keep-yes-for-la to keep trade #9 instead.
  - DRY RUN by default. Pass --commit to actually delete rows.
  - Writes a JSON backup of the deleted rows to data/backups/
    before committing, so the action is reversible.

Run from project root:
    .venv/bin/python scripts/cleanup_duplicate_trades_2026-05-19.py            # dry run
    .venv/bin/python scripts/cleanup_duplicate_trades_2026-05-19.py --commit   # actually delete
    .venv/bin/python scripts/cleanup_duplicate_trades_2026-05-19.py --keep-yes-for-la --commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def trades_to_delete(keep_yes_for_la: bool) -> list[int]:
    # ids come from the diagnostic query run on 2026-05-19 at 12:10 UTC.
    # These are stable — the trades aren't being inserted again now that the
    # scheduler dedup guard is fixed.
    base = {
        "2265993": {"keep": 2, "delete": [3, 8]},
        "2274465": {"keep": 6, "delete": [10]},
        "2274497": {"keep": 9 if keep_yes_for_la else 4,
                    "delete": [4] if keep_yes_for_la else [9]},
    }
    return sorted(tid for spec in base.values() for tid in spec["delete"])


# Audit 2026-05-19 HIGH #18: validate hardcoded ids match the expected
# (ticker, direction) tuples before deleting. On a re-seeded DB or after row
# renumbering, the ids would otherwise refer to completely different trades.
_EXPECTED = {
    2: ("2265993", "no"),
    3: ("2265993", "no"),
    8: ("2265993", "no"),
    4: ("2274497", "no"),
    6: ("2274465", "no"),
    9: ("2274497", "yes"),
    10: ("2274465", "no"),
}


def _validate_targets(rows) -> tuple[bool, list[str]]:
    """Verify every target row matches its expected ticker+direction.

    Returns (ok, errors). ok=False means do not proceed.
    """
    errors: list[str] = []
    for r in rows:
        tid = r["id"]
        expected = _EXPECTED.get(tid)
        if not expected:
            errors.append(f"id={tid} has no expected (ticker,dir) mapping")
            continue
        exp_ticker, exp_dir = expected
        actual_ticker = str(r["market_ticker"] or "")
        actual_dir = str(r["direction"] or "").lower()
        if actual_ticker != exp_ticker or actual_dir != exp_dir:
            errors.append(
                f"id={tid}: expected ({exp_ticker}, {exp_dir}), "
                f"got ({actual_ticker}, {actual_dir})"
            )
    return (len(errors) == 0, errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Actually delete rows")
    parser.add_argument("--keep-yes-for-la", dest="keep_yes_for_la",
                        action="store_true", default=True,
                        help="Keep trade #9 (YES @ 0.06) instead of #4 (NO @ 0.48) for ticker 2274497 [default]")
    parser.add_argument("--keep-no-for-la", dest="keep_yes_for_la",
                        action="store_false",
                        help="Override: keep trade #4 (NO @ 0.48) instead of #9 (YES @ 0.06)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from _process_guard import refuse_if_bot_running  # noqa: E402

    if args.commit:
        refuse_if_bot_running()

    db_path = root / "tradingbot.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist")
        return 1

    ids = trades_to_delete(args.keep_yes_for_la)
    placeholders = ",".join("?" * len(ids))

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        f"SELECT * FROM trades WHERE id IN ({placeholders})", ids
    ).fetchall()

    if not rows:
        print(f"No rows match ids {ids} — nothing to do (already cleaned up?)")
        return 0

    # Audit 2026-05-19 HIGH #18: never delete a row whose (ticker, direction)
    # doesn't match the expected fingerprint. If the DB has been re-seeded or
    # renumbered, the hardcoded ids could point at completely different
    # trades.
    ok, errors = _validate_targets(rows)
    if not ok:
        print("REFUSED: target id(s) don't match expected (ticker, direction):")
        for e in errors:
            print(f"  - {e}")
        print("Aborting without delete. Re-derive ids from a fresh diagnostic.")
        return 5

    print(f"Will delete {len(rows)} trade rows:")
    for r in rows:
        print(f"  id={r['id']:>3}  ticker={r['market_ticker']}  "
              f"dir={r['direction']}  entry={r['entry_price']:.3f}  "
              f"date={(r['timestamp'] or '')[:10]}  slug={r['event_slug']}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to actually delete)")
        return 0

    # Backup
    backup_dir = root / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"trades_pre_cleanup_{datetime.utcnow():%Y%m%dT%H%M%SZ}.json"
    backup_path.write_text(json.dumps([dict(r) for r in rows], default=str, indent=2))
    print(f"\nBacked up {len(rows)} rows -> {backup_path}")

    cur = con.cursor()
    cur.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", ids)
    con.commit()
    print(f"Deleted {cur.rowcount} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
