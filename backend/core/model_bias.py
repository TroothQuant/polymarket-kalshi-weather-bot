"""Model-upgrade v1 — nightly per-(city, model) bias table + signal-grading backfill.

Bias = mean(forecast daily-high − ERA5 actual) at the bot's own coords in the GMT
frame (the frame used by research_model_upgrade_groundwork_2026-07-01.md), over a
rolling window. The ensemble API's `past_days` is unreliable (sparse members), so
the window is filled by:
  (a) LOGGING each night's forecast forward into forecast_log, and
  (b) SEEDING gfs_seamless from the existing signals history (its reasoning already
      carries the GFS ensemble mean) so a GFS correction is usable ~immediately.
ECMWF has no history, so its window fills forward (bias 0.0 until n_days >= 20).

Also grades weather signals for calibration (Phase 3) using the same ERA5 actuals —
the old grader only touched *traded* signals AND compared 'yes'/'no' to 'up'/'down'
(never matched → all 0%). This grades the whole scanned population correctly.

Read/HTTP heavy but runs as a nightly sync job (thread executor), never on the
scan hot path. All failures are swallowed so a bad refresh never affects trading.
"""
import json
import logging
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from backend.config import settings
from backend.models.database import SessionLocal, ForecastLog, ModelBias, Signal, Trade
from backend.data.weather import CITY_CONFIG

log = logging.getLogger("trading_bot")

MODELS = tuple(m.strip() for m in settings.WEATHER_MODEL_V2_MODELS.split(",") if m.strip())

_CITY_NAME_TO_KEY = {
    "new york city": "nyc", "new york": "nyc", "nyc": "nyc",
    "chicago": "chicago", "miami": "miami",
    "los angeles": "los_angeles", "denver": "denver",
}
_ENS_RE = re.compile(r"Ensemble:\s*([\d.]+)F")
_HDR_RE = re.compile(r"([A-Za-z][A-Za-z ]*?)\s+high\s+(above|below)\s+([\d.]+)F\s+on\s+(\d{4}-\d{2}-\d{2})")


# ── HTTP (sync; nightly only) ────────────────────────────────────────────────
def _archive_highs(lat, lon, start, end):
    """ERA5 archive daily max (F), GMT day — {date: high}."""
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": start,
                                "end_date": end, "daily": "temperature_2m_max",
                                "temperature_unit": "fahrenheit"})
    with urllib.request.urlopen(f"https://archive-api.open-meteo.com/v1/archive?{q}", timeout=60) as r:
        j = json.load(r)
    days = j.get("daily", {}).get("time", [])
    highs = j.get("daily", {}).get("temperature_2m_max", [])
    return {d: h for d, h in zip(days, highs) if h is not None}


def _model_forecast_high(lat, lon, target_date_iso, model):
    """Mean daily-high forecast for one model/city/date (GMT frame). None on failure."""
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
                                "temperature_unit": "fahrenheit", "start_date": target_date_iso,
                                "end_date": target_date_iso, "models": model})
    with urllib.request.urlopen(f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}", timeout=60) as r:
        j = json.load(r)
    vals = [float(s[0]) for k, s in j.get("daily", {}).items()
            if k != "time" and "temperature_2m_max" in k and s and s[0] is not None]
    return statistics.mean(vals) if vals else None


# ── forecast_log persistence ─────────────────────────────────────────────────
def _upsert_forecast(db, city, model, target_date_iso, high):
    row = (db.query(ForecastLog)
           .filter(ForecastLog.city == city, ForecastLog.model == model,
                   ForecastLog.target_date == target_date_iso).first())
    if row is None:
        db.add(ForecastLog(city=city, model=model, target_date=target_date_iso,
                           forecast_high=high, recorded_at=datetime.utcnow()))
    else:
        row.forecast_high = high
        row.recorded_at = datetime.utcnow()


def log_tomorrow_forecasts(db):
    """Persist tomorrow's per-model forecast high for each city (window fills forward)."""
    tomorrow = (datetime.utcnow().date() + timedelta(days=1)).isoformat()
    for city, cfg in CITY_CONFIG.items():
        for model in MODELS:
            try:
                fh = _model_forecast_high(cfg["lat"], cfg["lon"], tomorrow, model)
                if fh is not None:
                    _upsert_forecast(db, city, model, tomorrow, fh)
            except Exception as e:
                log.warning(f"[model_bias] forecast log failed {city}/{model}: {e}")
    db.commit()


