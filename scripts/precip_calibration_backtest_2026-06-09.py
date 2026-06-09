#!/usr/bin/env python3
"""Precip calibration backtest (READ-ONLY analysis).

Does the GEFS ensemble's probability-of-rain match observed rain frequency?
Gate before any precip trading.

Method (reuses scripts/nomads_gfs_hindcast.py for the GEFS GRIB plumbing):
  - For each target UTC day D, use the D-1 00z GEFS run at ~24-48h lead.
  - APCP in GEFS pgrb2sp25 is 6h buckets; day D (00z-24z UTC) = f030+f036+f042+f048.
  - Per member: sum those 4 buckets per city -> member daily precip (mm -> inch).
  - PoP = fraction of members with daily precip >= 0.01 inch (measurable).
  - Observed: open-meteo ERA5 archive daily precipitation_sum, inch, timezone=GMT
    (same UTC day window as the GEFS sum). Outcome = observed >= 0.01 inch.
  - Score: Brier, 5-bin reliability, base rate, climatology Brier, per-city Brier.

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
import nomads_gfs_hindcast as ngh  # reuse _http_get, _build_url, MEMBERS

# city -> (lat, lon)  (representative metro points; 0.25deg grid ~28km so
# downtown-vs-station is immaterial for areal precip)
CITIES = {
    "denver":        (39.74, -104.99),
    "san_francisco": (37.77, -122.42),
    "atlanta":       (33.75, -84.39),
    "dallas":        (32.78, -96.80),
    "boston":        (42.36, -71.06),
    "nyc_centralpk": (40.78, -73.97),
}
FHOURS = [30, 36, 42, 48]          # 6h APCP buckets covering UTC day D (D-1 00z run)
THRESH_IN = 0.01
MM_PER_IN = 25.4
MIN_MEMBERS = 20                    # need >= this many full members for a valid PoP

# Target UTC days: 14-day window, ~3-5 days back so ERA5 obs are settled.
START_DAY = dt.date(2026, 5, 22)
N_DAYS = 14
TARGET_DAYS = [START_DAY + dt.timedelta(days=i) for i in range(N_DAYS)]


def parse_idx_apcp(idx_text):
    parsed = []
    for ln in idx_text.strip().split("\n"):
        if ":" not in ln:
            continue
        n, off, rest = ln.split(":", 2)
        try:
            parsed.append((int(n), int(off), rest))
        except ValueError:
            continue
    for i, (_, start, rest) in enumerate(parsed):
        if "APCP:surface" in rest:
            end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else None
            return start, end
    raise RuntimeError("APCP:surface not found in .idx")


def fetch_apcp_allcities(run_yyyymmdd, member, fhour):
    """Download one (member, fhour) APCP record, return {city: mm}."""
    url = ngh._build_url(run_yyyymmdd, member, fhour)
    idx = ngh._http_get(url + ".idx").decode()
    start, end = parse_idx_apcp(idx)
    rng = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    blob = ngh._http_get(url, range_hdr=rng)
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(blob)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            var = list(ds.data_vars)[0]
            out = {}
            for city, (lat, lon) in CITIES.items():
                lon360 = lon if lon >= 0 else lon + 360.0
                out[city] = float(ds[var].sel(latitude=lat, longitude=lon360,
                                              method="nearest").values)
            return out
        finally:
            ds.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def gefs_pop(target_day):
    """Return {city: PoP or None} for target UTC day D from the D-1 00z run."""
    run = (target_day - dt.timedelta(days=1)).strftime("%Y%m%d")
    acc = defaultdict(lambda: defaultdict(float))   # member -> city -> mm
    cnt = defaultdict(lambda: defaultdict(int))     # member -> city -> #buckets
    tasks = [(m, h) for m in ngh.MEMBERS for h in FHOURS]

    def job(m, h):
        try:
            return (m, fetch_apcp_allcities(run, m, h))
        except Exception:
            return (m, None)

    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = [pool.submit(job, m, h) for m, h in tasks]
        for fut in as_completed(futs):
            m, res = fut.result()
            if res is None:
                continue
            for city, mm in res.items():
                acc[m][city] += mm
                cnt[m][city] += 1

    pops = {}
    for city in CITIES:
        rain = total = 0
        for m in ngh.MEMBERS:
            if cnt[m][city] == len(FHOURS):       # only members with all 4 buckets
                total += 1
                if acc[m][city] / MM_PER_IN >= THRESH_IN:
                    rain += 1
        pops[city] = (rain / total) if total >= MIN_MEMBERS else None
    return pops


def era5_obs(target_day):
    """{city: observed_inch or None} over UTC day (timezone=GMT)."""
    out = {}
    for city, (lat, lon) in CITIES.items():
        u = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
             f"&start_date={target_day.isoformat()}&end_date={target_day.isoformat()}"
             f"&daily=precipitation_sum&precipitation_unit=inch&timezone=GMT")
        try:
            j = json.loads(urllib.request.urlopen(u, timeout=25).read())
            v = j["daily"]["precipitation_sum"][0]
            out[city] = float(v) if v is not None else None
        except Exception:
            out[city] = None
    return out


def main():
    pairs = []        # (city, day, pop, obs_inch, outcome)
    for day in TARGET_DAYS:
        pops = gefs_pop(day)
        obs = era5_obs(day)
        n_ok = 0
        for city in CITIES:
            pop = pops.get(city)
            ob = obs.get(city)
            if pop is None or ob is None:
                continue
            outcome = 1.0 if ob >= THRESH_IN else 0.0
            pairs.append((city, day.isoformat(), pop, ob, outcome))
            n_ok += 1
        print(f"  {day.isoformat()}: {n_ok}/{len(CITIES)} city-days scored", flush=True)

    if not pairs:
        print("NO DATA — all city-days failed.")
        return

    pops = np.array([p[2] for p in pairs])
    outs = np.array([p[4] for p in pairs])
    n = len(pairs)
    base = outs.mean()
    brier = float(np.mean((pops - outs) ** 2))
    brier_clim = float(np.mean((base - outs) ** 2))
    bss = 1.0 - brier / brier_clim if brier_clim > 0 else float("nan")

    print("\n" + "=" * 64)
    print("PRECIP CALIBRATION BACKTEST — GEFS PoP vs observed rain")
    print("=" * 64)
    print(f"  city-days scored:        {n}")
    print(f"  base rate (rain freq):   {base:.3f}  ({int(outs.sum())} wet / {n})")
    print(f"  Brier (GEFS PoP):        {brier:.4f}")
    print(f"  Brier (climatology):     {brier_clim:.4f}  (predict base rate daily)")
    print(f"  Brier Skill Score:       {bss:+.3f}   (>0 = beats climatology)")

    print("\n  5-BIN RELIABILITY (PoP bucket -> actual rain rate):")
    print(f"    {'bucket':<12}{'n':>5}{'mean PoP':>11}{'actual':>9}")
    edges = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)]
    for lo, hi in edges:
        mask = (pops >= lo) & (pops < hi)
        k = int(mask.sum())
        if k == 0:
            print(f"    [{lo:.1f},{hi:.1f})    {0:>5}{'—':>11}{'—':>9}")
            continue
        print(f"    [{lo:.1f},{hi:.1f})    {k:>5}{pops[mask].mean():>11.3f}{outs[mask].mean():>9.3f}")

    print("\n  PER-CITY Brier (n permitting):")
    print(f"    {'city':<16}{'n':>4}{'base':>7}{'Brier':>9}{'clim':>9}{'BSS':>8}")
    for city in CITIES:
        idx = [i for i, p in enumerate(pairs) if p[0] == city]
        if not idx:
            continue
        cp = pops[idx]
        co = outs[idx]
        cb = float(np.mean((cp - co) ** 2))
        cbase = co.mean()
        cclim = float(np.mean((cbase - co) ** 2))
        cbss = (1.0 - cb / cclim) if cclim > 0 else float("nan")
        print(f"    {city:<16}{len(idx):>4}{cbase:>7.2f}{cb:>9.4f}{cclim:>9.4f}{cbss:>+8.2f}")

    # dump raw pairs for the writeup
    print("\n  RAW (city,day,PoP,obs_in,wet):")
    for c, d, p, o, w in pairs:
        print(f"    {c:<15}{d}  PoP={p:.2f}  obs={o:.3f}in  {'WET' if w else 'dry'}")


if __name__ == "__main__":
    main()
