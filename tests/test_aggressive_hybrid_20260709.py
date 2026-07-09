"""Aggressive-hybrid v2 (2026-07-09) — pure-logic tests (no network, no posting).

Covers: build_limit_order_args rounding + 15-share guard, and the limit_price /
guard math resolve_weather_live applies. Mirrors the existing dry-run test style
(build_order_args is a static, dependency-free unit)."""
import math
import pytest
from backend.core.live_trader import WeatherLiveTrader as WLT


# ── build_limit_order_args: precision + the 15-share floor ────────────────────
def test_limit_price_rounds_down_to_tick_never_pays_above():
    a = WLT.build_limit_order_args("tok", size_usd=15.0, limit_price=0.677, tick_size="0.01")
    assert a["price"] == 0.67          # floored to tick, never 0.68 (never overpay)
    assert a["side"] == "BUY"
    assert a["token_id"] == "tok"


def test_shares_floor_and_amount_never_exceed_cap():
    a = WLT.build_limit_order_args("tok", size_usd=15.0, limit_price=0.70, tick_size="0.01")
    # shares = floor(15/0.70) to 4dp = 21.4285; cost = shares*price <= 15
    assert a["size"] == pytest.approx(21.4285, abs=1e-4)
    assert a["amount_usd"] <= 15.0


def test_fifteen_share_guard_at_high_price_low_cap():
    # $11 cap @ 0.80 → 13.75 shares < 15 → must refuse (today's refusal pattern)
    with pytest.raises(ValueError, match="15-share"):
        WLT.build_limit_order_args("tok", size_usd=11.0, limit_price=0.80, tick_size="0.01")


def test_fifteen_cap_clears_where_eleven_failed():
    # The whole point of $11->$15: at a high NO price (0.90), $15 buys >=15 shares,
    # $11 does not (11/0.90=12.2 < 15). $15 unblocks the high-priced NO entries.
    a = WLT.build_limit_order_args("tok", size_usd=15.0, limit_price=0.90, tick_size="0.01")
    assert a["size"] >= 15
    with pytest.raises(ValueError, match="15-share"):
        WLT.build_limit_order_args("tok", size_usd=11.0, limit_price=0.90, tick_size="0.01")


def test_missing_token_refused():
    with pytest.raises(ValueError, match="token_id"):
        WLT.build_limit_order_args("", 15.0, 0.70)


def test_price_clamped_below_one():
    a = WLT.build_limit_order_args("tok", size_usd=100.0, limit_price=0.999, tick_size="0.01")
    assert a["price"] <= 0.99


# ── the limit_price / guard math the scheduler applies (mirrored) ─────────────
def _limit_price(model_yes_prob, direction, floor=0.05):
    p_side = model_yes_prob if direction == "yes" else 1.0 - model_yes_prob
    return p_side - floor


def test_limit_price_no_side():
    # model YES 5% -> NO side model prob 95% -> limit 0.90 (the most we'd pay for NO)
    assert _limit_price(0.05, "no") == pytest.approx(0.90)


def test_limit_price_yes_side():
    assert _limit_price(0.82, "yes") == pytest.approx(0.77)


def test_guard_triggers_when_15x_limit_exceeds_cap():
    # cap $15, limit 0.90 -> 15*0.90=13.5 <= 15 -> OK (no guard)
    assert not (15.0 * _limit_price(0.05, "no") > 15.0)
    # cap $11, limit 0.90 -> 13.5 > 11 -> guard fires (clean skip)
    assert (15.0 * _limit_price(0.05, "no") > 11.0)


# ── _matched_shares: authoritative-only, never guesses ────────────────────────
def test_matched_shares_reads_variants():
    assert WLT._matched_shares({"size_matched": "12.5"}) == 12.5
    assert WLT._matched_shares({"sizeMatched": 3}) == 3.0
    assert WLT._matched_shares({}) == 0.0
    assert WLT._matched_shares(None) == 0.0
    assert WLT._matched_shares({"size_matched": "bad"}) == 0.0
