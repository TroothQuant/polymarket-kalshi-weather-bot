"""Weather data fetcher using Open-Meteo Ensemble API and NWS observations."""
import httpx
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
import statistics
import time

logger = logging.getLogger("trading_bot")

# City configurations with lat/lon and NWS station identifiers
CITY_CONFIG: Dict[str, dict] = {
    "nyc": {
        "name": "New York City",
        "lat": 40.7128,
        "lon": -74.0060,
        "nws_station": "KNYC",
        "nws_office": "OKX",
        "nws_gridpoint": "OKX/33,37",
    },
    "chicago": {
        "name": "Chicago",
        "lat": 41.8781,
        "lon": -87.6298,
        "nws_station": "KORD",
        "nws_office": "LOT",
        "nws_gridpoint": "LOT/75,72",
    },
    "miami": {
        "name": "Miami",
        "lat": 25.7617,
        "lon": -80.1918,
        "nws_station": "KMIA",
        "nws_office": "MFL",
        "nws_gridpoint": "MFL/75,53",
    },
    # Coords match KLAX (NWS settlement station for Polymarket).
    # Previously used downtown 34.0522/-118.2437 — 9.6°F too warm vs the
    # actual settlement location. See weather_source_mismatch_analysis_2026-05-29.md.
    "los_angeles": {
        "name": "Los Angeles",
        "lat": 33.9425,
        "lon": -118.4081,
        "nws_station": "KLAX",
        "nws_office": "LOX",
        "nws_gridpoint": "LOX/154,44",
    },
    "denver": {
        "name": "Denver",
        "lat": 39.7392,
        "lon": -104.9903,
        "nws_station": "KDEN",
        "nws_office": "BOU",
        "nws_gridpoint": "BOU/62,60",
    },
    # ── 6 gated stations (added 2026-07-20 after the station-bias backtest) ──
    # coords = the EXACT Polymarket settlement airport (verified from each
    # market's resolutionSource); forecasting reads lat/lon so it anchors to the
    # settlement point. nws_station holds the ICAO for reference (cosmetic).
    # Intl markets quote °C (parser converts →°F). Per-station backtest bias is
    # seeded in model_bias.STATION_BIAS_SEED_F.
    "san_francisco": {
        "name": "San Francisco", "lat": 37.6213, "lon": -122.3790, "nws_station": "KSFO",
    },
    "toronto": {
        "name": "Toronto", "lat": 43.6772, "lon": -79.6306, "nws_station": "CYYZ",
    },
    "london": {
        "name": "London", "lat": 51.5053, "lon": 0.0553, "nws_station": "EGLC",
    },
    "milan": {
        "name": "Milan", "lat": 45.6306, "lon": 8.7281, "nws_station": "LIMC",
    },
    "jeddah": {
        "name": "Jeddah", "lat": 21.6796, "lon": 39.1565, "nws_station": "OEJN",
    },
    "wuhan": {
        "name": "Wuhan", "lat": 30.7783, "lon": 114.2081, "nws_station": "ZHHH",
    },
}


