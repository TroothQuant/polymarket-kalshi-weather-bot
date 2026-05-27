"""NOAA GEFS ensemble hindcast adapter.

Pulls historical GEFS 0.25° ensemble forecasts (control + 30 perturbed
members = 31 total) from NOAA's Open Data S3 mirror and computes a
per-city daily-max temperature distribution.

DATA SOURCE
    https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/HH/atmos/pgrb2sp25/
        gec00.t00z.pgrb2s.0p25.fXXX      (control)
        gepNN.t00z.pgrb2s.0p25.fXXX      (perturbed, NN=01..30)

    Public-read S3 bucket — no AWS account or request signing required.
    Multi-year retention (probed 12-day-old data on 2026-05-27, got 200).
    Same GRIB2 format and .idx byte-offset scheme as NOMADS prod, but
    with sane long-term retention (NOMADS prod is real-time only, ~2
    days; see operational-gotchas memory entry #8).

WHY S3, NOT NOMADS
    The original 5/23 discovery probe targeted nomads.ncep.noaa.gov.
    That worked for current-day data but didn't probe retention. On
    2026-05-27 we tried to backtest 12-day-old trades and discovered
    NOMADS rotates after ~2 days. The S3 mirror has multi-year retention.

OPTIMIZATION
    Per (date, city), the adapter does 31 members × len(fcst_hours)
    GRIB-field fetches. Each fetch:
      1. GET <url>.idx                  (~10 KB, plain text byte-offsets)
      2. Parse to find TMAX:2 m above ground row
      3. GET <url> with Range: bytes=START-END  (~400-750 KB)
      4. Decode via xr.open_dataset(..., engine="cfgrib")
      5. Extract single grid point via .sel(method="nearest")
    Fetches are parallelized via a thread pool (default 16 workers).
    A per-process disk cache at /tmp/gefs_hindcast_cache/ skips repeat
    (date, city) work across harness reruns.

USAGE
    from nomads_gfs_hindcast import fetch_gefs_ensemble_hindcast
    result = fetch_gefs_ensemble_hindcast("2026-05-15", "Chicago")
    # {"members": [31 floats °F], "control": 70.03, "spread_std": 1.42}
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import numpy as np
import xarray as xr

log = logging.getLogger("nomads_gfs_hindcast")

S3_HOST = "https://noaa-gefs-pds.s3.amazonaws.com"
USER_AGENT = "trooth-weather-bot/backtest"
HTTP_TIMEOUT = 30.0
HTTP_RETRY_DELAY_SEC = 60.0
MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]   # 31 members

# Same coords the live bot uses (backend/data/weather.py CITY_CONFIG)
CITY_COORDS = {
    "Chicago":       (41.8781, -87.6298),
    "Denver":        (39.7392, -104.9903),
    "Miami":         (25.7617, -80.1918),
    "Los Angeles":   (34.0522, -118.2437),
    "New York City": (40.7128, -74.0060),
}

CACHE_DIR = "/tmp/gefs_hindcast_cache"


@dataclass
class EnsembleResult:
    members: list[float]      # daily max °F per member (length 31)
    control: float            # daily max °F for the control (gec00)
    spread_std: float         # numpy std across members
    n_members_used: int       # may be < 31 if some failed


# ── Low-level helpers ──────────────────────────────────────────────────────

def _http_get(url: str, range_hdr: Optional[str] = None) -> bytes:
    """Single HTTP GET with optional Range. Raises on non-2xx."""
    headers = {"User-Agent": USER_AGENT}
    if range_hdr:
        headers["Range"] = range_hdr
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read()


def _parse_idx_for_tmax(idx_text: str) -> tuple[int, Optional[int]]:
    """Find TMAX:2 m above ground in the .idx and return (start_byte, end_byte).
    end_byte is None for the last record."""
    lines = idx_text.strip().split("\n")
    parsed = []
    for ln in lines:
        if not ln or ":" not in ln:
            continue
        n, off, rest = ln.split(":", 2)
        try:
            parsed.append((int(n), int(off), rest))
        except ValueError:
            continue
    for i, (_, start, rest) in enumerate(parsed):
        if "TMAX:2 m above ground" in rest:
            end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else None
            return (start, end)
    raise RuntimeError("TMAX:2 m above ground not found in .idx")


def _build_url(date_yyyymmdd: str, member: str, fcst_hour: int) -> str:
    return (
        f"{S3_HOST}/gefs.{date_yyyymmdd}/00/atmos/pgrb2sp25/"
        f"{member}.t00z.pgrb2s.0p25.f{fcst_hour:03d}"
    )


def _fetch_one_field_tmax_kelvin(
    date_yyyymmdd: str, member: str, fcst_hour: int, lat: float, lon_0_360: float
) -> float:
    """Fetch the TMAX:2 m above ground value for a single (member, fcst_hour)
    at a single grid point. Returns temperature in Kelvin."""
    grib_url = _build_url(date_yyyymmdd, member, fcst_hour)
    idx_text = _http_get(grib_url + ".idx").decode()
    start, end = _parse_idx_for_tmax(idx_text)
    range_hdr = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    blob = _http_get(grib_url, range_hdr=range_hdr)
    # cfgrib needs a real filesystem path — write to a tempfile, decode, delete
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(blob)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            val = float(ds["tmax"].sel(latitude=lat, longitude=lon_0_360, method="nearest").values)
        finally:
            ds.close()
        return val  # Kelvin
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Public API ─────────────────────────────────────────────────────────────

def fetch_gefs_ensemble_hindcast(
    date: str,                           # YYYY-MM-DD
    city: str,                           # "Chicago" | "Denver" | "Miami" | "Los Angeles" | "New York City"
    fcst_hours: list[int] = [18, 24, 30],
    max_workers: int = 16,
    cache: bool = True,
) -> Optional[EnsembleResult]:
    """Return ensemble daily-max distribution for (date, city), or None on 404.

    Daily max per member = element-wise max across the requested forecast
    hours (default f018, f024, f030 covers the afternoon peak for the
    US central timezone from a 00Z run).

    Members with HTTP / decode failures are dropped from the result.
    If fewer than 25 of 31 members succeed, returns None (insufficient data).
    """
    if city not in CITY_COORDS:
        raise ValueError(f"Unknown city {city!r}; must be one of {list(CITY_COORDS)}")
    lat, lon = CITY_COORDS[city]
    lon_0_360 = lon if lon >= 0 else lon + 360.0
    yyyymmdd = date.replace("-", "")

    if cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(CACHE_DIR, f"{yyyymmdd}_{city.replace(' ','_')}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    d = json.load(f)
                return EnsembleResult(
                    members=d["members"], control=d["control"],
                    spread_std=d["spread_std"], n_members_used=d["n_members_used"],
                )
            except Exception:
                pass  # corrupt cache; refetch

    # Probe the control member for the smallest forecast hour first — if it
    # 404s, the whole date is missing and we should abort cheap.
    probe_url = _build_url(yyyymmdd, "gec00", fcst_hours[0])
    try:
        # HEAD-equivalent: GET with Range: bytes=0-15 to confirm reachability
        _http_get(probe_url, range_hdr="bytes=0-15")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.warning(f"gefs.{yyyymmdd} not in S3 archive (HTTP 404)")
            return None
        raise

    # Fetch each (member, fcst_hour) cell in parallel
    tasks = [(m, h) for m in MEMBERS for h in fcst_hours]
    cells: dict[tuple[str, int], float] = {}    # (member, fcst_hour) -> Kelvin

    def _job(m, h):
        try:
            return (m, h, _fetch_one_field_tmax_kelvin(yyyymmdd, m, h, lat, lon_0_360))
        except Exception as e:
            # One retry on transient failures (per spec: partial GRIB during 04 UTC window)
            time.sleep(2.0)
            try:
                return (m, h, _fetch_one_field_tmax_kelvin(yyyymmdd, m, h, lat, lon_0_360))
            except Exception as e2:
                return (m, h, e2)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_job, m, h) for m, h in tasks]
        for fut in as_completed(futures):
            m, h, val = fut.result()
            if isinstance(val, Exception):
                log.debug(f"gefs.{yyyymmdd} {m} f{h:03d}: {val}")
                continue
            cells[(m, h)] = val

    # Per-member daily max across the fcst_hours we have
    member_daily_max_f: list[float] = []
    n_members_used = 0
    control_f = None
    for m in MEMBERS:
        vals_k = [cells[(m, h)] for h in fcst_hours if (m, h) in cells]
        if not vals_k:
            continue
        # K -> F
        vals_f = [(k - 273.15) * 9.0 / 5.0 + 32.0 for k in vals_k]
        daily_max = max(vals_f)
        member_daily_max_f.append(daily_max)
        n_members_used += 1
        if m == "gec00":
            control_f = daily_max

    if n_members_used < 25:
        log.warning(f"gefs.{yyyymmdd} {city}: only {n_members_used}/31 members usable; skipping")
        return None

    spread_std = float(np.std(member_daily_max_f, ddof=1))
    result = EnsembleResult(
        members=member_daily_max_f,
        control=control_f if control_f is not None else float(np.mean(member_daily_max_f)),
        spread_std=spread_std,
        n_members_used=n_members_used,
    )

    if cache:
        try:
            with open(cache_path, "w") as f:
                json.dump({
                    "members": result.members, "control": result.control,
                    "spread_std": result.spread_std, "n_members_used": result.n_members_used,
                }, f)
        except Exception as e:
            log.debug(f"cache write failed for {cache_path}: {e}")

    return result


# ── Smoke test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    test_date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-22"
    test_city = sys.argv[2] if len(sys.argv) > 2 else "Chicago"
    t0 = time.time()
    res = fetch_gefs_ensemble_hindcast(test_date, test_city, cache=False)
    elapsed = time.time() - t0
    if res is None:
        print(f"NO DATA for {test_date} {test_city}")
        sys.exit(1)
    print(f"\n{test_city} {test_date}  ({elapsed:.1f}s)")
    print(f"  n_members_used:  {res.n_members_used}")
    print(f"  control (gec00): {res.control:.2f} °F")
    print(f"  mean:            {sum(res.members)/len(res.members):.2f} °F")
    print(f"  spread_std:      {res.spread_std:.2f} °F")
    print(f"  min / max:       {min(res.members):.2f} / {max(res.members):.2f} °F")
