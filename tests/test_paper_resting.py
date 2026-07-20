"""Paper resting-order (take+rest hybrid) tests — 2026-07-20.
Covers: rest created (+ dedupe), later fill, partial fill, no-fill-above-cap,
expiry at settlement approach. Pure DB + monkeypatched book (no network)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.models.database as db_mod
from backend.models.database import PaperRestingOrder, Trade, BotState
import backend.core.scheduler as sched
import backend.core.execution_realism as exr


def _mem_db():
    eng = create_engine("sqlite:///:memory:")
    db_mod.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _enable(monkeypatch):
    monkeypatch.setattr(sched.settings, "WEATHER_PAPER_REALISTIC_FILLS", True)


def _mock_book(monkeypatch, book):
    monkeypatch.setattr(exr, "resolve_token_id", lambda cond, direction, client=None: "tok")
    monkeypatch.setattr(exr, "fetch_book", lambda tok, client=None: book)


def _state(db, bankroll=1000.0):
    s = BotState(bankroll=bankroll, is_running=True, total_trades=0)
    db.add(s); db.commit(); return s


def _rest(db, **kw):
    d = dict(market_ticker="m1", platform="polymarket", event_slug="e1", condition_id="c1",
             direction="no", rest_price=0.60, remaining_shares=20.0, city_name="Jeddah",
             target_date=(datetime.utcnow().date() + timedelta(days=2)).isoformat(),
             model_probability=0.9, market_price_at_entry=0.4, edge_at_entry=0.3, status="resting")
    d.update(kw)
    ro = PaperRestingOrder(**d); db.add(ro); db.commit(); return ro


# 1. rest created + dedupe
def test_create_resting_order_and_dedupe():
    db = _mem_db()
    market = SimpleNamespace(market_id="m1", platform="polymarket", slug="e1", condition_id="c1",
                             city_name="Jeddah", target_date=datetime.utcnow().date() + timedelta(days=2))
    signal = SimpleNamespace(market=market, direction="no", signal_id=None,
                             model_probability=0.9, market_probability=0.4, edge=0.3)
    sched._create_resting_order(db, signal, rest_price=0.60, remaining_shares=20.0); db.commit()
    rows = db.query(PaperRestingOrder).all()
    assert len(rows) == 1 and rows[0].status == "resting" and rows[0].remaining_shares == 20.0
    sched._create_resting_order(db, signal, rest_price=0.60, remaining_shares=20.0); db.commit()
    assert db.query(PaperRestingOrder).count() == 1   # deduped


# 2. later fill — best_ask crossed down to <= rest price
def test_rest_later_fill(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db); _rest(db)
    _mock_book(monkeypatch, {"asks": [{"price": 0.58, "size": 100}]})
    sched.process_paper_resting_orders(db, state)
    trades = db.query(Trade).all()
    assert len(trades) == 1 and trades[0].fill_type == "rest_fill"
    assert db.query(PaperRestingOrder).first().status == "filled"
    assert state.total_trades == 1 and state.bankroll < 1000.0


# 3. partial fill — ask size < remaining
def test_rest_partial_fill(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db); _rest(db, remaining_shares=20.0)
    _mock_book(monkeypatch, {"asks": [{"price": 0.58, "size": 5}]})
    sched.process_paper_resting_orders(db, state)
    ro = db.query(PaperRestingOrder).first()
    assert ro.status == "resting"                       # still open
    assert ro.remaining_shares == pytest.approx(15.0)   # 20 - 5
    assert db.query(Trade).count() == 1


# 4. no fill — best_ask still above the rest price
def test_rest_no_fill_above_cap(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db); _rest(db)
    _mock_book(monkeypatch, {"asks": [{"price": 0.65, "size": 100}]})
    sched.process_paper_resting_orders(db, state)
    assert db.query(Trade).count() == 0
    assert db.query(PaperRestingOrder).first().status == "resting"


# 5. expiry at settlement approach (target day reached) — no trade even if fillable
def test_rest_expiry(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    _rest(db, target_date=(datetime.utcnow().date() - timedelta(days=1)).isoformat())
    _mock_book(monkeypatch, {"asks": [{"price": 0.10, "size": 100}]})   # would fill, but expires first
    sched.process_paper_resting_orders(db, state)
    assert db.query(PaperRestingOrder).first().status == "expired"
    assert db.query(Trade).count() == 0


# 6. book_unavailable — leave resting (no fill, no expiry)
def test_rest_book_unavailable_stays(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db); _rest(db)
    monkeypatch.setattr(exr, "resolve_token_id", lambda cond, direction, client=None: "tok")
    monkeypatch.setattr(exr, "fetch_book", lambda tok, client=None: None)   # fetch failed
    sched.process_paper_resting_orders(db, state)
    assert db.query(PaperRestingOrder).first().status == "resting"
    assert db.query(Trade).count() == 0
