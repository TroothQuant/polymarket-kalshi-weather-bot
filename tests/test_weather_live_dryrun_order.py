"""G2-1 test: dry-run order construction. Build the order spec for a known
weather market and assert its shape / token_id / price WITHOUT posting (no
network, no py-clob-client, no signing). post_order lives only in execute_buy,
which is never called here.
"""
from datetime import date

import sys
import pytest

from backend.core.live_trader import WeatherLiveTrader
from backend.data.weather_markets import WeatherMarket


def _market():
    return WeatherMarket(
        slug="highest-temperature-in-chicago-on-december-31-2099",
        market_id="999001", platform="polymarket", title="t",
        city_key="chicago", city_name="Chicago", target_date=date(2099, 12, 31),
        threshold_f=90.0, metric="high", direction="above",
        yes_price=0.30, no_price=0.70,
        token_id_yes="YESTOKEN", token_id_no="NOTOKEN", condition_id="0xCAFE",
    )


def test_yes_side_order_shape():
    m = _market()
    a = WeatherLiveTrader.build_order_args(m.token_id_yes, size_usd=2.0, market_price=m.yes_price)
    assert a["token_id"] == "YESTOKEN"      # buys the YES token
    assert a["side"] == "BUY"
    assert a["amount"] == 2.0               # honors the size we passed (capped upstream)
    assert a["price"] == 0.32               # 2-tick taker aggression: 0.30 + 0.02


def test_no_side_and_price_cap():
    m = _market()
    a = WeatherLiveTrader.build_order_args(m.token_id_no, size_usd=2.0, market_price=0.985)
    assert a["token_id"] == "NOTOKEN"       # buys the NO token
    assert a["price"] == 0.99               # +0.02 would be 1.005 → capped at 0.99


def test_p0_guard_refuses_missing_token():
    with pytest.raises(ValueError):
        WeatherLiveTrader.build_order_args("", 2.0, 0.30)


def test_zero_size_refused():
    with pytest.raises(ValueError):
        WeatherLiveTrader.build_order_args("YESTOKEN", 0.0, 0.30)


def test_dryrun_touches_no_network_or_crypto():
    # build_order_args is pure: a complete order spec, no client, no signing.
    a = WeatherLiveTrader.build_order_args("YESTOKEN", 2.0, 0.30)
    assert set(a.keys()) == {"token_id", "price", "amount", "side"}
    # constructing the order required neither py-clob-client nor any network.
    assert "py_clob_client" not in sys.modules


# ── _parse_fill: fill_price is the realized average (cost/shares) ─────────────
def test_parse_fill_price_is_realized_average_and_implies_shares():
    f = WeatherLiveTrader._parse_fill(
        {"makingAmount": "1.68", "takingAmount": "4.0"}, "OID", 2.0, 0.42)
    assert f["cost"] == 1.68 and f["shares"] == 4.0
    assert abs(f["fill_price"] - 1.68 / 4.0) < 1e-12
    # the settlement identity: shares = size / entry_price (within rounding)
    assert abs(f["cost"] / f["fill_price"] - f["shares"]) < 1e-9


def test_parse_fill_fallback_consistent_when_no_amounts():
    # No making/taking in the response → cost=size, shares=size/price, and the
    # identity still holds with fill_price == the order price.
    f = WeatherLiveTrader._parse_fill({}, "OID", 2.0, 0.40)
    assert abs(f["fill_price"] - 0.40) < 1e-12
    assert abs(f["cost"] / f["fill_price"] - f["shares"]) < 1e-9


def test_parse_fill_zero_shares_returns_none():
    # MATCHED but zero tokens received → no row.
    assert WeatherLiveTrader._parse_fill(
        {"makingAmount": "2.0", "takingAmount": "0"}, "OID", 2.0, 0.0) is None
