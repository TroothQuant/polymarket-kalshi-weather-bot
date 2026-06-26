"""Dry-run trip of each live guard at the REAL configured thresholds.

Loads the real settings from the live env (weather-live-mac.env: $11 per-trade,
$25 daily-stop, $33 exposure), flips WEATHER_LIVE_TRADING True IN-PROCESS ONLY
(the env file stays false), and exercises resolve_weather_live with an in-memory
SQLite session + a STUB trader factory. NO real order is ever posted; the stub
just records the size it would have sent. Proves each guard actually halts.
"""
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade
from backend.core.scheduler import (
    resolve_weather_live, _live_daily_realized_loss, _live_total_open_exposure,
)
from backend.config import settings

settings.WEATHER_LIVE_TRADING = True  # in-process toggle for the test ONLY


def fresh_db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def signal(city="nyc"):
    mkt = SimpleNamespace(platform="polymarket", market_type="weather",
                          city_key=city, token_id_yes="YES", token_id_no="NO")
    return SimpleNamespace(market=mkt, direction="no")


def stub_factory(rec):
    def make():
        def execute_buy(token_id, size_usd, market_price):
            rec.append(size_usd)  # record only — NEVER posts
            return {"order_id": "DRYRUN", "fill_price": market_price,
                    "shares": size_usd / market_price, "cost": size_usd}
        return SimpleNamespace(execute_buy=execute_buy)
    return make


C, D, E = (settings.WEATHER_LIVE_MAX_TRADE_USD,
           settings.WEATHER_LIVE_DAILY_LOSS_STOP_USD,
           settings.WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD)
print(f"REAL thresholds — per-trade=${C}  daily-stop=${D}  exposure=${E}\n")
ok = True

# 1) PER-TRADE $11 CAP — request way over, expect clamp to $11
rec = []
d = resolve_weather_live(signal(), trade_size=50.0, entry_price=0.50,
                         db=fresh_db(), settings=settings, live_trader_factory=stub_factory(rec))
clamp_ok = (d.action == "fill" and rec == [C])
ok &= clamp_ok
print(f"[1 per-trade cap]  requested $50 -> action={d.action}, size→trader={rec}")
print(f"   {'✅' if clamp_ok else '❌'} clamped to ${C}\n")

# 2) DAILY-HALT $25 — seed a settled live loss past the stop, expect halt + no order
db2 = fresh_db()
db2.add(Trade(order_id="L", market_type="weather", platform="polymarket", settled=True,
              pnl=-26.0, size=11.0, result="loss", timestamp=datetime.utcnow())); db2.commit()
seen = _live_daily_realized_loss(db2)
rec = []
d = resolve_weather_live(signal(), 11.0, 0.50, db2, settings, stub_factory(rec))
halt_ok = (d.action == "halt" and rec == [])
ok &= halt_ok
print(f"[2 daily-halt]  seeded realized loss=${seen:.2f} (>= ${D}) -> action={d.action}, order_attempted={bool(rec)}")
print(f"   {'✅' if halt_ok else '❌'} halted, no order\n")

# 3) EXPOSURE $33 CAP — seed 3 open live @ $11 = $33, +$11 new = $44 > $33, expect halt
db3 = fresh_db()
for i in range(3):
    db3.add(Trade(order_id=f"O{i}", market_type="weather", platform="polymarket",
                  result="pending", size=11.0, timestamp=datetime.utcnow()))
db3.commit()
seen = _live_total_open_exposure(db3)
rec = []
d = resolve_weather_live(signal(), 11.0, 0.50, db3, settings, stub_factory(rec))
exp_ok = (d.action == "halt" and rec == [])
ok &= exp_ok
print(f"[3 exposure cap]  open=${seen:.2f} + new $11 = ${seen+11:.0f} (> ${E}) -> action={d.action}, order_attempted={bool(rec)}")
print(f"   {'✅' if exp_ok else '❌'} halted, no order\n")

print("=" * 56)
print("ALL GUARDS TRIPPED CORRECTLY ✅" if ok else "A GUARD DID NOT TRIP ❌")
print("=" * 56)
