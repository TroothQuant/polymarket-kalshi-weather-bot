import os, sys, asyncio
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.core.weather_signals as ws
from backend.data.weather import EnsembleForecast
from backend.data.weather_markets import WeatherMarket


def _market():
    return WeatherMarket(
        slug="highest-temperature-in-nyc-on-december-31-2099",
        market_id="t1", platform="polymarket", title="t",
        city_key="nyc", city_name="New York City", target_date=date(2099, 12, 31),
        threshold_f=74.0, metric="high", direction="above",
        yes_price=0.65, no_price=0.35,
    )


def _forecast():
    # 9/31 members above 74F -> model YES ~0.29 vs market 0.65 -> NO edge ~0.36
    # (in [0.25,0.50]); mean_high ~73.2, std ~1.85 -> conviction z ~0.45.
    return EnsembleForecast(
        city_key="nyc", city_name="New York City", target_date=date(2099, 12, 31),
        member_highs=[72.0] * 22 + [76.0] * 9, member_lows=[60.0] * 31)


def _gen(monkeypatch, floor):
    monkeypatch.setattr(ws.settings, "WEATHER_MIN_CONVICTION_Z", floor)
    fc = _forecast()
    async def fake_fetch(city_key, target_date):
        return fc
    monkeypatch.setattr(ws, "fetch_ensemble_forecast", fake_fetch)
    return asyncio.run(ws.generate_weather_signal(_market(), current_bankroll=1000.0))


def test_noop_at_zero_stays_actionable(monkeypatch):
    sig = _gen(monkeypatch, 0.0)               # default value = no-op
    assert sig is not None
    assert "[ACTIONABLE]" in sig.reasoning      # still actionable, unchanged
    assert "Conviction z:" in sig.reasoning     # z logged on every signal
    assert "conviction z=" not in sig.reasoning  # gate filter-note did NOT fire


def test_floor_two_filters_low_z(monkeypatch):
    sig = _gen(monkeypatch, 2.0)               # armed
    assert sig is not None
    assert "[FILTERED]" in sig.reasoning        # low-z signal filtered out
    assert "conviction z=" in sig.reasoning     # the gate note fired
    assert "< 2.0 floor" in sig.reasoning
