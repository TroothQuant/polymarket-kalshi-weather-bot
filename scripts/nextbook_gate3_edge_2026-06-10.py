#!/usr/bin/env python3
"""GATE 3 — edge-vs-execution for the 5 GATE-2-REDO qualifiers (READ-ONLY).

For each city's LIVE daily-high market today: pull the bot's actual open-meteo
GFS ensemble (31 members), map members to the market's exact buckets to get
model P(bucket), and compare to the EXECUTABLE price (pay the ask to buy YES) —
not mid. Confirm edge survives the real spread. Apply the pre-peak filter
(a collapsed market = day already resolved = no entry).

Read-only: open-meteo ensemble API + Polymarket gamma. No bot/state touched.
"""
import datetime as dt
import json
import re
import urllib.request
import httpx
import numpy as np

H = {"User-Agent": "(trading-bot, contact@example.com)"}
TODAY = dt.date.today().isoformat()  # slug uses month-name though; set below
SLUG_DATE = "june-10-2026"
MIN_EDGE = 0.08  # bot's weather min-edge floor

# city -> (lat, lon, unit, gamma_city_slug)  settlement-airport coords
CITIES = {
    "dallas":       (32.8471, -96.8518, "F", "dallas"),
    "austin":       (30.1975, -97.6664, "F", "austin"),
    "london":       (51.5048, 0.0495, "C", "london"),
    "paris":        (48.9694, 2.4414, "C", "paris"),
    "buenos_aires": (-34.8222, -58.5358, "C", "buenos-aires"),
}


def ensemble_members(lat, lon, unit):
    p = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
         "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
         "start_date": "2026-06-10", "end_date": "2026-06-10", "models": "gfs_seamless"}
    d = httpx.get("https://ensemble-api.open-meteo.com/v1/ensemble", params=p, headers=H, timeout=30).json()["daily"]
    return [d[k][0] for k in d if "temperature_2m_max" in k and d[k][0] is not None]


def gamma_event(city_slug):
    bulk = {}
    for tag in ("temperature", "weather"):
        try:
            for e in httpx.get("https://gamma-api.polymarket.com/events",
                               params={"closed": "false", "limit": 300, "tag_slug": tag},
                               headers=H, timeout=25).json():
                bulk[e.get("slug")] = e
        except Exception:
            pass
    return bulk.get(f"highest-temperature-in-{city_slug}-on-{SLUG_DATE}")


def parse_bucket(q, unit):
    """Return (lo, hi) numeric range for a bucket question label."""
    s = q
    m = re.search(r"(\d+)\s*-\s*(\d+)", s)          # 'A-B'
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b + 1)
    m = re.search(r"(\d+).{0,12}(?:or higher|or above|or more)", s)
    if m:
        return (int(m.group(1)), 200.0)
    m = re.search(r"(\d+).{0,12}(?:or below|or lower|or less)", s)
    if m:
        return (-200.0, int(m.group(1)) + (1 if unit == "F" else 0.5))
    m = re.search(r"(\d+)\s*°?[CF]", s)              # single 'X°C'
    if m:
        x = int(m.group(1))
        return (x - 0.5, x + 0.5) if unit == "C" else (x, x + 1)
    return None


def localnow_hint(city):
    off = {"dallas": -5, "austin": -5, "london": 1, "paris": 2, "buenos_aires": -3}[city]
    h = (dt.datetime.utcnow().hour + off) % 24
    peak = 14 if city != "buenos_aires" else 15
    return h, ("PRE-PEAK" if h < peak else "AT/POST-PEAK (window closing)")


def main():
    print("=" * 92)
    print("GATE 3 — edge vs EXECUTABLE price (pay-the-ask), pre-peak filter — 5 REDO qualifiers")
    print("=" * 92)
    for city, (lat, lon, unit, cslug) in CITIES.items():
        lh, win = localnow_hint(city)
        try:
            mem = np.array(ensemble_members(lat, lon, unit))
        except Exception as e:
            print(f"\n{city}: ensemble fail {e}")
            continue
        e = gamma_event(cslug)
        if e is None or len(mem) < 10:
            print(f"\n{city}: no live event / ensemble (members={len(mem)})")
            continue
        # live middle?
        live_mid = [m for m in e.get("markets", [])
                    if m.get("bestBid") and m.get("bestAsk") and 0.10 <= m["bestAsk"] <= 0.90]
        best = None
        for m in e.get("markets", []):
            rng = parse_bucket(m.get("groupItemTitle") or m.get("question") or "", unit)
            ask = m.get("bestAsk")
            if rng is None or ask is None:
                continue
            lo, hi = rng
            p_model = float(np.mean((mem >= lo) & (mem < hi)))
            edge = p_model - ask        # buy YES at ask
            if best is None or edge > best[0]:
                best = (edge, m.get("groupItemTitle") or "", p_model, ask, m.get("bestBid"))
        print(f"\n{city}  (local ~{lh}:00, {win}; live-middle buckets={len(live_mid)}, ens_mean={mem.mean():.1f}{unit})")
        if best:
            ed, lbl, pm, ask, bid = best
            verdict = ("EDGE SURVIVES" if (ed >= MIN_EDGE and len(live_mid) >= 2 and "PRE" in win)
                       else "no tradeable edge")
            print(f"   best bucket: {lbl:<14} model_p={pm:.2f}  ask={ask}  bid={bid}  -> edge(buy)={ed:+.2f}  [{verdict}]")


if __name__ == "__main__":
    main()
