"""Settlement P&L + risk math (2026-07-21 coverage pass) — pure functions, no network.
calculate_pnl (share-purchase model, YES/NO win-loss, up/down mapping, fee-folded
basis) + compute_stop_loss_threshold + mark_to_market_loss."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from backend.models.database import Trade
from backend.core.settlement import (
    calculate_pnl, compute_stop_loss_threshold, mark_to_market_loss)


def _t(direction, entry, size):
    return Trade(direction=direction, entry_price=entry, size=size, market_type="weather")


def test_pnl_yes_win_and_loss():
    # $40 @ 0.40 -> 100 shares. Win pays $1/share.
    assert calculate_pnl(_t("yes", 0.40, 40.0), 1.0) == pytest.approx(60.0)   # 100*(1-0.4)
    assert calculate_pnl(_t("yes", 0.40, 40.0), 0.0) == pytest.approx(-40.0)  # =-size


def test_pnl_no_win_and_loss():
    # NO side wins when settlement_value=0 (down). #94-like: $25.02 @ 0.29.
    win = calculate_pnl(_t("no", 0.29, 25.02), 0.0)
    assert win == pytest.approx((25.02/0.29)*(1-0.29), abs=0.01)              # ~+61.26
    loss = calculate_pnl(_t("no", 0.29, 25.02), 1.0)
    assert loss == pytest.approx(-25.02, abs=0.01)                            # =-size


def test_pnl_up_down_alias_to_yes_no():
    assert calculate_pnl(_t("up", 0.5, 50.0), 1.0) == calculate_pnl(_t("yes", 0.5, 50.0), 1.0)
    assert calculate_pnl(_t("down", 0.5, 50.0), 0.0) == calculate_pnl(_t("no", 0.5, 50.0), 0.0)


def test_pnl_guards_bad_entry():
    assert calculate_pnl(_t("yes", 0.0, 40.0), 1.0) == 0.0
    assert calculate_pnl(_t("yes", None, 40.0), 1.0) == 0.0


def test_pnl_fee_folded_basis_lowers_win():
    # A fee-inclusive (higher) entry price is a higher cost basis -> smaller win.
    no_fee = calculate_pnl(_t("no", 0.7300, 15.0), 0.0)
    with_fee = calculate_pnl(_t("no", 0.7350, 15.0), 0.0)   # fee folded into basis
    assert with_fee < no_fee


def test_stop_loss_threshold_is_fraction_of_size():
    assert compute_stop_loss_threshold(0.4, 100.0, 0.50) == pytest.approx(50.0)
    assert compute_stop_loss_threshold(0.9, 25.0, 0.50) == pytest.approx(12.5)


def test_mark_to_market_loss_sign():
    # YES @ 0.50, $50 -> 100 sh. Mark down to 0.30 -> unrealized -20 -> loss +20.
    assert mark_to_market_loss(_t("yes", 0.50, 50.0), 0.30, 0.70) == pytest.approx(20.0)
    # Mark up to 0.70 -> gain -> loss is negative.
    assert mark_to_market_loss(_t("yes", 0.50, 50.0), 0.70, 0.30) == pytest.approx(-20.0)
    # NO position keys off no_price.
    assert mark_to_market_loss(_t("no", 0.50, 50.0), 0.70, 0.30) == pytest.approx(20.0)