@dataclass
class EnsembleForecast:
    """Ensemble weather forecast with per-member data."""
    city_key: str
    city_name: str
    target_date: date
    member_highs: List[float]  # Daily max temps (F) per ensemble member
    member_lows: List[float]   # Daily min temps (F) per ensemble member
    mean_high: float = 0.0
    std_high: float = 0.0
    mean_low: float = 0.0
    std_low: float = 0.0
    num_members: int = 0
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if self.member_highs:
            self.mean_high = statistics.mean(self.member_highs)
            self.std_high = statistics.stdev(self.member_highs) if len(self.member_highs) > 1 else 0.0
            self.num_members = len(self.member_highs)
        if self.member_lows:
            self.mean_low = statistics.mean(self.member_lows)
            self.std_low = statistics.stdev(self.member_lows) if len(self.member_lows) > 1 else 0.0

    def probability_high_above(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily high above threshold."""
        if not self.member_highs:
            return 0.5
        count = sum(1 for h in self.member_highs if h > threshold_f)
        return count / len(self.member_highs)

    def probability_high_below(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily high below threshold."""
        return 1.0 - self.probability_high_above(threshold_f)

    def probability_high_between(self, floor_f: float, cap_f: float) -> float:
        """Fraction of ensemble members with daily high in [floor, cap).

        Added 2026-05-20 for Kalshi narrow-bucket markets, which are
        "high is in this 1-2°F range" rather than cumulative thresholds.
        Half-open interval matches Kalshi's bucket convention.
        """
        if not self.member_highs:
            return 0.5
        count = sum(1 for h in self.member_highs if floor_f <= h < cap_f)
        return count / len(self.member_highs)

    def probability_low_above(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily low above threshold."""
        if not self.member_lows:
            return 0.5
        count = sum(1 for l in self.member_lows if l > threshold_f)
        return count / len(self.member_lows)

    def probability_low_below(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily low below threshold."""
        return 1.0 - self.probability_low_above(threshold_f)

    def probability_low_between(self, floor_f: float, cap_f: float) -> float:
        """Fraction of ensemble members with daily low in [floor, cap)."""
        if not self.member_lows:
            return 0.5
        count = sum(1 for l in self.member_lows if floor_f <= l < cap_f)
        return count / len(self.member_lows)

    @property
    def ensemble_agreement(self) -> float:
        """How one-sided the ensemble is (0.5 = split, 1.0 = unanimous)."""
        if not self.member_highs:
            return 0.5
        median = statistics.median(self.member_highs)
        above = sum(1 for h in self.member_highs if h > median)
        frac = above / len(self.member_highs)
        return max(frac, 1 - frac)


class PooledForecast:
    """Model-upgrade v1 (2026-07-01): equal-MODEL-weight pooled forecast with
    per-model bias correction. Mirrors EnsembleForecast's probability_* interface,
    but each probability is
        0.5 * frac(GFS corrected members) + 0.5 * frac(ECMWF corrected members)
    so ECMWF's ~51 members can't swamp GFS's ~31 — each model votes as a block.
    Members are bias-corrected BEFORE pooling: corrected = raw − bias_f(city, model).
    """

    def __init__(self, city_key, city_name, target_date, highs_by_model, lows_by_model):
        self.city_key = city_key
        self.city_name = city_name
        self.target_date = target_date
        self.highs_by_model = {m: v for m, v in highs_by_model.items() if v}
        self.lows_by_model = {m: v for m, v in lows_by_model.items() if v}
        self._high_models = list(self.highs_by_model)
        self._low_models = list(self.lows_by_model)
        # Display stats: equal-model-weight mean (mean of per-model means); std +
        # member count over the member union (rough dispersion for the log/columns).
        hmeans = [statistics.mean(v) for v in self.highs_by_model.values()]
        union_h = [x for v in self.highs_by_model.values() for x in v]
        self.mean_high = statistics.mean(hmeans) if hmeans else 0.0
        self.std_high = statistics.pstdev(union_h) if len(union_h) > 1 else 0.0
        lmeans = [statistics.mean(v) for v in self.lows_by_model.values()]
        union_l = [x for v in self.lows_by_model.values() for x in v]
        self.mean_low = statistics.mean(lmeans) if lmeans else 0.0
        self.std_low = statistics.pstdev(union_l) if len(union_l) > 1 else 0.0
        self.num_members = len(union_h)
        # v1-compat attributes (some callers read these directly).
        self.member_highs = union_h
        self.member_lows = union_l

    @staticmethod
    def _pooled(by_model, models, pred):
        if not models:
            return 0.5
        fracs = [sum(1 for x in by_model[m] if pred(x)) / len(by_model[m]) for m in models]
        return sum(fracs) / len(fracs)   # EQUAL model weight

    def probability_high_above(self, threshold_f: float) -> float:
        return self._pooled(self.highs_by_model, self._high_models, lambda h: h > threshold_f)

    def probability_high_below(self, threshold_f: float) -> float:
        return 1.0 - self.probability_high_above(threshold_f)

    def probability_high_between(self, floor_f: float, cap_f: float) -> float:
        return self._pooled(self.highs_by_model, self._high_models, lambda h: floor_f <= h < cap_f)

    def probability_low_above(self, threshold_f: float) -> float:
        return self._pooled(self.lows_by_model, self._low_models, lambda l: l > threshold_f)

    def probability_low_below(self, threshold_f: float) -> float:
        return 1.0 - self.probability_low_above(threshold_f)

    def probability_low_between(self, floor_f: float, cap_f: float) -> float:
        return self._pooled(self.lows_by_model, self._low_models, lambda l: floor_f <= l < cap_f)


# Simple cache: (city_key, target_date_str) -> (timestamp, EnsembleForecast)
_forecast_cache: Dict[str, tuple] = {}
_CACHE_TTL = 3600  # 60 min (raised 2026-07-20: cut Open-Meteo 429s after 5->11 city expansion)
_NEG_TTL = 180  # 3-min cooldown after a failed/429 forecast fetch, so a
# rate-limited (city,date) is not re-hammered by every market in the scan
_neg_cache: Dict[str, float] = {}
# Multi-model (v2 shadow) cache — separate so v1's cache is untouched.
_multimodel_cache: Dict[str, tuple] = {}


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


async def fetch_ensemble_forecast(city_key: str, target_date: Optional[date] = None) -> Optional[EnsembleForecast]:
    """
    Fetch ensemble forecast from Open-Meteo Ensemble API (free, 31-member GFS).
    Returns per-member daily max/min temperatures in Fahrenheit.
    """
    if city_key not in CITY_CONFIG:
        logger.warning(f"Unknown city key: {city_key}")
        return None

    if target_date is None:
        target_date = date.today()

    cache_key = f"{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _forecast_cache:
        cached_time, cached_forecast = _forecast_cache[cache_key]
        if now - cached_time < _CACHE_TTL:
            return cached_forecast
    neg_t = _neg_cache.get(cache_key)
    if neg_t is not None and now - neg_t < _NEG_TTL:
        return None  # recently failed — cool down instead of re-hitting the API

    city = CITY_CONFIG[city_key]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Open-Meteo Ensemble API — GFS ensemble with 31 members
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "models": "gfs_seamless",
            }

            response = await client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            daily = data.get("daily", {})

            # Open-Meteo returns each ensemble member as a separate key:
            #   temperature_2m_max (control), temperature_2m_max_member01, ..., _member30
            # Collect all member values for highs and lows
            member_highs = []
            member_lows = []

            for key, values in daily.items():
                if not isinstance(values, list) or not values:
                    continue
                val = values[0]
                if val is None:
                    continue
                if "temperature_2m_max" in key:
                    member_highs.append(float(val))
                elif "temperature_2m_min" in key:
                    member_lows.append(float(val))

            if not member_highs:
                logger.warning(f"No ensemble data for {city_key} on {target_date}")
                return None

            forecast = EnsembleForecast(
                city_key=city_key,
                city_name=city["name"],
                target_date=target_date,
                member_highs=member_highs,
                member_lows=member_lows,
            )

            _forecast_cache[cache_key] = (now, forecast)
            logger.info(f"Ensemble forecast for {city['name']} on {target_date}: "
                        f"High {forecast.mean_high:.1f}F +/- {forecast.std_high:.1f}F "
                        f"({forecast.num_members} members)")

            return forecast

    except Exception as e:
        _neg_cache[cache_key] = now
        logger.warning(f"Failed to fetch ensemble forecast for {city_key}: {e}")
        return None


async def fetch_multimodel_forecast(city_key: str, target_date: Optional[date] = None,
                                    models: Optional[List[str]] = None) -> Optional[dict]:
    """Model-upgrade v1 (2026-07-01): fetch per-member daily max/min for MULTIPLE
    models in ONE call, returning {"highs": {model: [F]}, "lows": {model: [F]}} —
    or None on failure.

    Used ONLY by the v2 SHADOW path; fetch_ensemble_forecast (v1, GFS-only) is left
    completely untouched so v1 trading stays byte-identical (a deliberate isolation
    choice — the extra cached call is cheap insurance for the live benchmark).

    Key routing: open-meteo leaves the FIRST requested model's variables un-suffixed
    and suffixes the rest, so a key belongs to a later model iff its model id appears
    in the key; otherwise it is the first model.
    """
    if city_key not in CITY_CONFIG:
        return None
    if target_date is None:
        target_date = date.today()
    if models is None:
        models = ["gfs_seamless", "ecmwf_ifs025"]

    cache_key = f"{city_key}_{target_date.isoformat()}_{','.join(models)}"
    now = time.time()
    if cache_key in _multimodel_cache:
        cached_time, cached = _multimodel_cache[cache_key]
        if now - cached_time < _CACHE_TTL:
            return cached

    city = CITY_CONFIG[city_key]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params={
                    "latitude": city["lat"], "longitude": city["lon"],
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": "fahrenheit",
                    "start_date": target_date.isoformat(),
                    "end_date": target_date.isoformat(),
                    "models": ",".join(models),
                },
            )
            resp.raise_for_status()
            daily = resp.json().get("daily", {})

        highs = {m: [] for m in models}
        lows = {m: [] for m in models}
        rest = models[1:]  # later models are suffixed; first model is bare
        for key, values in daily.items():
            if key == "time" or not isinstance(values, list) or not values:
                continue
            val = values[0]
            if val is None:
                continue
            model = next((m for m in rest if m in key), models[0])
            if "temperature_2m_max" in key:
                highs[model].append(float(val))
            elif "temperature_2m_min" in key:
                lows[model].append(float(val))

        # Require the primary (first) model to have members — else this is useless.
        if not highs.get(models[0]):
            logger.warning(f"Multi-model fetch for {city_key}: primary model {models[0]} empty")
            return None
        result = {"highs": highs, "lows": lows}
        _multimodel_cache[cache_key] = (now, result)
        return result
    except Exception as e:
        logger.warning(f"Multi-model fetch failed for {city_key}: {e}")
        return None


