"""Resting-order signal_id link + lifecycle (2026-07-21 coverage pass).
_create_resting_order now stamps the originating DB signal id for graduation-ledger
traceability. Pure in-memory DB; no network."""
import os, sys
from types import SimpleNamespace
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import backend.models.database as db_mod
from backend.models.database import PaperRestingOrder
import backend.core.scheduler as sched


def _db():
    eng = create_engine("sqlite:///:memory:")
    db_mod.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _sig(ticker="mX", direction="no"):
    mkt = SimpleNamespace(market_id=ticker, slug="evt", condition_id="cid",
                          platform="polymarket", city_name="Tokyo", target_date=date(2099, 1, 1))
    return SimpleNamespace(market=mkt, direction=direction,
                           model_probability=0.05, market_probability=0.42, edge=0.25)


def test_rest_stamped_with_signal_id():
    db = _db()
    sched._create_resting_order(db, _sig("mA"), 0.55, 20.0, signal_id=193514)
    ro = db.query(PaperRestingOrder).filter_by(market_ticker="mA").first()
    assert ro is not None
    assert ro.signal_id == 193514


def test_rest_signal_id_none_when_not_supplied():
    db = _db()
    sched._create_resting_order(db, _sig("mB"), 0.55, 20.0)   # no signal_id
    ro = db.query(PaperRestingOrder).filter_by(market_ticker="mB").first()
    assert ro is not None and ro.signal_id is None


def test_rest_dedupe_one_per_market_direction():
    db = _db()
    sched._create_resting_order(db, _sig("mC"), 0.55, 20.0, signal_id=1)
    sched._create_resting_order(db, _sig("mC"), 0.55, 15.0, signal_id=2)  # dup -> ignored
    rows = db.query(PaperRestingOrder).filter_by(market_ticker="mC").all()
    assert len(rows) == 1
    assert rows[0].signal_id == 1     # first one kept