def seed_gfs_from_signals(db, lookback_days=65):
    """One-time-ish: seed gfs_seamless forecast_log from the signals history (its
    reasoning carries the GFS ensemble mean) so GFS bias is usable immediately.
    Idempotent — only inserts (city, target_date) not already logged."""
    since = datetime.utcnow() - timedelta(days=lookback_days)
    latest = {}  # (city, date) -> (ts, mean)
    q = (db.query(Signal)
         .filter(Signal.market_type == "weather", Signal.platform == "polymarket",
                 Signal.timestamp >= since))
    for s in q:
        m = _ENS_RE.search(s.reasoning or "")
        h = _HDR_RE.search(s.reasoning or "")
        if not m or not h:
            continue
        city = _CITY_NAME_TO_KEY.get(h.group(1).strip().lower())
        tdate = h.group(4)
        if not city:
            continue
        key = (city, tdate)
        if key not in latest or s.timestamp > latest[key][0]:
            latest[key] = (s.timestamp, float(m.group(1)))
    n = 0
    for (city, tdate), (_, mean) in latest.items():
        exists = (db.query(ForecastLog)
                  .filter(ForecastLog.city == city, ForecastLog.model == "gfs_seamless",
                          ForecastLog.target_date == tdate).first())
        if exists is None:
            db.add(ForecastLog(city=city, model="gfs_seamless", target_date=tdate,
                               forecast_high=mean, recorded_at=datetime.utcnow()))
            n += 1
    db.commit()
    if n:
        log.info(f"[model_bias] seeded {n} gfs_seamless forecast_log rows from signals history")
    return n


# ── bias computation ─────────────────────────────────────────────────────────
def compute_bias(db, city, model, actuals):
    """(bias_f, n_days) for one (city, model) over the rolling window. bias 0.0 if
    n < WEATHER_MODEL_BIAS_MIN_DAYS; clamped to ±WEATHER_MODEL_BIAS_CLAMP_F."""
    window = settings.WEATHER_MODEL_BIAS_WINDOW_DAYS
    start = (datetime.utcnow().date() - timedelta(days=window)).isoformat()
    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    rows = (db.query(ForecastLog)
            .filter(ForecastLog.city == city, ForecastLog.model == model,
                    ForecastLog.target_date >= start, ForecastLog.target_date <= yesterday).all())
    errs = [r.forecast_high - actuals[city][r.target_date]
            for r in rows if r.target_date in actuals.get(city, {})]
    n = len(errs)
    if n < settings.WEATHER_MODEL_BIAS_MIN_DAYS:
        return 0.0, n
    bias = statistics.mean(errs)
    clamp = settings.WEATHER_MODEL_BIAS_CLAMP_F
    return max(-clamp, min(clamp, bias)), n


def _upsert_bias(db, city, model, bias_f, n_days):
    row = (db.query(ModelBias)
           .filter(ModelBias.city == city, ModelBias.model == model).first())
    if row is None:
        db.add(ModelBias(city=city, model=model, bias_f=bias_f, n_days=n_days,
                         window_days=settings.WEATHER_MODEL_BIAS_WINDOW_DAYS,
                         updated_at=datetime.utcnow()))
    else:
        row.bias_f = bias_f
        row.n_days = n_days
        row.window_days = settings.WEATHER_MODEL_BIAS_WINDOW_DAYS
        row.updated_at = datetime.utcnow()


def get_bias(db, city, model):
    """Read the active bias for (city, model). 0.0 when disabled/absent/under-n."""
    if not settings.WEATHER_MODEL_BIAS_ENABLED:
        return 0.0
    row = (db.query(ModelBias)
           .filter(ModelBias.city == city, ModelBias.model == model).first())
    if row is None or (row.n_days or 0) < settings.WEATHER_MODEL_BIAS_MIN_DAYS:
        return 0.0
    return float(row.bias_f or 0.0)


import time as _time
_bias_cache = {"ts": 0.0, "data": {}}
_BIAS_CACHE_TTL = 300  # 5 min


def get_bias_cached(city, model):
    """Bias lookup for the scan hot path — reads the whole ModelBias table once per
    5 min into memory so the v2 shadow doesn't hit the DB per signal. Honors the
    same enable/min-n rules as get_bias. 0.0 on any failure."""
    if not settings.WEATHER_MODEL_BIAS_ENABLED:
        return 0.0
    now = _time.time()
    if now - _bias_cache["ts"] > _BIAS_CACHE_TTL:
        data = {}
        try:
            db = SessionLocal()
            try:
                for row in db.query(ModelBias).all():
                    if (row.n_days or 0) >= settings.WEATHER_MODEL_BIAS_MIN_DAYS:
                        data[(row.city, row.model)] = float(row.bias_f or 0.0)
            finally:
                db.close()
        except Exception as e:
            log.debug(f"[model_bias] bias cache refresh failed: {e}")
            return 0.0
        _bias_cache["ts"] = now
        _bias_cache["data"] = data
    return _bias_cache["data"].get((city, model), 0.0)