async def fetch_nws_observed_temperature(city_key: str, target_date: Optional[date] = None) -> Optional[Dict[str, float]]:
    """
    Fetch observed temperature from NWS API for settlement.
    Returns dict with 'high' and 'low' in Fahrenheit, or None if not available.
    """
    if city_key not in CITY_CONFIG:
        return None

    city = CITY_CONFIG[city_key]
    if target_date is None:
        target_date = date.today()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # NWS observations endpoint
            station = city["nws_station"]
            url = f"https://api.weather.gov/stations/{station}/observations"
            headers = {"User-Agent": "(trading-bot, contact@example.com)"}

            # Get observations for the target date
            start = datetime.combine(target_date, datetime.min.time()).isoformat() + "Z"
            end = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).isoformat() + "Z"

            response = await client.get(url, params={"start": start, "end": end}, headers=headers)
            response.raise_for_status()
            data = response.json()

            features = data.get("features", [])
            if not features:
                return None

            temps = []
            for obs in features:
                props = obs.get("properties", {})
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temps.append(_celsius_to_fahrenheit(temp_c))

            if not temps:
                return None

            return {
                "high": max(temps),
                "low": min(temps),
            }

    except Exception as e:
        logger.warning(f"Failed to fetch NWS observations for {city_key}: {e}")
        return None


