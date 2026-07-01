"""OI1 (2026-06-24): unit-test the live hard caps in resolve_weather_live —
per-trade $ cap, daily realized-loss kill-switch, total open-exposure cap, and
the NYC-only live restriction. Uses a REAL in-memory SQLite session so the
actual SQL in _live_daily_realized_loss / _live_total_open_exposure is exercised.
No py-clob, no network: the live trader is a stub factory.
"""
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade
from backend.core.scheduler import resolve_weather_live


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _settings(**over):
    s = SimpleNamespace(
        WEATHER_LIVE_TRADING=True, WEATHER_LIVE_MAX_TRADE_USD=2.0,
        WEATHER_LIVE_DAILY_LOSS_STOP_USD=10.0, WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD=10.0,
        WEATHER_LIVE_CITIES="nyc")
    s.__dict__.update(over)
    return s


def _signal(city="nyc", direction="no"):
    mkt = SimpleNamespace(platform="polymarket", market_type="weather",
                          city_key=city, token_id_yes="YES", token_id_no="NO")
    return SimpleNamespace(market=mkt, direction=direction)


def _factory(rec):
    def make():
        def execute_buy(token_id, size_usd, market_price):
            rec.append(size_usd)
            return {"order_id": "OID", "fill_price": market_price,
                    "shares": size_usd / market_price, "cost": size_usd}
        return SimpleNamespace(execute_buy=execute_buy)
    return make


def test_per_trade_cap_clamps_size():
    rec = []
    d = resolve_weather_live(_signal(), trade_size=100.0, entry_price=0.40,
                             db=_db(), settings=_settings(), live_trader_factory=_factory(rec))
    assert d.action == "fill"
    assert rec == [2.0]            # $100 requested → clamped to the $2 per-trade cap


def test_daily_loss_kill_switch_halts():
    db = _db()
    db.add(Trade(order_id="L1", market_type="weather", platform="polymarket",
                 settled=True, pnl=-10.0, size=2.0, result="loss",
                 timestamp=datetime.utcnow(),
                 settlement_time=datetime.utcnow())); db.commit()  # daily halt keys on settlement day (audit 2)
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, db, _settings(), _factory(rec))
    assert d.action == "halt" and rec == []    # kill-switch tripped, no order attempted


def test_total_exposure_cap_halts():
    db = _db()
    for i in range(3):  # 3 open live positions × $3 = $9 open
        db.add(Trade(order_id=f"O{i}", market_type="weather", platform="polymarket",
                     result="pending", size=3.0, timestamp=datetime.utcnow()))
    db.commit()
    rec = []
    # 9 open + 2 new = 11 > 10 cap → halt
    d = resolve_weather_live(_signal(), 2.0, 0.40, db, _settings(), _factory(rec))
    assert d.action == "halt" and rec == []


def test_exposure_cap_allows_under_limit():
    db = _db()
    db.add(Trade(order_id="O1", market_type="weather", platform="polymarket",
                 result="pending", size=5.0, timestamp=datetime.utcnow())); db.commit()
    rec = []
    # 5 open + 2 new = 7 <= 10 → proceeds
    d = resolve_weather_live(_signal(), 2.0, 0.40, db, _settings(), _factory(rec))
    assert d.action == "fill" and rec == [2.0]


def test_nyc_only_guard_sends_non_nyc_to_paper():
    rec = []
    d = resolve_weather_live(_signal(city="chicago"), 2.0, 0.40, _db(), _settings(), _factory(rec))
    assert d.action == "paper" and rec == []   # non-NYC never reaches the live path


def test_flag_off_is_paper():
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, _db(),
                             _settings(WEATHER_LIVE_TRADING=False), _factory(rec))
    assert d.action == "paper" and rec == []
