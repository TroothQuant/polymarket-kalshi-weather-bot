"""Ladder WIRING tests (2026-07-22) — census params armed.

Covers the five wiring behaviors added on top of the BUILD-2 core:
  1. ladder_plan     — rung prices/sizes from census params (offsets 5/9/13c,
                       split 40/30/30), 15-share fold-into-rung-1 collapse,
                       maker-only drop, degenerate cases
  2. execute_ladder  — take-first delegation when the book has fillable asks;
                       maker rungs posted when it doesn't; fail-closed on an
                       unreadable book
  3. resolve_weather_live routing — flag ON → execute_ladder with settings
                       params; flag OFF/absent → the proven hybrid, unchanged
  4. edge-flip cancel — manage_live_resting_orders cancels a resting order when
                       the refreshed signal no longer wants that side (incl. a
                       DIRECTION FLIP via the both-token meta keying) or the
                       resting price no longer clears the edge floor; STALE
                       (DB-fallback) meta never triggers a cancel
  5. exposure        — rung notional flows into resting_notional (the $33 cap
                       input; e5c3f0d BUG-1 accounting covers rungs unchanged)

No network, no py-clob: trader instances built via WLT.__new__ with mocks.
"""
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, Trade
from backend.core.live_trader import WeatherLiveTrader as WLT
import backend.core.scheduler as sched
from backend.core.scheduler import resolve_weather_live, manage_live_resting_orders


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


# ── 1. ladder_plan ────────────────────────────────────────────────────────────
def test_plan_three_rungs_all_feasible_at_low_prices():
    # p_side 0.30 → rungs 0.25/0.21/0.17; $6/$4.50/$4.50 all clear 15 sh.
    plan = WLT.ladder_plan(0.30, best_ask=0.40, size_usd=15.0)
    assert [r["price"] for r in plan] == [pytest.approx(0.25), pytest.approx(0.21), pytest.approx(0.17)]
    assert all(r["shares"] >= 15 for r in plan)
    # split honored: 40/30/30 of $15
    assert plan[0]["amount_usd"] == pytest.approx(6.0, abs=0.25)
    assert plan[1]["amount_usd"] == pytest.approx(4.5, abs=0.25)
    assert plan[2]["amount_usd"] == pytest.approx(4.5, abs=0.25)


def test_plan_collapses_to_single_full_rung_at_typical_prices():
    # p_side 0.60 → rungs 0.55/0.51/0.47. $6 @ 0.55 is only 10.9 sh (<15), so
    # everything folds into rung 1: ONE full-budget rung at −offsets[0] — the
    # proven hybrid shape (this is the documented 15-share-minimum deviation
    # from the literal 40/30/30 split).
    plan = WLT.ladder_plan(0.60, best_ask=0.70, size_usd=15.0)
    assert len(plan) == 1
    assert plan[0]["price"] == pytest.approx(0.55)
    assert plan[0]["shares"] >= 15
    assert plan[0]["amount_usd"] == pytest.approx(15.0, abs=0.5)


def test_plan_partial_collapse_keeps_deep_feasible_rung():
    # p_side 0.40 → rungs 0.35/0.31/0.27 with $6/$4.50/$4.50.
    # rung1: $6/0.35 = 17.1 sh OK; rung2: $4.5/0.31 = 14.5 sh < 15 → folds into
    # rung1; rung3: $4.5/0.27 = 16.6 sh OK → survives. Two rungs.
    plan = WLT.ladder_plan(0.40, best_ask=0.50, size_usd=15.0)
    assert [r["price"] for r in plan] == [pytest.approx(0.35), pytest.approx(0.27)]
    assert plan[0]["amount_usd"] == pytest.approx(10.5, abs=0.5)   # $6 + folded $4.5
    assert all(r["shares"] >= 15 for r in plan)


def test_plan_maker_only_drops_crossed_rungs_folds_deeper():
    # best_ask 0.33 sits below rung1 (0.35): rung1 is dropped (would be
    # marketable) and its budget folds DEEPER; every surviving rung < best_ask.
    plan = WLT.ladder_plan(0.40, best_ask=0.33, size_usd=15.0)
    assert plan, "deeper maker rungs must survive"
    assert all(r["price"] < 0.33 for r in plan)
    assert sum(r["amount_usd"] for r in plan) >= 10.0   # folded budget retained


