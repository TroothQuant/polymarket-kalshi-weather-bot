"""OI1 (2026-06-24; v2 aggressive-hybrid 2026-07-09): unit-test the live hard
caps in resolve_weather_live — per-trade $ cap (with the 15-share min-size bump),
daily realized-loss kill-switch, total open-exposure cap, and the NYC-only live
restriction. REAL in-memory SQLite so the actual cap SQL runs. No py-clob, no
network: the live trader is a stub whose execute_aggressive_hybrid is mocked.
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
    # cap $15 (the live value). model NO-side prob 0.40 → limit 0.35 →
    # min_for_15 = 15*0.35+0.05 = $5.30 (the floor the min-size bump applies).
    s = SimpleNamespace(
        WEATHER_LIVE_TRADING=True, WEATHER_LIVE_MAX_TRADE_USD=15.0,
        WEATHER_LIVE_DAILY_LOSS_STOP_USD=10.0, WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD=100.0,
        WEATHER_LIVE_MIN_EDGE_FLOOR=0.05, WEATHER_LIVE_CITIES="nyc")
    s.__dict__.update(over)
    return s


def _signal(city="nyc", direction="no", model_yes=0.60):
    mkt = SimpleNamespace(platform="polymarket", market_type="weather",
                          city_key=city, market_id="2400000", slug="slug",
                          token_id_yes="YES", token_id_no="NO")
    return SimpleNamespace(market=mkt, direction=direction, edge=0.20,
                           market_probability=0.40, model_probability=model_yes)


def _factory(rec):
    def make():
        def execute_aggressive_hybrid(token_id, size_usd, limit_price):
            rec.append(size_usd)
            return {"order_id": "OID", "price": limit_price,
                    "filled_shares": size_usd / limit_price, "filled_cost": size_usd,
                    "fill_price": limit_price, "resting_shares": 0.0, "status": "matched"}
        return SimpleNamespace(execute_aggressive_hybrid=execute_aggressive_hybrid)
    return make


_LIMIT = 0.35                  # NO side, model_yes 0.60 → 1-0.60-0.05
_MIN15 = 15.0 * _LIMIT + 0.05  # 5.30 — the min-size bump floor


def test_per_trade_cap_clamps_size():
    rec = []
    d = resolve_weather_live(_signal(), trade_size=100.0, entry_price=0.40,
                             db=_db(), settings=_settings(), live_trader_factory=_factory(rec))
    assert d.action == "fill"
    assert rec == [15.0]            # $100 requested → clamped to the $15 per-trade cap


def test_min_size_bump_clears_15_shares():
    # A tiny Kelly size ($2) is bumped UP to the 15-share floor ($5.30), never sub-15.
    rec = []
    d = resolve_weather_live(_signal(), trade_size=2.0, entry_price=0.40,
                             db=_db(), settings=_settings(), live_trader_factory=_factory(rec))
    assert d.action == "fill"
    assert rec[0] == pytest.approx(_MIN15, abs=1e-6)
    assert rec[0] / _LIMIT >= 15


def test_guard_skip_when_cap_cannot_clear_15():
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, _db(),
                             _settings(WEATHER_LIVE_MAX_TRADE_USD=5.0), _factory(rec))
    assert d.action == "guard_skip" and rec == []


def test_daily_loss_kill_switch_halts():
    db = _db()
    db.add(Trade(order_id="L1", market_type="weather", platform="polymarket",
                 settled=True, pnl=-10.0, size=2.0, result="loss",
                 timestamp=datetime.utcnow(), settlement_time=datetime.utcnow()))
    db.commit()
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, db, _settings(), _factory(rec))
    assert d.action == "halt" and rec == []


def test_total_exposure_cap_halts():
    db = _db()
    for i in range(10):  # 10 open × $10 = $100 open (== cap; any new bump exceeds it)
        db.add(Trade(order_id=f"O{i}", market_type="weather", platform="polymarket",
                     result="pending", size=10.0, timestamp=datetime.utcnow()))
    db.commit()
    rec = []
    # 100 open + bumped new (>=5.30) > 100 cap → halt, no order attempted
    d = resolve_weather_live(_signal(), 2.0, 0.40, db,
                             _settings(WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD=100.0), _factory(rec))
    assert d.action == "halt" and rec == []


def test_exposure_cap_allows_under_limit():
    db = _db()
    db.add(Trade(order_id="O1", market_type="weather", platform="polymarket",
                 result="pending", size=5.0, timestamp=datetime.utcnow()))
    db.commit()
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, db, _settings(), _factory(rec))
    assert d.action == "fill" and rec[0] == pytest.approx(_MIN15, abs=1e-6)


def test_nyc_only_guard_sends_non_nyc_to_paper():
    rec = []
    d = resolve_weather_live(_signal(city="chicago"), 2.0, 0.40, _db(), _settings(), _factory(rec))
    assert d.action == "paper" and rec == []


def test_flag_off_is_paper():
    rec = []
    d = resolve_weather_live(_signal(), 2.0, 0.40, _db(),
                             _settings(WEATHER_LIVE_TRADING=False), _factory(rec))
    assert d.action == "paper" and rec == []
