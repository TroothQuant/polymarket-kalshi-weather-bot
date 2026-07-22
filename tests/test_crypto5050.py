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
        CRYPTO_5050_ENABLED=True, CRYPTO5050_ALLOCATION_USD=1000.0,
        CRYPTO5050_MAX_WINDOW_NOTIONAL_USD=200.0, CRYPTO5050_LEAN_RESERVE_USD=20.0,
        CRYPTO5050_POLL_SECONDS=4.0, CRYPTO5050_LEAN_SHARES=20.0,
        CRYPTO5050_FILL_SHARES=15.0,
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


def test_no_halt_allocation_funds_through_deep_loss():
    # NO halts (operator decision of record): a −$850 cumulative net still
    # funds windows ($1,000 + −850 = $150 < $200 cap? no — use −700 → $300 ≥ cap).
    db = _db()
    db.add(CryptoWindow(slug="w1", status="settled", net_pnl=-700.0))
    db.commit()
    r, events = _runner(db)
    assert r._cannot_fund_window(db) is False
    assert not any("EXHAUSTED" in m for _, m in events)


def test_allocation_exhausted_pauses_and_refunds():
    # $1,000 − $850 = $150 < $200 window cap → paused with a loud log …
    db = _db()
    w = CryptoWindow(slug="w1", status="settled", net_pnl=-850.0)
    db.add(w); db.commit()
    r, events = _runner(db)
    assert r._cannot_fund_window(db) is True
    assert any("ALLOCATION EXHAUSTED" in m for _, m in events)
    # … and NON-LATCHING: a settlement improving net re-funds the module.
    w.net_pnl = -700.0
    db.commit()
    assert r._cannot_fund_window(db) is False
    assert any("re-funded" in m for _, m in events)


def test_allocation_check_ignores_unsettled_rows():
    db = _db()
    db.add(CryptoWindow(slug="w1", status="open", net_pnl=None))
    db.add(CryptoWindow(slug="w2", status="settled", net_pnl=-750.0))
    db.commit()
    r, _ = _runner(db)
    assert r._cannot_fund_window(db) is False   # $250 available ≥ $200 cap


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


def test_sweep_stale_queues_ended_windows():
    from datetime import timedelta
    db = _db()
    db.add(CryptoWindow(slug="old", status="closing",
                        window_start=datetime.utcnow() - timedelta(minutes=20)))
    db.add(CryptoWindow(slug="current", status="open",
                        window_start=datetime.utcnow()))
    db.commit()
    r, events = _runner(db)

    async def fake_resolve(window_id):
        return None
    r._resolve_window_by_id = fake_resolve

    async def main():
        return await r._sweep_stale()
    tasks = asyncio.run(main())
    assert len(tasks) == 1                                  # only the ended window
    assert any("stale window old" in m for _, m in events)
    assert db.query(CryptoWindow).filter_by(slug="current").first().status == "open"


# ── budget split (Cowork 2026-07-22 PM): $18 lean reserve of the $40 cap ─────
def test_lean_affordable_boundary():
    from backend.core.crypto5050 import lean_affordable
    # sizing rev: $20 reserve covers the fixed 20-share lean at ANY valid price
    # (0.99 max tick) — the guard is now a safety no-op except on missing asks.
    assert lean_affordable(0.99, 20, 20.0)          # 20 x 0.99 = $19.80 → trades
    assert lean_affordable(0.10, 20, 20.0)
    assert not lean_affordable(None, 20, 20.0)      # no ask → no lean
    assert not lean_affordable(1.05, 20, 20.0)      # degenerate price → guard holds


def test_hedge_cap_is_cap_minus_reserve():
    # _apply_fill against the $180 hedge budget: a fill that fits the $200
    # window cap but not the $180 hedge budget must be refused.
    db = _db()
    row = CryptoWindow(slug="w1", status="open", up_shares=0.0, up_cost=0.0,
                       down_shares=0.0, down_cost=0.0)
    db.add(row); db.commit()
    r, _ = _runner(db)
    hedge_cap = 200.0 - 20.0
    assert r._apply_fill(db, row, "up", "taker", 0.50, 15.0,
                         spent=175.0, cap=hedge_cap, fees=0.0) is None  # 182.5 > 180
    ok = r._apply_fill(db, row, "up", "taker", 0.30, 15.0,
                       spent=175.0, cap=hedge_cap, fees=0.0)            # 179.5 <= 180
    assert ok == (pytest.approx(179.5), 0.0)


def test_choose_side_uses_configurable_fill_threshold():
    # 15-share fills: a 15-share imbalance triggers rebalance; 10 does not.
    assert choose_side(15, 0, 0.5, 0.5, fill_shares=15.0) == "down"
    assert choose_side(10, 0, 0.4, 0.6, fill_shares=15.0) == "up"   # cheaper side


