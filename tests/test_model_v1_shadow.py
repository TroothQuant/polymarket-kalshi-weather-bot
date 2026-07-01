"""Tests for the model-upgrade v1 shadow build (2026-07-01):
  - equal-MODEL-weight pooling math (ECMWF's members can't swamp GFS)
  - bias clamp + n<20 fallback in compute_bias
  - v1 dispatch extraction is faithful (byte-identical v1)
  - v2 shadow failure isolation (fetch fail / single model → NULLs, never raises)
  - ensure_schema idempotence (Signal v2 cols + model_bias/forecast_log tables)
"""
import asyncio
import types
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from backend.data.weather import PooledForecast, EnsembleForecast
from backend.core.weather_signals import _model_yes_prob, _compute_v2_shadow
from backend.core import model_bias
from backend.models import database as db_mod


# ── pooling math ─────────────────────────────────────────────────────────────
def test_pooled_equal_model_weight_not_member_weight():
    # GFS 10 members, 8 above 90 → 0.8. ECMWF 100 members, 20 above 90 → 0.2.
    highs = {"gfs_seamless": [91] * 8 + [89] * 2,
             "ecmwf_ifs025": [91] * 20 + [89] * 80}
    p = PooledForecast("nyc", "NYC", date(2026, 7, 2), highs,
                       {"gfs_seamless": [], "ecmwf_ifs025": []})
    # equal MODEL weight: 0.5*0.8 + 0.5*0.2 = 0.5
    assert abs(p.probability_high_above(90) - 0.5) < 1e-9
    # member-weighted would be 28/110 ≈ 0.255 — we must NOT be that
    assert abs(p.probability_high_above(90) - 28 / 110) > 0.2
    # below is the complement
    assert abs(p.probability_high_below(90) - 0.5) < 1e-9


def test_pooled_single_model_present_is_that_model():
    highs = {"gfs_seamless": [91] * 3 + [89]}   # 0.75 above 90
    p = PooledForecast("nyc", "NYC", date(2026, 7, 2), highs, {})
    assert abs(p.probability_high_above(90) - 0.75) < 1e-9


# ── v1 dispatch extraction is faithful ───────────────────────────────────────
def _mkt(**over):
    d = dict(strike_type=None, metric="high", direction="above", threshold_f=90.0,
             platform="polymarket", floor_strike=None, cap_strike=None)
    d.update(over)
    return types.SimpleNamespace(**d)


def test_v1_dispatch_matches_direct_methods():
    f = EnsembleForecast("nyc", "NYC", date(2026, 7, 2),
                         member_highs=[85, 88, 90, 92, 95], member_lows=[70] * 5)
    assert _model_yes_prob(f, _mkt(direction="above")) == f.probability_high_above(90.0)
    assert _model_yes_prob(f, _mkt(direction="below")) == f.probability_high_below(90.0)
    # kalshi 'between' with the +1 cap shift
    m = _mkt(platform="kalshi", strike_type="between", floor_strike=89.0, cap_strike=90.0)
    assert _model_yes_prob(f, m) == f.probability_high_between(89.0, 91.0)