async def prefetch_v1_forecasts_batched(pairs) -> None:
    """Warm _forecast_cache for many (city_key, target_date) pairs using ONE
    Open-Meteo call PER DATE (all cities comma-batched) instead of one call per
    city/market. Cuts a scan's forecast calls from ~(cities*dates*markets) to
    ~(dates), which keeps the server IP under Open-Meteo's per-IP rate limit
    after the 5->11 city expansion (2026-07-20).

    Best-effort and side-effect-only: it populates the SAME cache keys that
    fetch_ensemble_forecast() reads, so all existing call sites are unchanged and
    any failure simply leaves the per-city fetch to run as the fallback. An
    order/identity guard (returned latitude must match the requested city) blocks
    the one dangerous failure mode — assigning a city the wrong location's data.
    """
    now = time.time()
    by_date: Dict[date, list] = {}
    for city_key, target_date in pairs:
        if city_key not in CITY_CONFIG:
            continue
        if target_date is None:
            target_date = date.today()
        cache_key = f"{city_key}_{target_date.isoformat()}"
        cached = _forecast_cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL:
            continue  # already warm
        by_date.setdefault(target_date, [])
        if city_key not in by_date[target_date]:
            by_date[target_date].append(city_key)

    for target_date, cities in by_date.items():
        if not cities:
            continue
        lats = ",".join(str(CITY_CONFIG[c]["lat"]) for c in cities)
        lons = ",".join(str(CITY_CONFIG[c]["lon"]) for c in cities)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    "https://ensemble-api.open-meteo.com/v1/ensemble",
                    params={
                        "latitude": lats, "longitude": lons,
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "temperature_unit": "fahrenheit",
                        "start_date": target_date.isoformat(),
                        "end_date": target_date.isoformat(),
                        "models": "gfs_seamless",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as e:
            # Cool down every city in this failed batch so the per-city fallback
            # doesn't immediately re-hammer the same rate-limited endpoint.
            for _c in cities:
                _neg_cache[f"{_c}_{target_date.isoformat()}"] = now
            logger.warning(f"Batched forecast prefetch failed for {target_date} "
                           f"({len(cities)} cities): {e}")
            continue

        if isinstance(payload, dict):
            payload = [payload]  # single-city responses come back as an object
        if not isinstance(payload, list) or len(payload) != len(cities):
            logger.warning(f"Batched forecast prefetch: response count mismatch for "
                           f"{target_date} (got {len(payload) if isinstance(payload, list) else 'n/a'}, "
                           f"want {len(cities)}); leaving per-city fallback")
            continue

        warmed = 0
        for city_key, loc in zip(cities, payload):
            loc = loc or {}
            city = CITY_CONFIG[city_key]
            resp_lat = loc.get("latitude")
            # Order/identity guard: the API preserves request order, but never
            # trust a mislabelled row into the cache — that would mis-forecast a city.
            if resp_lat is None or abs(float(resp_lat) - float(city["lat"])) > 0.5:
                logger.warning(f"Batched prefetch: latitude guard tripped for {city_key} "
                               f"(resp {resp_lat} vs {city['lat']}); skipping, per-city fallback")
                continue
            daily = loc.get("daily", {})
            member_highs, member_lows = [], []
            for key, values in daily.items():
                if not isinstance(values, list) or not values:
                    continue
                val = values[0]
                if val is None:
                    continue
                if "temperature_2m_max" in key:
                    member_highs.append(float(val))
                elif "temperature_2m_min" in key:
                    member_lows.append(float(val))
            if not member_highs:
                continue
            forecast = EnsembleForecast(
                city_key=city_key, city_name=city["name"], target_date=target_date,
                member_highs=member_highs, member_lows=member_lows,
            )
            _forecast_cache[f"{city_key}_{target_date.isoformat()}"] = (now, forecast)
            warmed += 1
        logger.info(f"Batched forecast prefetch: warmed {warmed}/{len(cities)} cities "
                    f"for {target_date} in 1 call")
