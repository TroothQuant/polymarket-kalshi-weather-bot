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


# ── BUG 2 (cancel) + BUG 3 (actual fill economics), added 2026-07-10 ──────────
class _MockClient:
    def __init__(self, trades=None, cancel_resp=None):
        self._trades = trades or []
        self._cancel_resp = cancel_resp if cancel_resp is not None else {"canceled": []}
        self.cancel_calls = []

    def cancel_orders(self, hashes):
        self.cancel_calls.append(hashes)
        return self._cancel_resp

    def get_trades(self, params=None):
        return self._trades


def _trader_with_client(client):
    t = WLT.__new__(WLT)          # bypass __init__ (no network / no key)
    t.client = client
    return t


def test_cancel_uses_cancel_orders_list_of_hashes():
    c = _MockClient(cancel_resp={"canceled": ["0xabc"]})
    t = _trader_with_client(c)
    assert t.cancel("0xabc") is True
    assert c.cancel_calls == [["0xabc"]]      # BUG-2: list of hashes, NOT cancel_order(str)


def test_cancel_false_when_not_confirmed():
    t = _trader_with_client(_MockClient(cancel_resp={"canceled": []}))
    assert t.cancel("0xabc") is False


def test_actual_fill_sums_taker_trades_vwap():
    # BUG-3: the aggressive take sweeps BELOW the limit → real avg < limit.
    trades = [
        {"taker_order_id": "0xORD", "price": "0.70", "size": "10"},
        {"taker_order_id": "0xORD", "price": "0.80", "size": "5"},
        {"taker_order_id": "0xOTHER", "price": "0.99", "size": "100"},
    ]
    t = _trader_with_client(_MockClient(trades=trades))
    r = t._actual_fill_via_trades("0xORD")
    assert r["shares"] == pytest.approx(15.0)
    assert r["cost"] == pytest.approx(11.0)           # 7 + 4
    assert r["fill_price"] == pytest.approx(11.0 / 15.0)  # VWAP 0.7333, not the limit


def test_actual_fill_matches_trade2_real_economics():
    # The real trade #2: 15.07 sh @ 0.735 (recorded wrongly at limit 0.899).
    trades = [{"taker_order_id": "0x4b6f88f6", "price": "0.735", "size": "15.07"}]
    r = _trader_with_client(_MockClient(trades=trades))._actual_fill_via_trades("0x4b6f88f6")
    assert r["shares"] == pytest.approx(15.07)
    assert r["cost"] == pytest.approx(15.07 * 0.735, abs=1e-6)
    assert r["fill_price"] == pytest.approx(0.735)


def test_actual_fill_none_triggers_fallback():
    # No matching taker trade → None → execute() falls back to limit price + WARNING.
    assert _trader_with_client(_MockClient(trades=[]))._actual_fill_via_trades("0xORD") is None
