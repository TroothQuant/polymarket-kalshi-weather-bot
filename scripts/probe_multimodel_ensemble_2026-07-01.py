"""READ-ONLY multi-model ensemble probe (model-upgrade groundwork, 2026-07-01).

Hits open-meteo's ensemble API for the 5 live-scan cities across 4 global models
and reports, per model/city/day: member count, ensemble mean & std, and each
model's mean minus the current gfs_seamless mean. Confirms ECMWF member counts.

Standalone: NO bot imports, NO writes, NO order/trade actions. Just prints.
Usage:  python3 scripts/probe_multimodel_ensemble_2026-07-01.py
"""
import json
import urllib.request
import urllib.parse
import statistics as st

COORDS = {
    "nyc": (40.7128, -74.0060), "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918), "los_angeles": (33.9425, -118.4081),
    "denver": (39.7392, -104.9903),
}
MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_global"]


def fetch(lat, lon, model):
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "forecast_days": 3, "models": model,
    })
    url = f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        return {"_error": str(e)}


def per_day_members(daily):
    """Return {date: [member values]} from the ensemble 'daily' block. Handles
    both `temperature_2m_max_memberNN` and `..._<model>_memberNN` key spellings."""
    times = daily.get("time", [])
    member_keys = [k for k in daily if k.startswith("temperature_2m_max") and "member" in k]
    out = {t: [] for t in times}
    for k in member_keys:
        vals = daily[k]
        for t, v in zip(times, vals):
            if v is not None:
                out[t].append(v)
    return out, len(member_keys)


def main():
    # gfs baseline means per city/day
    gfs_mean = {}
    results = {}   # model -> city -> {date: (mean,std,n)}
    member_counts = {}  # model -> city -> n_members
    for model in MODELS:
        results[model] = {}
        member_counts[model] = {}
        for city, (lat, lon) in COORDS.items():
            j = fetch(lat, lon, model)
            if "_error" in j or "daily" not in j:
                results[model][city] = None
                member_counts[model][city] = 0
                continue
            pdm, nkeys = per_day_members(j["daily"])
            member_counts[model][city] = nkeys
            day_stats = {}
            for day, vals in pdm.items():
                if vals:
                    day_stats[day] = (st.mean(vals),
                                      st.pstdev(vals) if len(vals) > 1 else 0.0,
                                      len(vals))
            results[model][city] = day_stats
            if model == "gfs_seamless":
                gfs_mean[city] = {day: s[0] for day, s in day_stats.items()}

    print("=== MEMBER COUNTS (keys) per model x city ===")
    print(f"{'model':16s} " + " ".join(f"{c:>11s}" for c in COORDS))
    for model in MODELS:
        print(f"{model:16s} " + " ".join(f"{member_counts[model][c]:>11d}" for c in COORDS))

    print("\n=== PER MODEL/CITY/DAY: mean°F (std) | Δ vs gfs mean ===")
    for city in COORDS:
        print(f"\n-- {city} --")
        days = sorted(results["gfs_seamless"].get(city) or {})
        for day in days:
            line = f"  {day}: "
            for model in MODELS:
                ds = results[model].get(city)
                if not ds or day not in ds:
                    line += f"{model.split('_')[0]}=NA  "
                    continue
                mean, std, n = ds[day]
                delta = mean - gfs_mean.get(city, {}).get(day, mean)
                tag = model.split("_")[0]
                line += f"{tag}={mean:5.1f}(±{std:3.1f},d{delta:+.1f})  "
            print(line)

    print("\n=== CROSS-MODEL MEAN DISAGREEMENT (max mean − min mean across models, °F) ===")
    for city in COORDS:
        days = sorted(results["gfs_seamless"].get(city) or {})
        spreads = []
        for day in days:
            means = [results[m][city][day][0] for m in MODELS
                     if results[m].get(city) and day in results[m][city]]
            if len(means) > 1:
                spreads.append(max(means) - min(means))
        if spreads:
            print(f"  {city:12s} mean cross-model spread {st.mean(spreads):.1f}°F "
                  f"(max {max(spreads):.1f}°F over {len(spreads)} days)")

    ecmwf = member_counts.get("ecmwf_ifs025", {})
    print("\nECMWF member counts (expect 51):", {c: ecmwf.get(c, 0) for c in COORDS})


if __name__ == "__main__":
    main()
