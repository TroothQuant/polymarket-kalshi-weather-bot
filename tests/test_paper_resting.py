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


# ── Rest-lifecycle fixes 2026-07-22 ──────────────────────────────────────────
# 7. _rest_local_day_passed: keyed to the CITY-LOCAL day, not UTC. Qingdao is
# UTC+8 (lon 120): its July-21 market day runs 2026-07-20 16:00 → 07-21 16:00 UTC.
def test_local_day_expiry_boundary():
    # Mid-market-day (07-21 02:00 UTC = 10:00 local): rest must LIVE. This is
    # the exact condition that killed rests 2-32 under the old UTC rule.
    assert not sched._rest_local_day_passed(
        "Qingdao", "2026-07-21", datetime(2026, 7, 21, 2, 0))
    # Just before local midnight (15:59 UTC = 23:59 local): still alive.
    assert not sched._rest_local_day_passed(
        "Qingdao", "2026-07-21", datetime(2026, 7, 21, 15, 59))
    # Local day over (16:01 UTC = 00:01 local 07-22): expired.
    assert sched._rest_local_day_passed(
        "Qingdao", "2026-07-21", datetime(2026, 7, 21, 16, 1))
    # Western city (Los Angeles, UTC-8): 07-22 06:00 UTC is still 07-21 22:00
    # local — a UTC-keyed rule would have expired this a full 6 hours early.
    assert not sched._rest_local_day_passed(
        "Los Angeles", "2026-07-21", datetime(2026, 7, 22, 6, 0))
    assert sched._rest_local_day_passed(
        "Los Angeles", "2026-07-21", datetime(2026, 7, 22, 8, 1))
    # Unknown city: safe fallback — only expires after target + 1 full UTC day.
    assert not sched._rest_local_day_passed(
        "Atlantis", "2026-07-21", datetime(2026, 7, 22, 23, 0))
    assert sched._rest_local_day_passed(
        "Atlantis", "2026-07-21", datetime(2026, 7, 23, 0, 1))
    # NULL / garbage target_date: never expires on this rule.
    assert not sched._rest_local_day_passed("Qingdao", None, datetime(2026, 7, 23))
    assert not sched._rest_local_day_passed("Qingdao", "not-a-date", datetime(2026, 7, 23))


# 8. same-day-target rest LIVES through its market day and can still fill
# (the old rule expired it at the first scan pass — the 7/21 churn bug).
def test_same_day_rest_fills_not_expires(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    _rest(db, target_date=datetime.utcnow().date().isoformat())
    monkeypatch.setattr(sched, "_rest_local_day_passed", lambda *a, **k: False)
    _mock_book(monkeypatch, {"asks": [{"price": 0.58, "size": 100}]})
    sched.process_paper_resting_orders(db, state)
    ro = db.query(PaperRestingOrder).first()
    assert ro.status == "filled"          # filled, NOT expired
    assert db.query(Trade).count() == 1


# 9. dedup gap (ii): a trade from a DIFFERENT signal already holds the ticker →
# rest is cancelled_dupe, never filled (the #96-vs-#33 double-position gap).
def test_rest_fill_dedup_cancels(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    _rest(db, signal_id=101)
    db.add(Trade(market_ticker="m1", platform="polymarket", event_slug="e1",
                 market_type="weather", direction="no", entry_price=0.55, size=10.0,
                 fill_type="instant", signal_id=202))   # different signal
    db.commit()
    _mock_book(monkeypatch, {"asks": [{"price": 0.10, "size": 100}]})   # fillable!
    sched.process_paper_resting_orders(db, state)
    assert db.query(PaperRestingOrder).first().status == "cancelled_dupe"
    assert db.query(Trade).count() == 1   # no second position


# 10. NOT a dupe: the remainder rest of a partial instant fill (same signal_id)
# must still be allowed to complete — that's the designed take+rest flow.
def test_rest_fill_same_signal_remainder_allowed(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    _rest(db, signal_id=101)
    db.add(Trade(market_ticker="m1", platform="polymarket", event_slug="e1",
                 market_type="weather", direction="no", entry_price=0.55, size=10.0,
                 fill_type="instant", signal_id=101))   # SAME signal — partial take
    db.commit()
    _mock_book(monkeypatch, {"asks": [{"price": 0.58, "size": 100}]})
    sched.process_paper_resting_orders(db, state)
    assert db.query(PaperRestingOrder).first().status == "filled"
    assert db.query(Trade).count() == 2   # take row + rest_fill completion row


# 11. legacy rest (signal_id NULL): timestamp fallback — a trade opened >2 min
# after the rest was created came from a later scan → cancelled_dupe.
def test_legacy_rest_timestamp_dedup(monkeypatch):
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    ro = _rest(db, signal_id=None)
    ro.created_at = datetime.utcnow() - timedelta(hours=12)
    db.add(Trade(market_ticker="m1", platform="polymarket", event_slug="e1",
                 market_type="weather", direction="no", entry_price=0.55, size=10.0,
                 fill_type="instant", signal_id=None,
                 timestamp=datetime.utcnow() - timedelta(hours=1)))
    db.commit()
    _mock_book(monkeypatch, {"asks": [{"price": 0.10, "size": 100}]})
    sched.process_paper_resting_orders(db, state)
    assert db.query(PaperRestingOrder).first().status == "cancelled_dupe"
    assert db.query(Trade).count() == 1


# 12. dedup gap (i): an instant entry supersedes a STALE open rest on the same
# ticker — end-to-end through weather_scan_and_trade_job.
def test_instant_entry_cancels_stale_rest(monkeypatch):
    import asyncio
    _enable(monkeypatch); db = _mem_db(); state = _state(db)
    stale = _rest(db, rest_price=0.40, remaining_shares=62.5,
                  target_date=(datetime.utcnow().date() + timedelta(days=1)).isoformat())
    market = SimpleNamespace(
        market_id="m1", platform="polymarket", slug="e1", condition_id="c1",
        city_name="Jeddah", yes_price=0.45, no_price=0.55,
        metric="high", direction="above", threshold_f=104.0,
        target_date=datetime.utcnow().date() + timedelta(days=1))
    signal = SimpleNamespace(
        market=market, direction="no", passes_threshold=True, suggested_size=25.0,
        model_probability=0.10, market_probability=0.45, edge=0.35, signal_id=None)

    async def _fake_scan():
        return [signal]
    import backend.core.weather_signals as ws
    monkeypatch.setattr(ws, "scan_for_weather_signals", _fake_scan)
    monkeypatch.setattr(sched, "SessionLocal", lambda: db)
    _closed = {"v": False}
    monkeypatch.setattr(db, "close", lambda: _closed.__setitem__("v", True))
    # instant fill available at 0.55 (well above the stale 0.40 rest)
    _mock_book(monkeypatch, {"asks": [{"price": 0.55, "size": 1000}]})

    asyncio.run(sched.weather_scan_and_trade_job())

    db2 = db
    assert db2.query(PaperRestingOrder).get(stale.id).status == "cancelled_replaced"
    t = db2.query(Trade).filter(Trade.fill_type == "instant").all()
    assert len(t) == 1 and t[0].market_ticker == "m1"