def test_plan_empty_book_posts_all_rungs():
    plan_none = WLT.ladder_plan(0.30, best_ask=None, size_usd=15.0)
    assert [r["price"] for r in plan_none] == [pytest.approx(0.25), pytest.approx(0.21), pytest.approx(0.17)]


def test_plan_no_legal_rung_returns_empty():
    assert WLT.ladder_plan(0.0, best_ask=0.50, size_usd=15.0) == []
    assert WLT.ladder_plan(0.30, best_ask=0.01, size_usd=15.0) == []   # everything crossed
    assert WLT.ladder_plan(0.30, best_ask=0.40, size_usd=0.0) == []


def test_plan_tick_floor_drops_negative_rungs():
    # p_side 0.10 → rungs 0.05/0.01/−0.03; the negative rung's budget folds
    # into rung 1. All survivors are valid maker prices ≥ 1 tick.
    plan = WLT.ladder_plan(0.10, best_ask=0.20, size_usd=15.0)
    assert all(r["price"] >= 0.01 for r in plan)
    assert all(r["shares"] >= 15 for r in plan)


# ── 2. execute_ladder ─────────────────────────────────────────────────────────
def _ladder_trader(asks, posted, tick="0.01"):
    t = WLT.__new__(WLT)
    t.client = SimpleNamespace(get_tick_size=lambda tok: tick)
    t._book_asks = lambda tok: asks
    t.post_resting_limit = lambda tok, usd, price, ts="0.01": (
        posted.append((round(price, 2), round(usd, 2))) or
        {"order_id": f"OID{len(posted)}", "price": price,
         "shares": usd / price, "amount_usd": usd, "status": "live"})
    return t


def test_execute_ladder_takes_first_when_book_fillable(monkeypatch):
    # asks at/below the aggressive limit → FULL budget delegated to the hybrid
    # (take-first sweep unchanged), no maker rungs posted.
    posted, hybrid_calls = [], []
    t = _ladder_trader(asks=[(0.24, 40.0)], posted=posted)
    t.execute_aggressive_hybrid = lambda tok, usd, lim: (
        hybrid_calls.append((usd, lim)) or {"order_id": "H", "price": lim,
        "filled_shares": 20.0, "filled_cost": usd, "fill_price": lim,
        "resting_shares": 0.0, "status": "matched"})
    out = t.execute_ladder("TOK", 15.0, 0.30)
    assert hybrid_calls == [(15.0, pytest.approx(0.25))]
    assert posted == []
    assert out["filled_shares"] == 20.0


def test_execute_ladder_posts_maker_rungs_when_no_fillable():
    posted = []
    t = _ladder_trader(asks=[(0.40, 100.0)], posted=posted)   # best_ask above all rungs
    out = t.execute_ladder("TOK", 15.0, 0.30)
    assert [p for p, _ in posted] == [pytest.approx(0.25), pytest.approx(0.21), pytest.approx(0.17)]
    assert out["status"] == "rested_ladder"
    assert out["filled_shares"] == 0.0
    assert len(out["ladder_order_ids"]) == 3
    assert out["order_id"] == "OID1"


def test_execute_ladder_fail_closed_on_unreadable_book():
    posted = []
    t = _ladder_trader(asks=None, posted=posted)
    assert t.execute_ladder("TOK", 15.0, 0.30) is None
    assert posted == []


# ── 3. resolve_weather_live routing ──────────────────────────────────────────
def _settings(**over):
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


def test_resolve_routes_to_ladder_when_flag_on():
    calls = []
    def factory():
        def execute_ladder(token_id, size_usd, model_p_side, offsets, split):
            calls.append((token_id, size_usd, model_p_side, offsets, split))
            return {"order_id": "L1", "price": model_p_side - offsets[0],
                    "filled_shares": 0.0, "filled_cost": 0.0, "fill_price": None,
                    "resting_shares": 40.0, "status": "rested_ladder",
                    "ladder_order_ids": ["L1", "L2"]}
        return SimpleNamespace(execute_ladder=execute_ladder)
    st = _settings(WEATHER_LIVE_LADDER=True,
                   WEATHER_LIVE_LADDER_OFFSETS="0.05,0.09,0.13",
                   WEATHER_LIVE_LADDER_SPLIT="0.40,0.30,0.30")
    d = resolve_weather_live(_signal(), 15.0, 0.40, _db(), st, factory)
    assert d.action == "rested"
    assert calls == [("NO", 15.0, pytest.approx(0.40), (0.05, 0.09, 0.13), (0.40, 0.30, 0.30))]


