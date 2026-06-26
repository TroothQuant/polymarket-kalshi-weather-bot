"""F3 reconciliation dry-run: prove BOTH-direction detection (ghost + phantom)
and the grace window, with NO network and WITHOUT touching tradingbot_live.db
(monkeypatches SessionLocal to an in-memory DB and the data-api fetch to a stub).
"""
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade, BotState
from backend.config import settings
import backend.core.scheduler as sch

settings.WEATHER_LIVE_TRADING = True          # in-process only
settings.POLYMARKET_FUNDER_ADDRESS = "0xFUNDER"

eng = create_engine("sqlite:///:memory:")
Base.metadata.create_all(eng)
SL = sessionmaker(bind=eng)
sch.SessionLocal = SL                         # monkeypatch the DB the helper uses

# seed: one recorded OPEN live position on TOKEN_A + a BotState
db = SL()
db.add(BotState(id=1, bankroll=50.0, is_running=True))
db.add(Trade(order_id="O1", token_id="TOKEN_A", market_type="weather",
             platform="polymarket", result="pending", size=11.0,
             entry_price=0.55, timestamp=datetime.utcnow()))
db.commit(); db.close()

def status():
    d = SL(); s = d.query(BotState).first().reconcile_status; d.close(); return s

ok = True
# on-chain shows TOKEN_B (phantom) and NOT TOKEN_A (ghost)
sch._recon_divergence_counts.clear()
sch._fetch_onchain_positions = lambda funder: {"TOKEN_B": 20.0}

sch._reconcile_live_positions()
c1 = status()
print(f"cycle 1 (grace {sch.RECON_GRACE_CYCLES} not yet met) -> reconcile_status={c1!r}  EXPECT None")
ok &= (c1 is None)

sch._reconcile_live_positions()
c2 = status()
print(f"cycle 2 (grace met)                  -> reconcile_status={c2!r}  EXPECT '1 phantom / 1 ghost'")
ok &= (c2 is not None and "1 phantom" in c2 and "1 ghost" in c2)

# now on-chain matches the recorded position -> divergence clears to 'ok'
sch._recon_divergence_counts.clear()
sch._fetch_onchain_positions = lambda funder: {"TOKEN_A": 20.0}
sch._reconcile_live_positions()
c3 = status()
print(f"after on-chain matches recorded      -> reconcile_status={c3!r}  EXPECT 'ok'")
ok &= (c3 == "ok")

# fetch failure must NOT crash and must NOT change status
def _boom(funder):
    raise RuntimeError("data-api down")
sch._fetch_onchain_positions = _boom
sch._reconcile_live_positions()
print(f"after data-api fetch failure         -> reconcile_status={status()!r}  EXPECT unchanged 'ok' (no crash)")
ok &= (status() == "ok")

print("=" * 56)
print("F3 RECONCILE DRY-RUN PASSED ✅" if ok else "F3 RECONCILE DRY-RUN FAILED ❌")
