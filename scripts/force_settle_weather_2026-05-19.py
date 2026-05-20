"""
Manually trigger the bot's own settle_pending_trades() function against the
current weather trades. Use this when the dashboard shows positions as
"overdue" but the periodic settlement_job hasn't picked them up — usually
because the bot was restarted, the cycle hasn't run yet, or because we want
to verify what the settlement layer does without waiting 2-30 min.

Safe to run while the bot is running — uses the same code path that the
scheduler uses, with the same SQLAlchemy session boundaries. The bot's own
write lock and our write lock cooperate via SQLite's busy_timeout.

Run from project root:
    .venv/bin/python scripts/force_settle_weather_2026-05-19.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.settlement import (  # noqa: E402
    settle_pending_trades,
    update_bot_state_with_settlements,
)
from backend.models.database import SessionLocal  # noqa: E402


async def main() -> int:
    db = SessionLocal()
    try:
        settled = await settle_pending_trades(db)
        if not settled:
            print("Nothing settled — markets are either still open on Polymarket "
                  "or the parser does not consider them resolved.")
            return 0
        await update_bot_state_with_settlements(db, settled)
        wins = sum(1 for t in settled if t.result == "win")
        losses = sum(1 for t in settled if t.result == "loss")
        total_pnl = sum(t.pnl for t in settled if t.pnl is not None)
        print(f"Settled {len(settled)} trades: {wins}W / {losses}L, P&L ${total_pnl:+.2f}")
        for t in settled:
            sign = "+" if (t.pnl or 0) >= 0 else ""
            print(f"  trade #{t.id:>2}  {t.event_slug}: {t.result.upper()} "
                  f"{sign}${t.pnl:.2f}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