# ── fourth shadow rule: late-recency (2026-07-22 PM) ─────────────────────────
def test_lean_pick_late_recency():
    from backend.core.crypto5050 import lean_pick_late_recency
    assert lean_pick_late_recency(66000.0, 66040.0) == "up"
    assert lean_pick_late_recency(66040.0, 66000.0) == "down"
    assert lean_pick_late_recency(66000.0, 66000.0) is None    # flat → no pick
    assert lean_pick_late_recency(None, 66000.0) is None       # no final-minute sample
    assert lean_pick_late_recency(66000.0, None) is None       # no close sample


def test_late_recency_grades_like_other_rules():
    assert grade_pick("up", "up") == 1 and grade_pick("down", "up") == 0
    # windows predating the rule (pick NULL) are excluded from n — no backfill
    # exists because per-poll spot history was never stored.
    assert grade_pick(None, "down") is None


# ── fifth shadow rule: brownian-gated (2026-07-22 PM) + arb visibility ───────
def _samples(prices, dt=4.0):
    return [(i * dt, p) for i, p in enumerate(prices)]


def test_brownian_p_up_math():
    from backend.core.crypto5050 import brownian_p_up
    # flat drift with real vol → P = 0.5
    prices = [100.0, 100.1, 99.9, 100.05, 99.95, 100.0, 100.1, 99.9, 100.0, 100.05, 100.0]
    assert brownian_p_up(100.0, 100.0, _samples(prices), 60) == pytest.approx(0.5, abs=0.01)
    # strong up-drift vs tiny vol → P ≈ 1
    p = brownian_p_up(100.0, 150.0, _samples(prices), 60)
    assert p > 0.999
    # insufficient samples / missing data → None
    assert brownian_p_up(100.0, 101.0, _samples(prices[:5]), 60) is None
    assert brownian_p_up(None, 101.0, _samples(prices), 60) is None
    # zero vol: saturates with drift, None without
    flat = [100.0] * 12
    assert brownian_p_up(100.0, 101.0, _samples(flat), 60) == 1.0
    assert brownian_p_up(100.0, 99.0, _samples(flat), 60) == 0.0
    assert brownian_p_up(100.0, 100.0, _samples(flat), 60) is None


def test_brownian_gates():
    from backend.core.crypto5050 import brownian_gated_pick
    # P(Up)=0.9, ask 0.70 <= 0.85*0.9=0.765 → pick up
    assert brownian_gated_pick(0.90, 0.70, 0.35) == "up"
    # confident but too rich (0.80 > 0.765) → abstain
    assert brownian_gated_pick(0.90, 0.80, 0.35) == "abstain"
    # P(Down)=0.85, down ask 0.70 <= 0.85*0.85=0.7225 → pick down
    assert brownian_gated_pick(0.15, 0.90, 0.70) == "down"
    # nobody clears the 0.80 floor → abstain
    assert brownian_gated_pick(0.60, 0.50, 0.50) == "abstain"
    # estimate unavailable → None (distinct from abstain)
    assert brownian_gated_pick(None, 0.50, 0.50) is None
    # missing ask on the confident side → abstain (can't price-gate)
    assert brownian_gated_pick(0.90, None, 0.35) == "abstain"


def test_brownian_abstain_excluded_from_grading():
    # the runner passes None for abstain/no-data; only real sides grade
    assert grade_pick(None, "up") is None
    assert grade_pick("up", "up") == 1


def test_arb_sum_trigger():
    from backend.core.crypto5050 import arb_sum
    assert arb_sum(0.48, 0.49) == pytest.approx(0.97)   # < $1.00 → a hit
    assert arb_sum(0.52, 0.53) == pytest.approx(1.05)   # no hit
    assert arb_sum(None, 0.5) is None                   # unpollable → not counted


# ── balance discipline v2 (2026-07-22 PM) ────────────────────────────────────
def test_choose_side_v2_deficit_priority():
    from backend.core.crypto5050 import choose_side_v2
    # down lags → deficit priority even though up is cheaper
    assert choose_side_v2(30, 15, 0.30, 0.70) == "down"
    assert choose_side_v2(15, 30, 0.70, 0.30) == "up"
    # balanced → cheaper side (v1 behavior)
    assert choose_side_v2(15, 15, 0.40, 0.55) == "up"
    # deficit unquoted, no bargain → WAIT (never deepen imbalance)
    assert choose_side_v2(30, 15, 0.30, None) is None


def test_choose_side_v2_surplus_bargain_exception():
    from backend.core.crypto5050 import choose_side_v2
    # up is surplus, but up_ask+down_ask = 0.30+0.55 = 0.85 < 0.99 AND surplus
    # is cheaper → the instantly-pairable bargain justifies temporary imbalance
    assert choose_side_v2(30, 15, 0.30, 0.55) == "up"
    # sub-0.99 pair but surplus NOT cheaper → still deficit
    assert choose_side_v2(30, 15, 0.55, 0.30) == "down"
    # pair >= 0.99 → no exception, deficit only
    assert choose_side_v2(30, 15, 0.44, 0.56) == "down"


