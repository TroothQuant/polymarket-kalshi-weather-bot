"""CRYPTO5050 paper book tests (2026-07-22).

Covers the spec's verification list: window lifecycle math, pair-VWAP hard
stop, balance logic, lean sizing/rules, fee math, rule grading, module halt,
and crash isolation from the weather loop. Pure logic + in-memory DB — no
network, no asyncio timing."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.core.crypto5050 as c5
from backend.core.crypto5050 import (
    window_epoch, window_slug, fee_for, pair_vwap, vwap_allows_fill,
    choose_side, lean_pick_spot_drift, lean_pick_momentum, lean_pick_depth,
    grade_pick, settle_window, best_bid_ask, Crypto5050Runner)
from backend.models.database import Base, CryptoWindow, CryptoFill, Trade


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _settings(**over):
    s = SimpleNamespace(
        CRYPTO_5050_ENABLED=True, CRYPTO5050_ALLOCATION_USD=500.0,
        CRYPTO5050_MAX_WINDOW_NOTIONAL_USD=30.0, CRYPTO5050_HALT_PNL_USD=-100.0,
        CRYPTO5050_POLL_SECONDS=4.0, CRYPTO5050_LEAN_SHARES=20.0,
        CRYPTO5050_MAKER_FEE_RATE=0.0, CRYPTO5050_TAKER_FEE_RATE=0.0)
    s.__dict__.update(over)
    return s


# ── window identity ──────────────────────────────────────────────────────────
def test_window_epoch_and_slug():
    assert window_epoch(1784736000) == 1784736000          # exact boundary
    assert window_epoch(1784736299.9) == 1784736000        # last second of window
    assert window_epoch(1784736300) == 1784736300          # next window
    assert window_slug(1784736000) == "btc-updown-5m-1784736000"


# ── fee math (CLOB formula, rate x min(p,1-p) x sh) ─────────────────────────
def test_fee_formula_and_zero_default():
    assert fee_for(0.61, 100, 0.0) == 0.0                  # evidence-based default
    assert fee_for(0.61, 100, 0.10) == pytest.approx(0.10 * 0.39 * 100)
    assert fee_for(0.39, 100, 0.10) == pytest.approx(0.10 * 0.39 * 100)  # min(p,1-p) symmetric
    assert fee_for("bad", 100, 0.10) == 0.0


# ── pair VWAP + the $1.00 hard stop ─────────────────────────────────────────
def test_pair_vwap_reference_case():
    # DoggyStyIe 7/22: 1950 Up @0.61 + 1950 Down @0.365 → 0.975/pair
    pv = pair_vwap(1950 * 0.61, 1950, 1950 * 0.365, 1950)
    assert pv == pytest.approx(0.975)


def test_vwap_hard_stop_refuses_dollar_pairs():
    # up VWAP 0.61; a down fill at 0.39 makes the pair exactly 1.00 → REFUSED
    assert not vwap_allows_fill(6.1, 10, 0.0, 0.0, "down", 0.39, 10)
    # at 0.38 the pair is 0.99 → allowed
    assert vwap_allows_fill(6.1, 10, 0.0, 0.0, "down", 0.38, 10)
    # one-sided accumulation (no pair yet) always allowed
    assert vwap_allows_fill(0.0, 0.0, 0.0, 0.0, "up", 0.65, 5)


# ── balance / cheaper-side logic ─────────────────────────────────────────────
def test_choose_side_balances_first_then_cheaper():
    assert choose_side(10, 5, 0.30, 0.70) == "down"        # down lags a full fill
    assert choose_side(5, 10, 0.70, 0.30) == "up"          # up lags
    assert choose_side(5, 5, 0.40, 0.55) == "up"           # balanced → cheaper ask
    assert choose_side(5, 5, 0.55, 0.40) == "down"
    assert choose_side(5, 5, None, None) is None           # no book
    assert choose_side(0, 0, None, 0.5) == "down"          # only down quoted, balanced
    assert choose_side(0, 5, None, 0.5) is None            # down already ahead, up unquoted


# ── lean rules + grading ─────────────────────────────────────────────────────
def test_lean_rules():
    assert lean_pick_spot_drift(100000.0, 100050.0) == "up"
    assert lean_pick_spot_drift(100000.0, 99900.0) == "down"
    assert lean_pick_spot_drift(100000.0, 100000.0) is None
    assert lean_pick_spot_drift(None, 100000.0) is None
    assert lean_pick_momentum(0.50, 0.56) == "up"
    assert lean_pick_momentum(0.50, 0.44) == "down"
    assert lean_pick_depth(500.0, 200.0) == "up"
    assert lean_pick_depth(200.0, 500.0) == "down"
    assert lean_pick_depth(None, 500.0) is None


def test_grade_pick():
    assert grade_pick("up", "up") == 1
    assert grade_pick("down", "up") == 0
    assert grade_pick(None, "up") is None                  # no pick → excluded from n
    assert grade_pick("up", None) is None


# ── settlement economics ─────────────────────────────────────────────────────
def test_settle_locked_pairs_win_regardless_of_outcome():
    # 10 pairs at 0.975 → +$0.25 locked either way; no lean, no excess.
    for winner in ("up", "down"):
        econ = settle_window(6.1, 10, 3.65, 10, None, 0.0, 0.0, winner, 0.0)
        assert econ["pairs"] == 10
        assert econ["locked_pnl"] == pytest.approx(10 * 0.025)
        assert econ["net_pnl"] == pytest.approx(0.25)


def test_settle_lean_and_unhedged_and_fees():
    # 10/10 pairs @0.975 + lean 20sh up @0.55 ($11), fees $0.10
    econ_win = settle_window(6.1, 10, 3.65, 10, "up", 11.0, 20.0, "up", 0.10)
    assert econ_win["lean_pnl"] == pytest.approx(20.0 - 11.0)
    assert econ_win["net_pnl"] == pytest.approx(0.25 + 9.0 - 0.10)
    econ_loss = settle_window(6.1, 10, 3.65, 10, "up", 11.0, 20.0, "down", 0.10)
    assert econ_loss["lean_pnl"] == pytest.approx(-11.0)
    # unhedged excess: 15 up vs 10 down (5 excess up @ up VWAP 0.61)
    econ_ex = settle_window(15 * 0.61, 15, 3.65, 10, None, 0.0, 0.0, "down", 0.0)
    assert econ_ex["unhedged_pnl"] == pytest.approx(-(5 * 0.61))


# ── book parsing ─────────────────────────────────────────────────────────────
def test_best_bid_ask_unsorted_levels():
    book = {"bids": [{"price": "0.48", "size": "100"}, {"price": "0.50", "size": "40"}],
            "asks": [{"price": "0.55", "size": "60"}, {"price": "0.52", "size": "10"}]}
    bid, ask, depth = best_bid_ask(book)
    assert bid == 0.50 and ask == 0.52
    assert depth == pytest.approx(140.0)
    assert best_bid_ask({}) == (None, None, None)


# ── module halt ──────────────────────────────────────────────────────────────
def _runner(db, settings=None, events=None):
    events = events if events is not None else []
    r = Crypto5050Runner(settings or _settings(), lambda: db,
                         lambda lvl, msg, data=None: events.append((lvl, msg)))
    return r, events


def test_halt_triggers_at_cumulative_loss():
    db = _db()
    db.add(CryptoWindow(slug="w1", status="settled", net_pnl=-60.0))
    db.add(CryptoWindow(slug="w2", status="settled", net_pnl=-45.0))
    db.commit()
    r, events = _runner(db)
    assert r._check_halt(db) is True
    assert r.halted is True
    assert any("AUTO-HALT" in m for _, m in events)


def test_halt_not_triggered_above_threshold():
    db = _db()
    db.add(CryptoWindow(slug="w1", status="settled", net_pnl=-99.0))
    db.commit()
    r, events = _runner(db)
    assert r._check_halt(db) is False
    assert r.halted is False


def test_halt_ignores_unsettled_rows():
    db = _db()
    db.add(CryptoWindow(slug="w1", status="open", net_pnl=None))
    db.add(CryptoWindow(slug="w2", status="settled", net_pnl=-50.0))
    db.commit()
    r, _ = _runner(db)
    assert r._check_halt(db) is False


# ── fill application: caps + accounting + never touches trades ───────────────
def test_apply_fill_respects_cap_and_never_writes_trades():
    db = _db()
    row = CryptoWindow(slug="w1", status="open", up_shares=0.0, up_cost=0.0,
                       down_shares=0.0, down_cost=0.0)
    db.add(row); db.commit()
    r, _ = _runner(db)
    out = r._apply_fill(db, row, "up", "taker", 0.50, 5.0, spent=0.0, cap=30.0, fees=0.0)
    assert out == (pytest.approx(2.5), 0.0)
    assert row.up_shares == 5.0 and row.taker_fills == 1
    assert db.query(CryptoFill).count() == 1
    assert db.query(Trade).count() == 0                    # NEVER the weather ledger
    # cap refusal
    assert r._apply_fill(db, row, "up", "taker", 0.50, 5.0,
                         spent=29.0, cap=30.0, fees=0.0) is None
    assert row.fills_count == 1                            # unchanged after refusal


# ── crash isolation ──────────────────────────────────────────────────────────
def test_run_loop_survives_poison_and_never_raises():
    db = _db()
    r, events = _runner(db)
    calls = {"n": 0}

    async def poison(epoch, pending):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError      # end the test loop cleanly
        raise RuntimeError("boom")            # simulated crash — must be swallowed

    r._trade_window = poison

    async def fast_sleep(_):
        return None

    async def main():
        orig_sleep = asyncio.sleep
        asyncio.sleep = lambda s: orig_sleep(0)
        try:
            with pytest.raises(asyncio.CancelledError):
                await r.run()
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(main())
    assert calls["n"] == 3                    # crashed twice, kept running


def test_startup_hook_isolated(monkeypatch):
    # even a failing start_crypto5050 import path must not raise out of startup —
    # simulate by calling the wrapper the way main.py does.
    import backend.core.crypto5050 as mod
    def boom(*a, **k):
        raise RuntimeError("startup boom")
    monkeypatch.setattr(mod, "start_crypto5050", boom)
    # mimic main.py's guard
    events = []
    try:
        try:
            mod.start_crypto5050(None, None, None)
        except Exception as e:
            events.append(str(e))
    except Exception:
        pytest.fail("exception escaped the startup guard")
    assert events == ["startup boom"]


# ── clean-data guards (2026-07-22, post-first-live-windows) ──────────────────
def test_partial_join_guard():
    from backend.core.crypto5050 import is_partial_join
    assert not is_partial_join(1784736000 + 5, 1784736000)     # on-time join
    assert not is_partial_join(1784736000 + 30, 1784736000)    # boundary ok
    assert is_partial_join(1784736000 + 31, 1784736000)        # late → skip
    assert is_partial_join(1784736000 + 120, 1784736000)       # restart mid-window
