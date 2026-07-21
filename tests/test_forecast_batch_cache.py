"""Batched forecast guard + negative cache (2026-07-21 coverage pass).
Covers model_bias._model_forecast_highs_batched (lat+lon order guard, count
mismatch) and weather.fetch_ensemble_forecast negative-cache short-circuit +
set-on-failure. No real network — urlopen/AsyncClient monkeypatched."""
import os, sys, json, time, asyncio
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backend.core.model_bias as mb
import backend.data.weather as weather


class _FakeResp:
    def __init__(self, payload): self._s = json.dumps(payload)
    def read(self, *a): return self._s
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _loc(lat, lon, members):
    daily = {"time": ["2026-07-22"]}
    daily["temperature_2m_max"] = [members[0]]
    for i, v in enumerate(members[1:], 1):
        daily[f"temperature_2m_max_member{i:02d}"] = [v]
    return {"latitude": lat, "longitude": lon, "daily": daily}


def test_batched_highs_correct(monkeypatch):
    cities = [("nyc", 40.71, -74.01), ("miami", 25.76, -80.19)]
    payload = [_loc(40.75, -74.0, [80.0, 82.0, 84.0]), _loc(25.75, -80.25, [90.0, 92.0])]
    monkeypatch.setattr(mb.urllib.request, "urlopen", lambda url, timeout=0: _FakeResp(payload))
    out = mb._model_forecast_highs_batched(cities, "2026-07-22", "gfs_seamless")
    assert out["nyc"] == 82.0        # mean(80,82,84)
    assert out["miami"] == 91.0      # mean(90,92)


def test_batched_highs_latlon_guard_drops_mismatch(monkeypatch):
    cities = [("nyc", 40.71, -74.01), ("miami", 25.76, -80.19)]
    # first location's latitude is way off -> guard must drop nyc, keep miami
    payload = [_loc(50.0, -74.0, [80.0, 82.0]), _loc(25.75, -80.25, [90.0, 92.0])]
    monkeypatch.setattr(mb.urllib.request, "urlopen", lambda url, timeout=0: _FakeResp(payload))
    out = mb._model_forecast_highs_batched(cities, "2026-07-22", "gfs_seamless")
    assert "nyc" not in out
    assert out["miami"] == 91.0


def test_batched_highs_count_mismatch_returns_empty(monkeypatch):
    cities = [("nyc", 40.71, -74.01), ("miami", 25.76, -80.19)]
    payload = [_loc(40.75, -74.0, [80.0])]   # only 1 loc for 2 cities
    monkeypatch.setattr(mb.urllib.request, "urlopen", lambda url, timeout=0: _FakeResp(payload))
    assert mb._model_forecast_highs_batched(cities, "2026-07-22", "gfs_seamless") == {}


def test_negcache_short_circuits_without_network(monkeypatch):
    calls = []
    def fake_client(*a, **k):
        calls.append(1); raise RuntimeError("network")
    monkeypatch.setattr(weather.httpx, "AsyncClient", fake_client)
    key = "nyc_2099-01-01"
    weather._neg_cache[key] = time.time()          # recently failed
    weather._forecast_cache.pop(key, None)
    res = asyncio.run(weather.fetch_ensemble_forecast("nyc", date(2099, 1, 1)))
    assert res is None
    assert calls == []                              # never touched the network


def test_failure_sets_negcache(monkeypatch):
    key = "nyc_2099-01-02"
    weather._neg_cache.pop(key, None)
    weather._forecast_cache.pop(key, None)
    monkeypatch.setattr(weather.httpx, "AsyncClient",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = asyncio.run(weather.fetch_ensemble_forecast("nyc", date(2099, 1, 2)))
    assert res is None
    assert key in weather._neg_cache                # failure recorded for cooldown
