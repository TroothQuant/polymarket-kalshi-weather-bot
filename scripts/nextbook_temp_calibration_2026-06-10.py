#!/usr/bin/env python3
"""GATE 2 — daily high-temp calibration backtest for next-book EXPANSION cities.

Does the GEFS ensemble's daily-high forecast calibrate well enough to trade the
Polymarket daily high-temp BUCKET markets in cities the bot does NOT yet trade?

Method (reuses nomads_gfs_hindcast.py GRIB plumbing, like the precip backtest):
  - Per target day D, the D 00z GEFS run. TMAX (2m) per member at the fhours that
    bracket EACH city's local-afternoon peak (timezone-specific):
      Americas cities -> f018/f024/f030 (≈12-30z = US/S-Am afternoon)
      Europe cities   -> f012/f018      (≈06-18z = Euro morning-afternoon)
    Member daily-high = max(TMAX) over the city's fhours, converted to city unit.
  - Observed: open-meteo ERA5 archive temperature_2m_max at the SETTLEMENT-station
    airport coords, city unit, timezone=auto (local calendar day — temp markets
    settle on the local-day high).
  - Score per city: MAE + bias of ensemble-mean vs observed; modal-bucket hit
    rate (does the most-probable bucket verify) and within-1-bucket rate; bucket
    Brier vs a climatology baseline (predict the empirical bucket frequency).
  - Per-city tier: trade / size-down / exclude — derived fresh for THIS instrument.

Read-only: AWS noaa-gefs-pds (public S3) + open-meteo ERA5 archive only.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/home/trooth/Projects/trooth-weather-bot/scripts")
import numpy as np
import xarray as xr
import nomads_gfs_hindcast as ngh  # _http_get, _build_url, _parse_idx_for_tmax, MEMBERS

# city -> (lat, lon, unit 'F'/'C', bucket_width, fhours)  coords = settlement airport
AMER_FH = [18, 24, 30]
EURO_FH = [12, 18]
CITIES = {
    "atlanta":      (33.6407, -84.4277, "F", 2, AMER_FH),   # KATL Hartsfield
    "dallas":       (32.8471, -96.8518, "F", 2, AMER_FH),   # KDAL Love Field
    "austin":       (30.1975, -97.6664, "F", 2, AMER_FH),   # KAUS Bergstrom
    "seattle":      (47.4502, -122.3088, "F", 2, AMER_FH),  # KSEA Sea-Tac
    "toronto":      (43.6777, -79.6248, "C", 1, AMER_FH),   # CYYZ Pearson
    "sao_paulo":    (-23.4356, -46.4731, "C", 1, AMER_FH),  # SBGR Guarulhos
    "buenos_aires": (-34.8222, -58.5358, "C", 1, AMER_FH),  # SAEZ Ezeiza
    "london":       (51.5048, 0.0495, "C", 1, EURO_FH),     # EGLC London City
    "paris":        (48.9694, 2.4414, "C", 1, EURO_FH),     # LFPB Le Bourget
    "madrid":       (40.4719, -3.5626, "C", 1, EURO_FH),    # LEMD Barajas
}
ALL_FHOURS = sorted({h for *_ , fh in CITIES.values() for h in fh})

START_DAY = dt.date(2026, 5, 20)
N_DAYS = 15
TARGET_DAYS = [START_DAY + dt.timedelta(days=i) for i in range(N_DAYS)]


def k_to(unit, k):
    return (k - 273.15) * 9 / 5 + 32 if unit == "F" else k - 273.15


def fetch_tmax_allcities(run_yyyymmdd, member, fhour):
    """One (member,fhour) TMAX GRIB -> {city: Kelvin}."""
    url = ngh._build_url(run_yyyymmdd, member, fhour)
    idx = ngh._http_get(url + ".idx").decode()
    start, end = ngh._parse_idx_for_tmax(idx)
    rng = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    blob = ngh._http_get(url, range_hdr=rng)
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(blob)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            var = "tmax" if "tmax" in ds.data_vars else list(ds.data_vars)[0]
            out = {}
            for city, (lat, lon, *_ ) in CITIES.items():
                lon360 = lon if lon >= 0 else lon + 360.0
                out[city] = float(ds[var].sel(latitude=lat, longitude=lon360, method="nearest").values)
            return out
        finally:
            ds.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def gefs_member_highs(target_day):
    """{city: [member daily-highs in city unit]} for target day D (D 00z run)."""
    run = target_day.strftime("%Y%m%d")
    cell = defaultdict(dict)  # (member,fhour) -> {city: K}
    tasks = [(m, h) for m in ngh.MEMBERS for h in ALL_FHOURS]

    def job(m, h):
        try:
            return (m, h, fetch_tmax_allcities(run, m, h))
        except Exception:
            return (m, h, None)

    with ThreadPoolExecutor(max_workers=24) as pool:
        for fut in as_completed([pool.submit(job, m, h) for m, h in tasks]):
            m, h, res = fut.result()
            if res is not None:
                cell[(m, h)] = res

    out = {}
    for city, (lat, lon, unit, bw, fhs) in CITIES.items():
        highs = []
        for m in ngh.MEMBERS:
            vals = [cell[(m, h)][city] for h in fhs if (m, h) in cell and city in cell[(m, h)]]
            if len(vals) == len(fhs):
                highs.append(k_to(unit, max(vals)))
        out[city] = highs
    return out


def era5_obs_high(target_day):
    """{city: observed daily-high in city unit} (local day)."""
    out = {}
    for city, (lat, lon, unit, *_ ) in CITIES.items():
        u = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
             f"&start_date={target_day.isoformat()}&end_date={target_day.isoformat()}"
             f"&daily=temperature_2m_max&temperature_unit={'fahrenheit' if unit=='F' else 'celsius'}"
             f"&timezone=auto")
        try:
            j = json.loads(urllib.request.urlopen(u, timeout=25).read())
            v = j["daily"]["temperature_2m_max"][0]
            out[city] = float(v) if v is not None else None
        except Exception:
            out[city] = None
    return out


def bucket_index(value, bw):
    """Bucket id = floor(value / bw). Width bw °. Stable integer label."""
    return int(np.floor(value / bw))


def main():
    # collect per city: list of (member_highs, observed)
    data = defaultdict(list)
    for day in TARGET_DAYS:
        highs = gefs_member_highs(day)
        obs = era5_obs_high(day)
        n_ok = 0
        for city in CITIES:
            mh = highs.get(city) or []
            ob = obs.get(city)
            if len(mh) >= 20 and ob is not None:
                data[city].append((mh, ob))
                n_ok += 1
        print(f"  {day.isoformat()}: {n_ok}/{len(CITIES)} city-days", flush=True)

    print("\n" + "=" * 78)
    print("GATE 2 — daily high-temp calibration (GEFS ensemble vs ERA5 observed high)")
    print("=" * 78)
    print(f"  {'city':<14}{'n':>3}{'unit':>5}{'MAE':>7}{'bias':>7}{'modal_hit':>10}{'within1':>9}{'Brier':>8}{'climo':>8}{'BSS':>7}  tier")
    summary = []
    for city, (lat, lon, unit, bw, fhs) in CITIES.items():
        rows = data.get(city, [])
        if len(rows) < 5:
            print(f"  {city:<14}{len(rows):>3}  insufficient data")
            continue
        means = np.array([np.mean(mh) for mh, ob in rows])
        obsv = np.array([ob for mh, ob in rows])
        mae = float(np.mean(np.abs(means - obsv)))
        bias = float(np.mean(means - obsv))
        # bucket scoring
        modal_hit = within1 = 0
        briers = []
        actual_buckets = []
        all_member_bucket_probs = []
        for mh, ob in rows:
            mh = np.array(mh)
            ab = bucket_index(ob, bw)
            actual_buckets.append(ab)
            # member bucket distribution
            mbk = np.array([bucket_index(x, bw) for x in mh])
            buckets = sorted(set(list(mbk) + [ab]))
            p = {b: float(np.mean(mbk == b)) for b in buckets}
            all_member_bucket_probs.append(p)
            modal = max(p, key=p.get)
            modal_hit += (modal == ab)
            within1 += (abs(modal - ab) <= 1)
            # multi-bucket Brier for this day
            briers.append(sum((p.get(b, 0.0) - (1.0 if b == ab else 0.0)) ** 2 for b in buckets))
        brier = float(np.mean(briers))
        # climatology baseline: predict empirical bucket frequency over the sample
        from collections import Counter
        freq = Counter(actual_buckets)
        n = len(actual_buckets)
        clim_p = {b: c / n for b, c in freq.items()}
        clim_brier = float(np.mean([
            sum((clim_p.get(b, 0.0) - (1.0 if b == ab else 0.0)) ** 2
                for b in set(list(clim_p) + [ab]))
            for ab in actual_buckets]))
        bss = (1.0 - brier / clim_brier) if clim_brier > 0 else float("nan")
        mh_rate = modal_hit / len(rows)
        w1_rate = within1 / len(rows)
        # tier: trade if accurate + skillful; size-down if marginal; exclude if poor
        if mae <= (2.0 if unit == "F" else 1.2) and bss > 0.15 and mh_rate >= 0.45:
            tier = "TRADE"
        elif mae <= (3.5 if unit == "F" else 2.0) and bss > 0.0:
            tier = "size-down"
        else:
            tier = "EXCLUDE"
        summary.append((city, tier))
        print(f"  {city:<14}{len(rows):>3}{unit:>5}{mae:>7.2f}{bias:>+7.2f}{mh_rate:>10.0%}{w1_rate:>9.0%}{brier:>8.3f}{clim_brier:>8.3f}{bss:>+7.2f}  {tier}")

    print("\n  TIERS (this instrument, derived fresh):")
    for t in ("TRADE", "size-down", "EXCLUDE"):
        cs = [c for c, x in summary if x == t]
        if cs:
            print(f"    {t:<10}: {', '.join(cs)}")


if __name__ == "__main__":
    main()
