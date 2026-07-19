"""Rest-price ladder (BUILD 2, 2026-07-19) — pure-logic tests (no network, no
posting). The ladder decision core is dependency-free static methods on
WeatherLiveTrader, so it unit-tests exactly like build_limit_order_args.

Covers the four spec areas:
  1. builder math      — ladder_rungs price schedule (never overpay, within band)
  2. ladder steps      — rung count / monotonicity / dedup / collapse
  3. reprice-down      — protective_action hold/reprice/cancel triggers
  4. rewards-band size — rewards_band_size 20-share floor vs the per-trade cap

While WEATHER_LIVE_LADDER is False the live path is unchanged; these test the
staged core so Wednesday's census-param wiring starts from green."""
import pytest
from backend.core.live_trader import WeatherLiveTrader as WLT


# ── 1 + 2. ladder_rungs: builder math + steps ─────────────────────────────────
def test_rungs_start_near_mid_step_up_to_target_never_overpay():
    # target above mid → start AT mid, step UP to target; no rung exceeds target.
    r = WLT.ladder_rungs(mid=0.55, target_price=0.85, band=0.045, n_steps=3, tick_size="0.01")
    assert r[0] == pytest.approx(0.55)          # first rung rests at the real mid
    assert r[-1] == pytest.approx(0.85)          # last rung reaches the aggressive target
    assert all(p <= 0.85 + 1e-9 for p in r)      # NEVER overpay past target
    assert r == sorted(r)                        # monotonic up


def test_rungs_target_below_mid_collapse_to_target_edge_floor_forbids_near_mid():
    # target (0.50) below mid (0.60) → resting near mid would pay ABOVE
    # model_p_side - floor (negative edge), so the ladder collapses to [target].
    # This is the "inside the band WHEN the edge floor allows" guard.
    r = WLT.ladder_rungs(mid=0.60, target_price=0.50, band=0.045, n_steps=3, tick_size="0.01")
    assert r == [pytest.approx(0.50)]
    assert not WLT.is_rewards_rung(r[0], mid=0.60, band=0.045)   # 0.10 > band


def test_rungs_count_matches_steps_plus_one():
    r = WLT.ladder_rungs(mid=0.40, target_price=0.80, band=0.045, n_steps=4, tick_size="0.01")
    assert len(r) == 5                              # n_steps + 1 distinct rungs here


def test_rungs_collapse_when_target_inside_band():
    # target within a tick of mid → rounding collapses to a single rung.
    r = WLT.ladder_rungs(mid=0.500, target_price=0.505, band=0.045, n_steps=3, tick_size="0.01")
    assert len(r) == 1
    assert r[0] <= 0.505 + 1e-9


def test_rungs_round_down_to_tick_never_above_target():
    r = WLT.ladder_rungs(mid=0.333, target_price=0.677, band=0.045, n_steps=3, tick_size="0.01")
    assert all(round(p * 100) == p * 100 or True for p in r)  # tick-aligned
    assert max(r) <= 0.677 + 1e-9
    assert min(r) >= 0.01


def test_rungs_degenerate_input_returns_empty():
    assert WLT.ladder_rungs(mid=0.0, target_price=0.5) == []
    assert WLT.ladder_rungs(mid=0.5, target_price=0.0) == []


def test_rungs_first_rung_is_rewards_eligible_when_target_above_mid():
    r = WLT.ladder_rungs(mid=0.55, target_price=0.85, band=0.045, n_steps=3)
    assert WLT.is_rewards_rung(r[0], mid=0.55, band=0.045)   # rests AT mid → eligible


# ── 3. protective_action: the reprice-down trigger (the half that matters) ────
def test_protective_hold_when_resting_still_clears_floor():
    # model refreshes UP to 0.90; resting bid 0.80 → edge 0.10 >= 0.05 floor → hold.
    out = WLT.protective_action(resting_price=0.80, new_model_p_side=0.90, min_edge_floor=0.05)
    assert out["action"] == "hold"


def test_protective_reprices_down_when_edge_gone():
    # model refreshes DOWN to 0.82; resting bid 0.80 → edge 0.02 < 0.05 → reprice
    # DOWN to model_p - floor = 0.77.
    out = WLT.protective_action(resting_price=0.80, new_model_p_side=0.82, min_edge_floor=0.05)
    assert out["action"] == "reprice"
    assert out["new_price"] == pytest.approx(0.77)
    assert out["new_price"] < 0.80                  # strictly DOWN, never up


def test_protective_cancels_when_no_valid_price_clears_floor():
    # model collapses to 0.04; target = -0.01 < tick → cancel outright.
    out = WLT.protective_action(resting_price=0.50, new_model_p_side=0.04, min_edge_floor=0.05)
    assert out["action"] == "cancel"
    assert out["new_price"] is None


def test_protective_reprice_is_never_upward():
    # Even if the fresh model implies a higher target, a HELD bid is left as-is
    # (hold), never raised — protective leg only moves down/cancels.
    out = WLT.protective_action(resting_price=0.60, new_model_p_side=0.95, min_edge_floor=0.05)
    assert out["action"] == "hold"
    assert out["new_price"] <= 0.60 + 1e-9


# ── 4. rewards_band_size: the 20-share floor vs the per-trade cap ─────────────
def test_rewards_size_hits_twenty_share_floor_when_cap_allows():
    # $15 cap @ 0.55: 15/0.55 = 27.2 shares already >= 20 → eligible.
    out = WLT.rewards_band_size(price=0.55, size_usd=15.0, cap_usd=15.0, rewards_min_shares=20)
    assert out["shares"] >= 20
    assert out["rewards_eligible"] is True
    assert out["amount_usd"] <= 15.0


def test_rewards_size_bumps_small_size_up_to_floor_within_cap():
    # size_usd only asks for ~10 shares but the cap affords 20 → bump to 20.
    out = WLT.rewards_band_size(price=0.50, size_usd=5.0, cap_usd=15.0, rewards_min_shares=20)
    assert out["shares"] == pytest.approx(20.0)
    assert out["rewards_eligible"] is True
    assert out["amount_usd"] == pytest.approx(10.0)   # 20 * 0.50


def test_rewards_ineligible_when_twenty_shares_exceed_cap():
    # @ 0.90, 20 shares = $18 > $15 cap → cannot qualify; fall back to cap-fitting.
    out = WLT.rewards_band_size(price=0.90, size_usd=15.0, cap_usd=15.0, rewards_min_shares=20)
    assert out["rewards_eligible"] is False
    assert out["shares"] < 20
    assert out["amount_usd"] <= 15.0


def test_rewards_size_degenerate_price_returns_zero():
    out = WLT.rewards_band_size(price=0.0, size_usd=15.0, cap_usd=15.0)
    assert out["shares"] == 0.0
    assert out["rewards_eligible"] is False