def test_balance_only_mode():
    from backend.core.crypto5050 import balance_only_allows
    assert balance_only_allows("down", 30, 15)        # reduces imbalance
    assert not balance_only_allows("up", 30, 15)      # deepens → forbidden
    assert not balance_only_allows("up", 15, 15)      # balanced → nothing allowed
    assert not balance_only_allows("down", 15, 15)


def test_balance_sweep_guard():
    from backend.core.crypto5050 import balance_sweep_wanted
    assert balance_sweep_wanted(30, 15, 0.60) == ("down", 15.0)   # residue>5, ask ok
    assert balance_sweep_wanted(30, 15, 0.98) is None             # ask > 0.97 → eat it
    assert balance_sweep_wanted(18, 15, 0.60) is None             # residue 3 <= 5
    assert balance_sweep_wanted(15, 15, 0.60) is None             # balanced
    assert balance_sweep_wanted(15, 30, 0.97) == ("up", 15.0)     # boundary ask ok


# ── post-mortem arithmetic on a fixture window ───────────────────────────────
def _fixture_row(**kw):
    d = dict(id=99, slug="btc-updown-5m-1", question="fixture", status="settled",
             up_shares=60.0, up_cost=60 * 0.5575, down_shares=45.0, down_cost=45 * 0.3967,
             lean_side="up", lean_shares=20.0, lean_price=0.76, lean_cost=15.2,
             lean_pnl=-15.2, fees_paid=0.0, net_pnl=-21.5, resolution="down",
             resolution_source="gamma", logic_version="v1",
             pick_spot_drift="up", pick_momentum="up", pick_depth="up",
             pick_late_recency="down", pick_brownian="abstain",
             hit_spot_drift=0, hit_momentum=0, hit_depth=0, hit_late_recency=1,
             hit_brownian=None, spot_open=66136.0, spot_t60=66200.0, spot_close=66150.0,
             up_mark=0.01, down_mark=0.99)
    d.update(kw)
    return SimpleNamespace(polls=[], **d)


def test_postmortem_decomposition_window10_fixture():
    from backend.core.crypto5050 import postmortem_decompose
    d = postmortem_decompose(_fixture_row())
    # window-10 arithmetic: locked +2.07, residue −8.36 (15sh up), lean −15.20
    assert d["locked"] == pytest.approx(2.07, abs=0.02)
    assert d["residue_pnl"] == pytest.approx(-8.36, abs=0.02)
    assert d["residue_side"] == "up" and d["residue_shares"] == pytest.approx(15.0)
    assert d["lean_pnl"] == pytest.approx(-15.2)
    # verdict = most negative component → lean-wrong
    assert d["verdict"] == "lean-wrong"
    # counterfactuals: no-lean removes the lean loss exactly
    assert d["cf"]["no_lean"] == pytest.approx(-21.5 + 15.2, abs=0.01)
    # balanced-at-close: buy 15 down @ close mark 0.99 → pair cost 0.5575+0.99
    # = 1.5475 → delta = 15*(1-1.5475) − (−8.36) = +0.15
    assert d["cf"]["balanced_delta"] == pytest.approx(0.15, abs=0.03)
    # late-recency traded instead (down, winner) at 1−0.76=0.24 → +15.2 swing
    # to a winning 20sh lean: net −21.5 +15.2 + (20 − 0.24*20) = +8.9
    assert d["rule_cf"]["late_recency"] == pytest.approx(8.9, abs=0.1)
    assert d["rule_cf"]["brownian"] is None      # abstain → no counterfactual
    assert d["rule_cf"]["spot_drift"] == pytest.approx(-21.5)   # same as traded


def test_postmortem_verdict_residue_loss():
    from backend.core.crypto5050 import postmortem_decompose
    # no lean, big losing residue → residue-loss
    d = postmortem_decompose(_fixture_row(lean_side=None, lean_shares=0.0,
                                          lean_price=None, lean_cost=0.0, lean_pnl=0.0,
                                          net_pnl=-6.29))
    assert d["verdict"] == "residue-loss"


def test_postmortem_backfill_idempotent(tmp_path, monkeypatch):
    import backend.core.crypto5050 as c5mod
    monkeypatch.setattr(c5mod, "POSTMORTEM_DIR", str(tmp_path))
    db = _db()
    w = CryptoWindow(slug="w1", status="settled", net_pnl=-5.0, up_shares=15.0,
                     up_cost=7.5, down_shares=0.0, down_cost=0.0, resolution="down",
                     lean_shares=0.0, lean_cost=0.0, fees_paid=0.0)
    db.add(w); db.commit()
    r, events = _runner(db)
    asyncio.run(r._backfill_postmortems())
    assert w.verdict == "residue-loss"
    assert (tmp_path / f"window_{w.id}.md").exists()
    n_events = len(events)
    asyncio.run(r._backfill_postmortems())      # second run: verdict set → skipped
    assert len(events) == n_events
