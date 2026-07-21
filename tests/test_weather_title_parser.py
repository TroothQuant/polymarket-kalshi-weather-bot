"""Weather title parser (2026-07-21 coverage pass) — F and C paths, alias
resolution across the 48-city set, range-bucket rejection. Pure, no network."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.data.weather_markets import _parse_weather_market_title


def test_fahrenheit_threshold_us_city():
    p = _parse_weather_market_title("Will the highest temperature in New York be 77°F or higher on July 21, 2026?")
    assert p is not None
    assert p["city_key"] == "nyc"
    assert abs(p["threshold_f"] - 77.0) < 0.01
    assert p["direction"] == "above"


def test_celsius_converts_to_fahrenheit():
    p = _parse_weather_market_title("Will the highest temperature in Tokyo be 38°C or higher on July 22, 2026?")
    assert p is not None
    assert p["city_key"] == "tokyo"
    assert abs(p["threshold_f"] - 100.4) < 0.01   # 38C -> 100.4F


def test_below_direction_and_celsius_alias_city():
    p = _parse_weather_market_title("Will the highest temperature in Buenos Aires be 15°C or below on July 22, 2026?")
    assert p is not None
    assert p["city_key"] == "buenos_aires"
    assert p["direction"] == "below"
    assert abs(p["threshold_f"] - 59.0) < 0.01    # 15C -> 59F


def test_multiword_aliases_resolve():
    for title, key in [
        ("Will the highest temperature in Hong Kong be 33°C or higher on July 22, 2026?", "hong_kong"),
        ("Will the highest temperature in Mexico City be 27°C or higher on July 22, 2026?", "mexico_city"),
        ("Will the highest temperature in Tel Aviv be 34°C or higher on July 22, 2026?", "tel_aviv"),
        ("Will the highest temperature in Los Angeles be 80°F or higher on July 22, 2026?", "los_angeles"),
    ]:
        p = _parse_weather_market_title(title)
        assert p is not None and p["city_key"] == key, (title, p)


def test_unknown_city_returns_none():
    assert _parse_weather_market_title("Will the highest temperature in Atlantis be 80°F or higher?") is None


def test_non_threshold_bucket_rejected():
    # No "or higher/below" edge phrasing -> not a single-threshold market.
    assert _parse_weather_market_title("Highest temperature in New York on July 21, 2026?") is None
