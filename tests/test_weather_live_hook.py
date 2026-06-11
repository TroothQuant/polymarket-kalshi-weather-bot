"""G2-2 tests: the flag-gated live hook (resolve_weather_live). execute_buy is
always MOCKED — no network, no real order. Covers: flag-off=paper, Kalshi/
non-polymarket can't go live, missing-token skip, confirmed-fill economics +
per-trade cap, no-fill = no row, and the daily realized-loss kill-switch.
"""
import types
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade
from backend.core.scheduler import resolve_weather_live


# ── fixtures ──────────────────────────────────────────────────────────────────
def _signal(direction="yes", platform="polymarket", token_yes="YESTOK", token_no="NOTOK"):
    market = types.SimpleNamespace(
        platform=platform, market_id="2400000", slug="slug",
        token_id_yes=token_yes, token_id_no=token_no,
        model_probability=0.6, threshold_f=90.0, direction="above",
        metric="high", city_name="Chicago",
    )
    return types.SimpleNamespace(
        market=market, direction=direction, edge=0.20,
        market_probability=0.40, model_probability=0.60, suggested_size=50.0,
    )


def _settings(live=True, cap=2.0, stop=10.0):
    return types.SimpleNamespace(
        WEATHER_LIVE_TRADING=live,
        WEATHER_LIVE_MAX_TRADE_USD=cap,
        WEATHER_LIVE_DAILY_LOSS_STOP_USD=stop,
    )


class _MockTrader:
    """Records execute_buy calls; returns a preset fill (or None for no-fill)."""
    def __init__(self, fill):
        self.fill = fill
        self.calls = []

    def execute_buy(self, token_id, size_usd, market_price):
        self.calls.append((token_id, size_usd, market_price))
        return self.fill


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _seed_loss(db, pnl, order_id="OLD", when=None):
    db.add(Trade(market_ticker="m", platform="polymarket", market_type="weather",
                 direction="yes", entry_price=0.4, size=2.0, order_id=order_id,
                 settled=True, pnl=pnl, timestamp=when or datetime.utcnow()))
    db.commit()


_FILL = {"order_id": "OID1", "fill_price": 0.42, "shares": 4.0, "cost": 1.68}


# ── flag-off / defense-in-depth ──────────────────────────────────────────────
def test_flag_off_is_paper():
    d = resolve_weather_live(_signal(), 50.0, 0.40, None, _settings(live=False), None)
    assert d.action == "paper"
    assert d.size == 50.0 and d.entry_price == 0.40 and d.order_id is None


def test_kalshi_can_never_go_live():
    # platform != polymarket → paper even with the flag ON and no db/factory used.
    d = resolve_weather_live(_signal(platform="kalshi"), 50.0, 0.40, None, _settings(live=True), None)
    assert d.action == "paper"


# ── guards ───────────────────────────────────────────────────────────────────
def test_missing_token_id_skips():
    t = _MockTrader(_FILL)
    d = resolve_weather_live(_signal(token_yes="", direction="yes"), 50.0, 0.40,
                             _db(), _settings(), lambda: t)
    assert d.action == "skip"
    assert t.calls == []  # never even attempted an order


# ── fill economics + cap ─────────────────────────────────────────────────────
def test_confirmed_fill_writes_actual_economics_and_caps_size():
    t = _MockTrader(_FILL)
    d = resolve_weather_live(_signal(), 50.0, 0.40, _db(), _settings(cap=2.0), lambda: t)
    assert d.action == "fill"
    assert d.entry_price == 0.42      # ACTUAL fill price, not the 0.40 quote
    assert d.size == 1.68             # ACTUAL cost
    assert d.order_id == "OID1"
    assert t.calls[0][1] == 2.0       # size passed to execute_buy capped at MAX_TRADE_USD


def test_no_fill_writes_no_row():
    t = _MockTrader(None)
    d = resolve_weather_live(_signal(), 50.0, 0.40, _db(), _settings(), lambda: t)
    assert d.action == "skip"
    assert len(t.calls) == 1          # it tried, got no fill


# ── kill-switch ──────────────────────────────────────────────────────────────
def test_killswitch_halts_at_or_above_stop():
    db = _db()
    _seed_loss(db, pnl=-12.0)         # today's live loss 12 >= stop 10
    d = resolve_weather_live(_signal(), 50.0, 0.40, db, _settings(stop=10.0),
                             lambda: _MockTrader(_FILL))
    assert d.action == "halt"


def test_killswitch_allows_below_stop():
    db = _db()
    _seed_loss(db, pnl=-5.0)          # 5 < 10 → proceeds to fill
    d = resolve_weather_live(_signal(), 50.0, 0.40, db, _settings(stop=10.0),
                             lambda: _MockTrader(_FILL))
    assert d.action == "fill"


def test_killswitch_ignores_paper_losses():
    db = _db()
    _seed_loss(db, pnl=-99.0, order_id=None)  # PAPER loss (order_id NULL) — must not count
    d = resolve_weather_live(_signal(), 50.0, 0.40, db, _settings(stop=10.0),
                             lambda: _MockTrader(_FILL))
    assert d.action == "fill"
