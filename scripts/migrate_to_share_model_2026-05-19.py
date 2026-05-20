"""
One-off migration: convert the weather bot's historical trade ledger from the
old fictional CFD payoff model to the correct Polymarket share-purchase model.

Background
----------
Before today, `backend.core.settlement.calculate_pnl` modeled trades as
fixed-notional CFD bets:
    win:  pnl = +size * (1 - entry_price)
    loss: pnl = -size * entry_price
This treats `size` as a max-payout notional, not as a stake. It under-counts
real Polymarket P&L by a factor of 1/entry_price (often 5x–50x).

Real Polymarket binary mechanics:
    stake $size buys (size / entry_price) shares at $1 face value
    win:  payout = shares * 1.0   →  pnl = shares * (1 - entry_price)
    loss: payout = 0              →  pnl = -size  (full stake lost)
    stop-loss at mark X: payout = shares * X     →  pnl = shares * (X - entry_price)

Today's code change updated `calculate_pnl`, `mark_to_market_loss`,
`compute_stop_loss_threshold`, and `update_bot_state_with_settlements` to use
share-purchase math, and added stake-deduction-at-entry to
`weather_scan_and_trade_job`. This script backfills the existing DB rows so
historical bookkeeping is consistent.

What it does
------------
1. Backs up tradingbot.db to data/backups/tradingbot.db.pre_share_migration.<ts>
2. For each settled weather trade, recomputes pnl using the share-purchase
   formula.
3. Rewrites bot_state.bankroll from scratch:
     bankroll = INITIAL_BANKROLL
              + sum(new_pnl for all settled trades)
              - sum(size       for all open weather trades)
4. Rewrites bot_state.total_pnl = sum(new_pnl across ALL settled trades).
5. Idempotent — running it twice yields the same final state.

Run from project root:
    .venv/bin/python scripts/migrate_to_share_model_2026-05-19.py            # dry-run preview
    .venv/bin/python scripts/migrate_to_share_model_2026-05-19.py --commit   # apply

WARNING: stop the bot before --commit, otherwise the live bot may overwrite
the migrated bot_state row on its next heartbeat.
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
INITIAL_BANKROLL = 10000.0

sys.path.insert(0, str(ROOT / "scripts"))
from _process_guard import refuse_if_bot_running  # noqa: E402


def new_pnl_for_settled(direction: str, entry: float, size: float,
                        result: str, settlement_value, old_pnl: float) -> float:
    """Compute the share-purchase pnl for a single settled trade."""
    direction = (direction or "").lower()
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    if entry <= 0 or entry >= 1:
        return float(old_pnl or 0.0)

    shares = size / entry

    if result == "stop_loss":
        # Bot closed at a current mark; the old pnl was size*(mark - entry).
        # Solve for the mark, then apply share-purchase: shares*(mark - entry).
        # Algebraic shortcut: new_pnl = old_pnl / entry.
        return round(old_pnl / entry, 2)

    # Naturally settled — outcome from settlement_value (1.0 if YES/UP won)
    if settlement_value is None:
        return float(old_pnl or 0.0)

    if direction == "yes":
        my_side_value = settlement_value
    else:
        my_side_value = 1.0 - settlement_value

    return round(shares * (my_side_value - entry), 2)


_MIGRATION_ID = "share_model_2026-05-19"


def _ensure_migrations_table(con: sqlite3.Connection) -> None:
    """Idempotency sentinel table (audit 2026-05-19 CRITICAL #4).

    The stop_loss pnl shortcut `new_pnl = old_pnl / entry` is only correct
    on pre-migration CFD-shaped rows; running --commit a second time would
    divide already-migrated rows by `entry` again and corrupt bankroll by
    hundreds of dollars. This table records that the migration has run so
    a subsequent --commit can refuse loudly instead of silently corrupting.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at   TEXT NOT NULL,
            note         TEXT
        )
    """)
    con.commit()


def _migration_already_applied(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM migrations WHERE migration_id = ?", (_MIGRATION_ID,)
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Override the idempotency sentinel and re-run --commit. "
             "ONLY use if you have manually verified the trades table is "
             "back to CFD units (e.g. restored from a pre-migration backup). "
             "Otherwise this will corrupt the ledger.",
    )
    parser.add_argument(
        "--mark-applied",
        action="store_true",
        help="Backfill the migration sentinel without running the migration. "
             "Use this once on a DB where the migration was ALREADY applied "
             "(e.g. 2026-05-19) but the sentinel was added later. After this "
             "the script will refuse any future --commit unless --force.",
    )
    args = parser.parse_args()

    if args.mark_applied:
        if not DB_PATH.exists():
            print(f"ERROR: {DB_PATH} not found")
            return 1
        con = sqlite3.connect(DB_PATH)
        _ensure_migrations_table(con)
        if _migration_already_applied(con):
            print(f"Sentinel for '{_MIGRATION_ID}' already present. No-op.")
            return 0
        con.execute(
            "INSERT INTO migrations (migration_id, applied_at, note) VALUES (?, ?, ?)",
            (
                _MIGRATION_ID,
                datetime.utcnow().isoformat() + "Z",
                "backfilled by --mark-applied (assumed applied 2026-05-19)",
            ),
        )
        con.commit()
        con.close()
        print(f"Marked '{_MIGRATION_ID}' as applied. Future --commit will refuse.")
        return 0

    if args.commit:
        refuse_if_bot_running()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        return 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Audit 2026-05-19 CRITICAL #4: refuse to re-run --commit unless --force.
    _ensure_migrations_table(con)
    already = _migration_already_applied(con)
    if already:
        print(f"NOTICE: migration '{_MIGRATION_ID}' was already applied to this DB.")
        if args.commit and not args.force:
            print("Refusing to --commit a second time -- the stop_loss "
                  "shortcut would double-divide by entry_price and corrupt "
                  "bankroll. Use --force only after restoring from a "
                  "pre-migration backup.")
            return 4
        if args.commit and args.force:
            print("--force passed; proceeding (you asserted the ledger is "
                  "back in CFD units).")

    settled = con.execute("""
        SELECT id, market_ticker, event_slug, direction, entry_price, size,
               settled, settlement_value, pnl, result
          FROM trades
         WHERE market_type='weather' AND settled=1
      ORDER BY id
    """).fetchall()

    # Audit 2026-05-19 HIGH #16: refuse to --commit if any settled row has
    # settlement_value=None and result != 'stop_loss'. new_pnl_for_settled
    # falls through such rows by returning old_pnl unchanged, silently
    # mixing CFD-shape pnl with share-purchase-shape pnl in the same total.
    bad_rows = [
        r for r in settled
        if r["settlement_value"] is None and (r["result"] or "") != "stop_loss"
    ]
    if bad_rows and args.commit:
        print(f"ERROR: {len(bad_rows)} settled row(s) have settlement_value=NULL "
              f"and result != 'stop_loss'. Refusing to --commit -- those rows "
              f"would silently retain CFD-shaped pnl while the rest is migrated, "
              f"producing a mixed-unit total.")
        for r in bad_rows[:10]:
            print(f"  trade #{r['id']}  ticker={r['market_ticker']}  "
                  f"result={r['result']!r}  pnl={r['pnl']}")
        if len(bad_rows) > 10:
            print(f"  ...and {len(bad_rows) - 10} more")
        print("Resolve these rows manually (set a result + settlement_value, "
              "or change to result='stop_loss' if that's what happened) before "
              "re-running --commit.")
        return 5

    open_trades = con.execute("""
        SELECT id, market_ticker, direction, entry_price, size
          FROM trades
         WHERE market_type='weather' AND settled=0
      ORDER BY id
    """).fetchall()

    state = con.execute("SELECT id, bankroll, total_pnl FROM bot_state").fetchone()

    if not state:
        print("ERROR: no bot_state row")
        return 1

    print("Settled weather trades — pnl change:")
    print(f"{'id':>3} {'dir':>4} {'entry':>6} {'size':>5} {'result':>10} "
          f"{'old_pnl':>9} {'new_pnl':>9}")
    new_pnls = {}
    for r in settled:
        new_pnl = new_pnl_for_settled(
            r["direction"], r["entry_price"], r["size"],
            r["result"], r["settlement_value"], r["pnl"],
        )
        new_pnls[r["id"]] = new_pnl
        print(f"{r['id']:>3} {r['direction'][:3]:>4} {r['entry_price']:>6.3f} "
              f"{r['size']:>5.0f} {r['result']:>10} "
              f"{r['pnl']:>+9.2f} {new_pnl:>+9.2f}")

    open_stake_sum = sum(float(r["size"]) for r in open_trades)
    new_total_pnl = sum(new_pnls.values())
    new_bankroll = INITIAL_BANKROLL + new_total_pnl - open_stake_sum

    print()
    print(f"Open trade count:               {len(open_trades)}")
    print(f"Open stake sum (locked stakes): ${open_stake_sum:.2f}")
    print(f"Settled trade count:            {len(settled)}")
    print()
    print(f"bot_state.total_pnl  before:  ${state['total_pnl']:+.2f}")
    print(f"bot_state.total_pnl  after:   ${new_total_pnl:+.2f}")
    print(f"bot_state.bankroll   before:  ${state['bankroll']:.2f}")
    print(f"bot_state.bankroll   after:   ${new_bankroll:.2f}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to apply)")
        return 0

    # Backup
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"tradingbot.db.pre_share_migration.{datetime.utcnow():%Y%m%dT%H%M%SZ}"
    shutil.copy(DB_PATH, backup_path)
    print(f"\nBacked up DB -> {backup_path.relative_to(ROOT)}")

    cur = con.cursor()
    for trade_id, new_pnl in new_pnls.items():
        cur.execute("UPDATE trades SET pnl=? WHERE id=?", (new_pnl, trade_id))
    cur.execute(
        "UPDATE bot_state SET bankroll=?, total_pnl=? WHERE id=?",
        (new_bankroll, new_total_pnl, state["id"]),
    )
    # Record the sentinel so a second --commit refuses (CRITICAL #4).
    cur.execute(
        "INSERT OR REPLACE INTO migrations (migration_id, applied_at, note) VALUES (?, ?, ?)",
        (
            _MIGRATION_ID,
            datetime.utcnow().isoformat() + "Z",
            f"settled_rows={len(new_pnls)} open_stake_sum={open_stake_sum:.2f}",
        ),
    )
    con.commit()
    print(f"Updated {len(new_pnls)} trade rows and 1 bot_state row.")
    print(f"Recorded migration sentinel '{_MIGRATION_ID}' in the migrations table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
