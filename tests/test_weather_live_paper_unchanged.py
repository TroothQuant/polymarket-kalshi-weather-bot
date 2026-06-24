"""G2-1 test: with WEATHER_LIVE_TRADING off (the default), the paper path is
byte-for-byte unchanged by the P0 token-id capture, and the live module never
pulls py-clob-client onto the paper import path.
"""
import sys

from backend.config import Settings
from backend.data.weather_markets import _parse_polymarket_weather

# A far-future date so the market always clears the `target_date >= today` filter.
SAMPLE = {
    "id": "999001",
    "question": "Will the highest temperature in Chicago be 90F or higher on December 31, 2099?",
    "outcomePrices": '["0.30", "0.70"]',     # JSON-string form (the live shape)
    "clobTokenIds": '["111yes", "222no"]',   # JSON-string form
    "conditionId": "0xCAFE",
    "closed": False,
    "volume": "5000",
}


def test_flag_ships_off():
    s = Settings()
    assert s.WEATHER_LIVE_TRADING is False           # the master flag still ships OFF
    assert s.WEATHER_LIVE_MAX_TRADE_USD == 11.0       # tiny per-trade cap; raised 2->11 (2026-06-24) to clear the REAL 15-share CLOB min
    assert s.WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD == 25.0
    assert s.WEATHER_LIVE_DAILY_LOSS_STOP_USD == 10.0
    assert s.CLOB_HOST == "https://clob.polymarket.com"


def test_paper_fields_unchanged_and_tokens_captured():
    m = _parse_polymarket_weather(SAMPLE, "highest-temperature-in-chicago-on-december-31-2099")
    assert m is not None
    # Paper-relevant fields are exactly what they were pre-P0.
    assert m.platform == "polymarket"
    assert m.city_key == "chicago"
    assert m.direction == "above"
    assert m.threshold_f == 90.0
    assert abs(m.yes_price - 0.30) < 1e-9
    assert abs(m.no_price - 0.70) < 1e-9
    assert abs(m.volume - 5000.0) < 1e-9
    # New P0 fields captured (JSON-string form handled, ordered yes/no).
    assert m.token_id_yes == "111yes"
    assert m.token_id_no == "222no"
    assert m.condition_id == "0xCAFE"


def test_tokens_handle_array_form():
    d = dict(SAMPLE, clobTokenIds=["aaa", "bbb"], outcomePrices=["0.40", "0.60"])
    m = _parse_polymarket_weather(d, "highest-temperature-in-chicago-on-december-31-2099")
    assert m is not None
    assert m.token_id_yes == "aaa"
    assert m.token_id_no == "bbb"


def test_f3_yes_no_mapped_by_outcomes_label_not_position():
    # Reversed outcomes: "No" first. Both price AND token must map by LABEL, so
    # yes_* resolves to index 1 and no_* to index 0 — not a blind [0]/[1].
    d = dict(
        SAMPLE,
        outcomes='["No", "Yes"]',
        outcomePrices='["0.70", "0.30"]',       # No=0.70, Yes=0.30
        clobTokenIds='["notoken", "yestoken"]',  # No-token first
    )
    m = _parse_polymarket_weather(d, "highest-temperature-in-chicago-on-december-31-2099")
    assert m is not None
    assert abs(m.yes_price - 0.30) < 1e-9
    assert abs(m.no_price - 0.70) < 1e-9
    assert m.token_id_yes == "yestoken"
    assert m.token_id_no == "notoken"


def test_missing_tokens_default_empty_paper_still_parses():
    # A market with no clobTokenIds still parses for paper (live path will refuse it).
    d = dict(SAMPLE)
    d.pop("clobTokenIds")
    d.pop("conditionId")
    m = _parse_polymarket_weather(d, "highest-temperature-in-chicago-on-december-31-2099")
    assert m is not None                 # paper unaffected
    assert m.token_id_yes == ""          # live path must refuse this market
    assert m.token_id_no == ""
    assert m.condition_id == ""


def test_live_trader_import_does_not_pull_pyclob():
    # Importing the live module on the paper path must NOT import py-clob-client.
    sys.modules.pop("py_clob_client", None)
    import backend.core.live_trader  # noqa: F401
    assert "py_clob_client" not in sys.modules, \
        "py-clob-client leaked onto the paper import path"
