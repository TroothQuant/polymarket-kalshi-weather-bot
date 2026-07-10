"""Regression tests for the 2026-07-01 Fable-5 live-path audit fixes.

Covers:
  1c  scan loop — a booked live fill survives a LATER candidate raising.
  2   daily-loss halt keys on settlement day, not entry day.
  3a  _parse_fill never raises on a non-dict response.
  3c  execute_buy: non-dict response = clean non-fill; unparseable response +
      a confirming order lookup = fill recorded (never a phantom).
  5c  build_order_args refuses < 15 shares (the CLOB minimum).
"""
import asyncio
import types
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade, BotState
from backend.core import scheduler
from backend.core.scheduler import _live_daily_realized_loss, LiveDecision
from backend.core.live_trader import WeatherLiveTrader


def _mem_sessionmaker():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


# ── 5c: 15-share CLOB minimum ────────────────────────────────────────────────
def test_build_order_args_refuses_sub_15_shares():
    # $11 at price 0.80 → +2 ticks = 0.82 → 11/0.82 = 13.41 shares < 15 → refuse.
    import pytest
    with pytest.raises(ValueError):
        WeatherLiveTrader.build_order_args("TOK", 11.0, 0.80)


def test_build_order_args_allows_15_plus_shares():
    # $11 at price 0.40 → 0.42 → 26.19 shares ≥ 15 → fine.
    args = WeatherLiveTrader.build_order_args("TOK", 11.0, 0.40)
    assert args["size"] >= 15
    assert args["price"] == 0.42


# ── 3a / 3c: fill parsing never confuses "unparseable" with "no fill" ─────────
def test_parse_fill_nondict_returns_none_no_raise():
    assert WeatherLiveTrader._parse_fill(["not", "a", "dict"], "OID") is None
    assert WeatherLiveTrader._parse_fill(None, "OID") is None


class _FakeClient:
    def __init__(self, post_resp, order_rec=None):
        self._post_resp = post_resp
        self._order_rec = order_rec
        self.get_order_calls = []

    def get_tick_size(self, token_id):
        return "0.01"

    def create_market_order(self, order_args, options=None):
        return {"signed": True}

    def post_order(self, signed, order_type):
        return self._post_resp

    def get_order(self, order_id):
        self.get_order_calls.append(order_id)
        return self._order_rec


def _trader_with_client(client):
    t = WeatherLiveTrader.__new__(WeatherLiveTrader)  # bypass __init__ (needs a wallet)
    t.client = client
    return t


def test_execute_buy_nondict_response_is_clean_nonfill():
    t = _trader_with_client(_FakeClient(post_resp=["garbage"]))
    # No order id, non-dict → routine 0-fill, no exception, no lookup.
    assert t.execute_buy("TOK", 11.0, 0.40) is None
    assert t.client.get_order_calls == []


def test_execute_buy_unparseable_but_lookup_confirms_fill():
    # Response has an order id + a non-kill status but no making/taking → _parse_fill
    # returns None; the order lookup then CONFIRMS a real fill, so we record it
    # rather than silently drop a real-money position.
    post = {"orderID": "OID9", "status": "delayed"}
    rec = {"size_matched": "20", "price": "0.55"}
    t = _trader_with_client(_FakeClient(post_resp=post, order_rec=rec))
    fill = t.execute_buy("TOK", 11.0, 0.40)
    assert fill is not None
    assert fill["order_id"] == "OID9"
    assert abs(fill["shares"] - 20.0) < 1e-9
    assert abs(fill["cost"] - 11.0) < 1e-9      # 20 * 0.55
    assert t.client.get_order_calls == ["OID9"]


def test_execute_buy_unparseable_and_lookup_empty_is_nonfill():
    # Lookup shows no matched size → we do NOT fabricate a fill (returns None).
    post = {"orderID": "OID9", "status": "delayed"}
    rec = {"size_matched": "0", "price": "0.55"}
    t = _trader_with_client(_FakeClient(post_resp=post, order_rec=rec))
    assert t.execute_buy("TOK", 11.0, 0.40) is None


# ── 2: daily-loss halt keys on SETTLEMENT day ────────────────────────────────
def _seed(db, pnl, ts, settle, order_id="L1"):
    db.add(Trade(market_ticker="m", platform="polymarket", market_type="weather",
                 direction="no", entry_price=0.5, size=2.0, order_id=order_id,
                 settled=True, pnl=pnl, timestamp=ts, settlement_time=settle))
    db.commit()


def test_daily_loss_counts_yesterday_entry_settled_today():
    Session = _mem_sessionmaker()
    db = Session()
    yesterday = datetime.utcnow() - timedelta(days=1)
    now = datetime.utcnow()
    _seed(db, pnl=-4.0, ts=yesterday, settle=now)      # entered yesterday, settled today
    assert _live_daily_realized_loss(db) >= 4.0


def test_daily_loss_ignores_settled_yesterday():
    Session = _mem_sessionmaker()
    db = Session()
    yesterday = datetime.utcnow() - timedelta(days=1)
    _seed(db, pnl=-9.0, ts=yesterday, settle=yesterday)  # settled yesterday → not today's halt
    assert _live_daily_realized_loss(db) == 0.0


# ── 1c CRITICAL: a booked fill survives a later candidate raising ────────────
def _wx_signal(market_id):
    market = types.SimpleNamespace(
        platform="polymarket", market_id=market_id, slug=f"slug-{market_id}",
        city_name="NYC", city_key="nyc", metric="high", direction="above",
        threshold_f=90.0, yes_price=0.40, no_price=0.60,
        token_id_yes="Y" + market_id, token_id_no="N" + market_id,
    )
    return types.SimpleNamespace(
        market=market, direction="no", edge=0.20, market_probability=0.40,
        model_probability=0.60, suggested_size=50.0, passes_threshold=True,
    )


def test_booked_fill_survives_later_candidate_error(monkeypatch):
    Session = _mem_sessionmaker()
    seed = Session()
    seed.add(BotState(id=1, bankroll=1000.0, is_running=True, total_trades=0))
    seed.commit()
    seed.close()

    # In-memory DB for the job.
    monkeypatch.setattr(scheduler, "SessionLocal", Session)

    s1, s2 = _wx_signal("M1"), _wx_signal("M2")

    async def fake_scan():
        return [s1, s2]

    import backend.core.weather_signals as ws
    monkeypatch.setattr(ws, "scan_for_weather_signals", fake_scan)

    # Candidate M1 → confirmed fill; candidate M2 → raises mid-processing.
    def fake_resolve(signal, trade_size, entry_price, db, settings_, factory, **kwargs):
        if signal.market.market_id == "M1":
            return LiveDecision("fill", 0.42, 1.68, "F1")
        raise RuntimeError("boom on candidate 2")

    monkeypatch.setattr(scheduler, "resolve_weather_live", fake_resolve)

    asyncio.run(scheduler.weather_scan_and_trade_job())

    check = Session()
    rows = check.query(Trade).all()
    # M1's booked live fill persisted despite M2 raising afterward.
    assert len(rows) == 1
    assert rows[0].market_ticker == "M1"
    assert rows[0].order_id == "F1"
    assert rows[0].token_id == "NM1"       # NO-side token stored on the live fill
    check.close()