# ── compute_bias clamp + n<20 fallback ───────────────────────────────────────
def _mem_db():
    eng = create_engine("sqlite:///:memory:")
    db_mod.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_compute_bias_clamp_and_n_fallback(monkeypatch):
    monkeypatch.setattr(db_mod.settings, "WEATHER_MODEL_BIAS_MIN_DAYS", 20)
    monkeypatch.setattr(db_mod.settings, "WEATHER_MODEL_BIAS_WINDOW_DAYS", 30)
    monkeypatch.setattr(db_mod.settings, "WEATHER_MODEL_BIAS_CLAMP_F", 5.0)
    db = _mem_db()
    actuals = {"nyc": {}, "miami": {}}
    today = datetime.utcnow().date()
    # nyc: 25 days, forecast − actual = +10 everywhere → mean 10 → clamp to +5
    for i in range(1, 26):
        d = (today - timedelta(days=i)).isoformat()
        db.add(db_mod.ForecastLog(city="nyc", model="gfs_seamless", target_date=d, forecast_high=100.0))
        actuals["nyc"][d] = 90.0
    # miami: only 10 days → n<20 → bias 0.0 regardless of error
    for i in range(1, 11):
        d = (today - timedelta(days=i)).isoformat()
        db.add(db_mod.ForecastLog(city="miami", model="gfs_seamless", target_date=d, forecast_high=100.0))
        actuals["miami"][d] = 90.0
    db.commit()

    bias, n = model_bias.compute_bias(db, "nyc", "gfs_seamless", actuals)
    assert n == 25 and bias == 5.0            # clamped from +10 to +5
    bias2, n2 = model_bias.compute_bias(db, "miami", "gfs_seamless", actuals)
    assert n2 == 10 and bias2 == 0.0          # under min-days → uncorrected


# ── v2 shadow failure isolation ──────────────────────────────────────────────
def test_v2_shadow_none_on_fetch_failure(monkeypatch):
    async def fail(*a, **k):
        return None
    monkeypatch.setattr("backend.data.weather.fetch_multimodel_forecast", fail)
    m = _mkt()
    m.city_key = "nyc"; m.city_name = "NYC"; m.target_date = date(2026, 7, 2)
    assert asyncio.run(_compute_v2_shadow(m)) == (None, None, None, None)


def test_v2_shadow_none_when_single_model(monkeypatch):
    async def one_model(*a, **k):
        return {"highs": {"gfs_seamless": [91, 92], "ecmwf_ifs025": []},
                "lows": {"gfs_seamless": [70, 71], "ecmwf_ifs025": []}}
    monkeypatch.setattr("backend.data.weather.fetch_multimodel_forecast", one_model)
    m = _mkt()
    m.city_key = "nyc"; m.city_name = "NYC"; m.target_date = date(2026, 7, 2)
    assert asyncio.run(_compute_v2_shadow(m)) == (None, None, None, None)


def test_v2_shadow_applies_bias_correction(monkeypatch):
    async def both(*a, **k):
        return {"highs": {"gfs_seamless": [91] * 10, "ecmwf_ifs025": [91] * 10},
                "lows": {"gfs_seamless": [70] * 10, "ecmwf_ifs025": [70] * 10}}
    monkeypatch.setattr("backend.data.weather.fetch_multimodel_forecast", both)
    # +2°F bias on both models → corrected members = 89 → below the 90 threshold,
    # so P(high>90) collapses from 1.0 (raw) toward 0.05 (clipped).
    monkeypatch.setattr("backend.core.model_bias.get_bias_cached", lambda c, mdl: 2.0)
    m = _mkt(direction="above", threshold_f=90.0)
    m.city_key = "nyc"; m.city_name = "NYC"; m.target_date = date(2026, 7, 2)
    prob, mean_v2, std_v2, bias_json = asyncio.run(_compute_v2_shadow(m))
    assert prob == 0.05                        # all corrected members below 90 → clipped
    assert abs(mean_v2 - 89.0) < 1e-9          # 91 − 2 bias
    assert '"gfs_seamless": 2.0' in bias_json


# ── ensure_schema idempotence ────────────────────────────────────────────────
def test_ensure_schema_idempotent(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db_mod, "engine", eng)
    db_mod.Base.metadata.create_all(eng)
    db_mod.ensure_schema()
    db_mod.ensure_schema()   # twice must not raise
    insp = sa_inspect(eng)
    sig_cols = {c["name"] for c in insp.get_columns("signals")}
    assert {"model_probability_v2", "ensemble_mean_v2", "ensemble_std_v2",
            "bias_applied_json"} <= sig_cols
    assert "model_bias" in insp.get_table_names()
    assert "forecast_log" in insp.get_table_names()