# ── Phase 3: grade weather signals for calibration ───────────────────────────
def grade_weather_signals(db, actuals):
    """Grade polymarket weather signals in the trailing window. Fixes both old bugs:
    grades the FULL scanned population (not just traded), and maps the weather
    'yes'/'no' direction to the outcome correctly.

    Outcome truth, in priority order:
      1. The linked TRADE's settlement_value (AUTHORITATIVE — the market's own NWS
         settlement). This anchors the gate's Brier on correctly-labelled data.
      2. ERA5 archive high at bot coords vs the signal's threshold (PROXY for the
         untraded majority; carries a ~30% label-noise floor vs true settlement from
         the station/local-day mismatch — but it's common to v1 and v2 so the
         relative comparison holds). Idempotent."""
    window = settings.WEATHER_MODEL_BIAS_WINDOW_DAYS
    since = datetime.utcnow() - timedelta(days=window + 5)
    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()

    # signal_id -> authoritative settlement_value from settled trades.
    settled_map = {}
    for t in (db.query(Trade)
              .filter(Trade.market_type == "weather", Trade.platform == "polymarket",
                      Trade.settled == True, Trade.signal_id.isnot(None),  # noqa: E712
                      Trade.settlement_value.isnot(None))):
        settled_map[t.signal_id] = t.settlement_value

    graded = 0
    q = (db.query(Signal)
         .filter(Signal.market_type == "weather", Signal.platform == "polymarket",
                 Signal.timestamp >= since))
    for s in q:
        h = _HDR_RE.search(s.reasoning or "")
        if not h:
            continue
        city = _CITY_NAME_TO_KEY.get(h.group(1).strip().lower())
        thr_dir, threshold, tdate = h.group(2), float(h.group(3)), h.group(4)
        if not city:
            continue
        if s.id in settled_map:
            yes_won = settled_map[s.id] == 1.0        # authoritative market settlement
        else:
            if tdate > yesterday:
                continue
            actual = actuals.get(city, {}).get(tdate)
            if actual is None:
                continue
            yes_won = (actual > threshold) if thr_dir == "above" else (actual < threshold)
        s.settlement_value = 1.0 if yes_won else 0.0
        s.actual_outcome = "up" if yes_won else "down"
        # weather signal.direction is 'yes'/'no'; the model-favored side is correct
        # iff it matches the winning side.
        s.outcome_correct = (s.direction == "yes" and yes_won) or (s.direction == "no" and not yes_won)
        s.settled_at = datetime.utcnow()
        graded += 1
        if graded % 500 == 0:
            db.commit()
    db.commit()
    if graded:
        log.info(f"[model_bias] graded {graded} weather signals (calibration backfill)")
    return graded


# ── nightly entry point ──────────────────────────────────────────────────────
def refresh_all(db=None):
    """Nightly: seed → log forward → fetch actuals → compute bias per (city,model)
    → grade signals → log the table. Fully self-contained; swallows all errors."""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        try:
            seed_gfs_from_signals(db)
        except Exception as e:
            log.warning(f"[model_bias] seed failed: {e}")
        try:
            log_tomorrow_forecasts(db)
        except Exception as e:
            log.warning(f"[model_bias] forward-log failed: {e}")

        window = settings.WEATHER_MODEL_BIAS_WINDOW_DAYS
        start = (datetime.utcnow().date() - timedelta(days=window)).isoformat()
        end = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        actuals = {}
        for city, cfg in CITY_CONFIG.items():
            try:
                actuals[city] = _archive_highs(cfg["lat"], cfg["lon"], start, end)
            except Exception as e:
                log.warning(f"[model_bias] archive fetch failed {city}: {e}")
                actuals[city] = {}

        table = []
        for city in CITY_CONFIG:
            for model in MODELS:
                try:
                    bias, n = compute_bias(db, city, model, actuals)
                    _upsert_bias(db, city, model, bias, n)
                    table.append((city, model, bias, n))
                except Exception as e:
                    log.warning(f"[model_bias] compute failed {city}/{model}: {e}")
        db.commit()

        log.info("[model_bias] refreshed table (bias °F | n_days):")
        for city, model, bias, n in table:
            active = "ACTIVE" if n >= settings.WEATHER_MODEL_BIAS_MIN_DAYS else "cold"
            log.info(f"  {city:12s} {model:14s} bias={bias:+.2f}  n={n:3d}  [{active}]")

        try:
            grade_weather_signals(db, actuals)
        except Exception as e:
            log.warning(f"[model_bias] signal grading failed: {e}")
    finally:
        if own:
            db.close()