def test_resolve_flag_off_uses_hybrid_unchanged():
    rec = []
    def factory():
        def execute_aggressive_hybrid(token_id, size_usd, limit_price):
            rec.append(limit_price)
            return {"order_id": "H", "price": limit_price, "filled_shares": 0.0,
                    "filled_cost": 0.0, "fill_price": None,
                    "resting_shares": 30.0, "status": "live"}
        return SimpleNamespace(execute_aggressive_hybrid=execute_aggressive_hybrid)
    d = resolve_weather_live(_signal(), 15.0, 0.40, _db(), _settings(), factory)
    assert d.action == "rested" and rec == [pytest.approx(0.35)]


# ── 4. edge-flip cancel in manage_live_resting_orders ────────────────────────
def _fake_order(order_id="0xR1", token="NO", price=0.55, size=27.0, matched=0.0):
    return {"id": order_id, "asset_id": token, "price": str(price),
            "original_size": str(size), "size_matched": str(matched), "side": "BUY"}


def _mgr_trader(orders, cancels):
    t = WLT.__new__(WLT)
    t.list_open_weather_orders = lambda: orders
    t.cancel = lambda oid: cancels.append(oid) or True
    return t


def _meta(actionable=True, p_side=0.65, direction="no", fresh=True, target=None):
    m = {"market_id": "2400000", "slug": "slug", "direction": direction,
         "model_probability": 1.0 - p_side if direction == "no" else p_side,
         "market_probability": 0.40, "edge": 0.2, "target_date": target,
         "model_p_side": p_side, "actionable": actionable}
    if fresh:
        m["fresh"] = True
    return m


def _mgr_settings():
    return SimpleNamespace(WEATHER_LIVE_TRADING=True, WEATHER_LIVE_MIN_EDGE_FLOOR=0.05)


def test_edge_flip_cancels_when_no_longer_actionable():
    cancels = []
    t = _mgr_trader([_fake_order()], cancels)
    toks, notional, ok = manage_live_resting_orders(
        t, _db(), {"NO": _meta(actionable=False)}, _mgr_settings())
    assert cancels == ["0xR1"]
    assert toks == set() and notional == 0.0 and ok


def test_edge_flip_cancels_on_direction_flip_other_side_token():
    # The refreshed signal flipped to YES: the NO-side token's meta (both-token
    # keying) carries actionable=False → the stale NO rest is cancelled.
    cancels = []
    t = _mgr_trader([_fake_order(token="NO")], cancels)
    meta_no = _meta(actionable=False, p_side=0.30, direction="no")   # side no longer wanted
    toks, _, _ = manage_live_resting_orders(t, _db(), {"NO": meta_no}, _mgr_settings())
    assert cancels == ["0xR1"] and toks == set()


def test_edge_flip_cancels_when_price_no_longer_clears_floor():
    # actionable, but refreshed p_side 0.55 − floor 0.05 = 0.50 < resting 0.55
    # → protective_action says reprice/cancel → we cancel.
    cancels = []
    t = _mgr_trader([_fake_order(price=0.55)], cancels)
    toks, _, _ = manage_live_resting_orders(
        t, _db(), {"NO": _meta(actionable=True, p_side=0.55)}, _mgr_settings())
    assert cancels == ["0xR1"] and toks == set()


def test_edge_flip_holds_when_still_actionable_and_clearing_floor():
    cancels = []
    t = _mgr_trader([_fake_order(price=0.55)], cancels)
    toks, notional, ok = manage_live_resting_orders(
        t, _db(), {"NO": _meta(actionable=True, p_side=0.65)}, _mgr_settings())
    assert cancels == []
    assert toks == {"NO"}
    assert notional == pytest.approx(0.55 * 27.0)   # rung notional → $33 cap input


def test_stale_db_fallback_meta_never_triggers_edge_flip():
    # No fresh meta for the token: manage falls back to a prior Trade row —
    # entry-time numbers must NOT cancel the order.
    cancels = []
    db = _db()
    db.add(Trade(market_ticker="2400000", platform="polymarket", event_slug="s",
                 market_type="weather", direction="no", entry_price=0.55, size=5.5,
                 order_id="0xR1", token_id="NO", model_probability=0.35))
    db.commit()
    t = _mgr_trader([_fake_order()], cancels)
    toks, notional, ok = manage_live_resting_orders(t, db, {}, _mgr_settings())
    assert cancels == []
    assert toks == {"NO"}
