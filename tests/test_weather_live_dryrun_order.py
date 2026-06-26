"""G2-1 test: dry-run order construction. Build the order spec for a known
weather market and assert its shape / token_id / price / SIZE WITHOUT posting
(no network, no py-clob-client, no signing).

Updated 2026-06-24: build_order_args now returns `size` (SHARES = size_usd/price)
not `amount` (USD), fixing the latent OrderArgs units bug. py-clob OrderArgs has
no `amount` field; size is in conditional tokens. cost = size*price ~= size_usd.
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


def test_yes_side_order_shape_and_size_in_shares():
    m = _market()
    a = WeatherLiveTrader.build_order_args(m.token_id_yes, size_usd=2.0, market_price=m.yes_price)
    assert a["token_id"] == "YESTOKEN"
    assert a["side"] == "BUY"
    assert a["price"] == 0.32                # 2-tick taker aggression: 0.30 + 0.02
    # size is SHARES = size_usd / price (NOT USD). 2.0 / 0.32 = 6.25.
    assert a["size"] == 6.25
    assert a["amount_usd"] == 2.0
    # the units identity: cost = size * price ~= the dollars we intended to spend
    assert abs(a["size"] * a["price"] - 2.0) < 0.01


def test_no_side_and_price_cap():
    m = _market()
    a = WeatherLiveTrader.build_order_args(m.token_id_no, size_usd=2.0, market_price=0.985)
    assert a["token_id"] == "NOTOKEN"
    assert a["price"] == 0.99                # +0.02 → 1.005 capped at 0.99
    assert abs(a["size"] * a["price"] - 2.0) < 0.02


def test_p0_guard_refuses_missing_token():
    with pytest.raises(ValueError):
        WeatherLiveTrader.build_order_args("", 2.0, 0.30)


def test_zero_size_refused():
    with pytest.raises(ValueError):
        WeatherLiveTrader.build_order_args("YESTOKEN", 0.0, 0.30)


def test_dryrun_touches_no_network_or_crypto():
    a = WeatherLiveTrader.build_order_args("YESTOKEN", 2.0, 0.30)
    assert set(a.keys()) == {"token_id", "price", "size", "amount_usd", "side"}
    assert "py_clob_client" not in sys.modules


def test_parse_fill_price_is_realized_average_and_implies_shares():
    f = WeatherLiveTrader._parse_fill(
        {"makingAmount": "1.68", "takingAmount": "4.0"}, "OID", 2.0, 0.42)
    assert f["cost"] == 1.68 and f["shares"] == 4.0
    assert abs(f["fill_price"] - 1.68 / 4.0) < 1e-12
    assert abs(f["cost"] / f["fill_price"] - f["shares"]) < 1e-9


def test_parse_fill_empty_response_is_non_fill():
    # Under FAK the response is authoritative: no amounts = killed = non-fill.
    # (No size_usd/price estimate fallback — that could fabricate a phantom on a
    # 0-fill now that _parse_fill runs on every response. Audit E3, 2026-06-26.)
    assert WeatherLiveTrader._parse_fill({}, "OID", 2.0, 0.40) is None


def test_parse_fill_partial_fak_records_actual_filled_amount():
    # Thin-book FAK fills only part: record the ACTUAL filled portion, no phantom
    # for the killed remainder.
    f = WeatherLiveTrader._parse_fill(
        {"makingAmount": "5.5", "takingAmount": "10.0"}, "OID", 11.0, 0.55)
    assert f["cost"] == 5.5 and f["shares"] == 10.0
    assert abs(f["fill_price"] - 0.55) < 1e-9


def test_parse_fill_zero_shares_returns_none():
    assert WeatherLiveTrader._parse_fill(
        {"makingAmount": "2.0", "takingAmount": "0"}, "OID", 2.0, 0.0) is None
