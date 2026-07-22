#!/usr/bin/env python3
"""GATE 2-REDO — calibrate the BOT'S OWN forecast source, not raw GEFS-NOMADS.

GATE 2 (v1/v2) tested GEFS-NOMADS 0.25° and failed. But the live weather bot
forecasts from open-meteo's GFS, which calibrates on the existing 5 cities.
This isolates tool-vs-edge: re-score the SAME cities/window/stations using
open-meteo's **Historical Forecast API** (faithful archived forecast, not ERA5
reanalysis), and add the 5 EXISTING cities as a known-good control anchor.

NOTE on faithfulness: open-meteo's ENSEMBLE archive only reaches ~3 days back,
so a 15-day ensemble backtest is impossible. The Historical Forecast API serves
the **deterministic GFS** (the ensemble's control member) over the full window —
which tests the core question (is the forecast accurate to bucket precision).
Metrics: MAE, bias, exact-bucket-hit, within-1-bucket vs IEM station truth.

Decision rule:
  - controls calibrate under open-meteo AND >=1 expansion city hits TRADE -> tool
    was the problem; those cities advance to GATE 3.
  - open-meteo ALSO fails the expansion (while calibrating controls) -> edge not
    reachable on these cities; shelve cleanly.

Read-only: open-meteo Historical Forecast API + IEM ASOS only.
"""
import datetime as dt
import json
import sys
import urllib.request

import numpy as np

# city -> (lat, lon, unit, bucket_width, (iem_station, iem_network), kind)
CITIES = {
    # ---- EXISTING (control anchor) — settlement stations from prior work ----
    "NYC*":          (40.7794, -73.8803, "F", 2, ("LGA", "NY_ASOS"), "control"),
    "Chicago*":      (41.9786, -87.9048, "F", 2, ("ORD", "IL_ASOS"), "control"),
    "Miami*":        (25.7959, -80.2870, "F", 2, ("MIA", "FL_ASOS"), "control"),
    "LosAngeles*":   (33.9425, -118.4081, "F", 2, ("LAX", "CA_ASOS"), "control"),
    "Denver*":       (39.8561, -104.6737, "F", 2, ("DEN", "CO_ASOS"), "control"),
    # ---- EXPANSION candidates (settlement airport coords) ----
    "atlanta":       (33.6407, -84.4277, "F", 2, ("ATL", "GA_ASOS"), "exp"),
    "dallas":        (32.8471, -96.8518, "F", 2, ("DAL", "TX_ASOS"), "exp"),
    "austin":        (30.1975, -97.6664, "F", 2, ("AUS", "TX_ASOS"), "exp"),
    "seattle":       (47.4502, -122.3088, "F", 2, ("SEA", "WA_ASOS"), "exp"),
    "toronto":       (43.6777, -79.6248, "C", 1, ("YYZ", "CA_ASOS"), "exp"),
    "sao_paulo":     (-23.4356, -46.4731, "C", 1, ("SBGR", "BR__ASOS"), "exp"),
    "buenos_aires":  (-34.8222, -58.5358, "C", 1, ("SAEZ", "AR__ASOS"), "exp"),
    "london":        (51.5048, 0.0495, "C", 1, ("EGLC", "GB__ASOS"), "exp"),
    "paris":         (48.9694, 2.4414, "C", 1, ("LFPB", "FR__ASOS"), "exp"),
    "madrid":        (40.4719, -3.5626, "C", 1, ("LEMD", "ES__ASOS"), "exp"),
}
START = dt.date(2026, 5, 20)
END = dt.date(2026, 6, 3)

# NOMADS v1 MAE/bias for side-by-side (from nextbook_temp_calibration run)
NOMADS = {
    "atlanta": (2.87, 1.61), "dallas": (4.88, 4.88), "austin": (2.71, 2.02),
    "seattle": (2.11, 1.50), "toronto": (1.94, -0.98), "sao_paulo": (0.84, 0.27),
    "buenos_aires": (0.85, 0.52), "london": (1.47, -1.37), "paris": (0.60, -0.05),
    "madrid": (1.97, -1.97),
}


def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=45).read())


def openmeteo_forecast(city):
    lat, lon, unit, *_ = CITIES[city]
    u = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
         f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
         f"&temperature_unit={'fahrenheit' if unit=='F' else 'celsius'}"
         f"&start_date={START.isoformat()}&end_date={END.isoformat()}&models=gfs_seamless")
    try:
        d = _get(u).get("daily", {})
        return {t: v for t, v in zip(d.get("time", []), d.get("temperature_2m_max", [])) if v is not None}
    except Exception as e:
        print(f"  openmeteo fail {city}: {e}", file=sys.stderr)
        return {}


def iem_obs(city):
    _, _, unit, _, (st, net), _ = CITIES[city]
    u = f"https://mesonet.agron.iastate.edu/api/1/daily.json?station={st}&network={net}"
    out = {}
    try:
        for r in _get(u).get("data", []):
            f = r.get("max_tmpf")
            if f is not None and START.isoformat() <= r.get("date", "") <= END.isoformat():
                out[r["date"]] = float(f) if unit == "F" else (float(f) - 32) * 5 / 9
    except Exception as e:
        print(f"  iem fail {city}: {e}", file=sys.stderr)
    return out


def main():
    print("=" * 96)
    print("GATE 2-REDO — open-meteo GFS (bot's source) vs IEM station truth, + NOMADS side-by-side")
    print("=" * 96)
    print(f"  {'city':<14}{'kind':>8}{'n':>4}{'unit':>5}{'MAE':>7}{'bias':>7}{'exact':>7}{'win1':>6}"
          f"{'| NOMADS MAE/bias':>20}  tier")
    tiers = {}
    for city, (lat, lon, unit, bw, st, kind) in CITIES.items():
        fc = openmeteo_forecast(city)
        ob = iem_obs(city)
        pairs = [(fc[d], ob[d]) for d in fc if d in ob]
        if len(pairs) < 5:
            print(f"  {city:<14}{kind:>8}{len(pairs):>4}  insufficient (fc={len(fc)} obs={len(ob)})")
            continue
        f = np.array([p[0] for p in pairs])
        o = np.array([p[1] for p in pairs])
        mae = float(np.mean(np.abs(f - o)))
        bias = float(np.mean(f - o))
        bf = np.floor(f / bw)
        bo = np.floor(o / bw)
        exact = float(np.mean(bf == bo))
        win1 = float(np.mean(np.abs(bf - bo) <= 1))
        if mae <= 0.7 * bw and exact >= 0.40 and win1 >= 0.85:
            tier = "TRADE"
        elif mae <= 1.2 * bw and win1 >= 0.70:
            tier = "size-down"
        else:
            tier = "EXCLUDE"
        tiers[city] = (kind, tier)
        nm = NOMADS.get(city)
        nmstr = f"{nm[0]:.2f}/{nm[1]:+.2f}" if nm else "--"
        print(f"  {city:<14}{kind:>8}{len(pairs):>4}{unit:>5}{mae:>7.2f}{bias:>+7.2f}{exact:>7.0%}{win1:>6.0%}"
              f"{nmstr:>20}  {tier}")

    print("\n  CONTROLS (existing live cities) under open-meteo:")
    for c, (k, t) in tiers.items():
        if k == "control":
            print(f"    {c:<14} {t}")
    print("  EXPANSION tiers under open-meteo:")
    for t in ("TRADE", "size-down", "EXCLUDE"):
        cs = [c for c, (k, x) in tiers.items() if k == "exp" and x == t]
        if cs:
            print(f"    {t:<10}: {', '.join(cs)}")


if __name__ == "__main__":
    main()
