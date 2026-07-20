"""Realistic-fill engine tests (2026-07-20) — pure logic, no network.
Covers: book with asks, empty book, asks above cap, partial size, and fee."""
import math
import pytest
from backend.core.execution_realism import (
    taker_fee, sweep_fill, realistic_fill,
)


# ── taker fee ─────────────────────────────────────────────────────────────────
def test_taker_fee_formula():
    # 0.05 * p * (1-p) * shares
    assert taker_fee(0.5, 100) == pytest.approx(0.05 * 0.5 * 0.5 * 100)   # 1.25
    assert taker_fee(0.9, 20) == pytest.approx(0.05 * 0.9 * 0.1 * 20)     # 0.09
    assert taker_fee(0.0, 100) == 0.0
    assert taker_fee(1.0, 100) == 0.0


# ── sweep_fill: the VWAP sweep with an entry cap ──────────────────────────────
def test_sweep_single_level_below_cap():
    vwap, filled = sweep_fill([{"price": 0.60, "size": 100}], cap_price=0.65, desired_shares=40)
    assert filled == pytest.approx(40)
    assert vwap == pytest.approx(0.60)


def test_sweep_vwap_across_levels():
    # want 30 sh; 10@0.60 + 20@0.62 all <= cap 0.65 -> vwap = (6+12.4)/30
    vwap, filled = sweep_fill(
        [{"price": 0.62, "size": 20}, {"price": 0.60, "size": 10}],  # unsorted on purpose
        cap_price=0.65, desired_shares=30)
    assert filled == pytest.approx(30)
    assert vwap == pytest.approx((10*0.60 + 20*0.62) / 30)


def test_sweep_stops_at_cap():
    # cap 0.61: only the 0.60 level (10 sh) is fillable; the 0.62 level is above cap
    vwap, filled = sweep_fill(
        [{"price": 0.60, "size": 10}, {"price": 0.62, "size": 50}],
        cap_price=0.61, desired_shares=40)
    assert filled == pytest.approx(10)      # partial — only 10 sh <= cap
    assert vwap == pytest.approx(0.60)


def test_sweep_all_above_cap_is_zero():
    vwap, filled = sweep_fill([{"price": 0.80, "size": 100}], cap_price=0.70, desired_shares=20)
    assert (vwap, filled) == (0.0, 0.0)


def test_sweep_empty_book_is_zero():
    assert sweep_fill([], 0.7, 20) == (0.0, 0.0)
    assert sweep_fill(None, 0.7, 20) == (0.0, 0.0)


def test_sweep_ignores_zero_and_negative_levels():
    vwap, filled = sweep_fill(
        [{"price": 0.60, "size": 0}, {"price": 0.0, "size": 50}, {"price": 0.60, "size": 25}],
        cap_price=0.65, desired_shares=25)
    assert filled == pytest.approx(25)
    assert vwap == pytest.approx(0.60)


# ── realistic_fill: full path incl. fee-folding ───────────────────────────────
def test_realistic_fill_full_with_fee():
    book = {"asks": [{"price": 0.50, "size": 1000}]}
    out = realistic_fill(book, cap_price=0.50, size_usd=10.0)
    assert out is not None
    # desired = 10/0.50 = 20 sh; fully filled at 0.50
    assert out["filled_shares"] == pytest.approx(20)
    assert out["fill_price"] == pytest.approx(0.50)
    assert out["partial"] is False
    fee = 0.05 * 0.50 * 0.50 * 20                      # 0.25
    assert out["fee"] == pytest.approx(fee)
    assert out["cost"] == pytest.approx(20*0.50 + fee)  # notional + fee
    # fee folded into effective entry
    assert out["effective_entry_price"] == pytest.approx(0.50 + fee/20)


def test_realistic_fill_partial():
    # cap 0.60, want 10/0.60=16.67 sh, but only 5 sh available <= cap
    book = {"asks": [{"price": 0.60, "size": 5}, {"price": 0.70, "size": 999}]}
    out = realistic_fill(book, cap_price=0.60, size_usd=10.0)
    assert out is not None
    assert out["filled_shares"] == pytest.approx(5)
    assert out["partial"] is True


def test_realistic_fill_empty_book_none():
    assert realistic_fill({"asks": []}, cap_price=0.60, size_usd=10.0) is None
    assert realistic_fill({}, cap_price=0.60, size_usd=10.0) is None


def test_realistic_fill_all_above_cap_none():
    book = {"asks": [{"price": 0.75, "size": 500}]}
    assert realistic_fill(book, cap_price=0.60, size_usd=10.0) is None


def test_realistic_fill_bad_inputs_none():
    assert realistic_fill({"asks": [{"price": 0.5, "size": 100}]}, cap_price=0.0, size_usd=10) is None
    assert realistic_fill({"asks": [{"price": 0.5, "size": 100}]}, cap_price=0.5, size_usd=0) is None
