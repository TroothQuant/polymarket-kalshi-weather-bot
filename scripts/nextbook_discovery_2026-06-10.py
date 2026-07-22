#!/usr/bin/env python3
"""Weather next-book GATE 1 discovery (READ-ONLY).

Enumerates live Polymarket daily high-temp markets across Americas/Europe cities,
captures settlement station + live middle-bucket spreads, and ranks the
expansion candidates against the proven temp book's current cities.
Read-only: gamma public API only. No bot/state touched.
"""
import json
import re
import httpx

H = {"User-Agent": "(trading-bot, contact@example.com)"}
GAMMA = "https://gamma-api.polymarket.com/events"

# Cities the bot ALREADY trades (don't re-propose)
CURRENT = {"nyc", "chicago", "miami", "los-angeles", "denver"}

# Americas + late-Europe candidates whose pre-peak uncertain window overlaps a
# US-anchored 24/7 run (Asian megacities resolve out before US daytime).
CANDIDATES = ["atlanta", "dallas", "austin", "seattle", "toronto",
              "sao-paulo", "buenos-aires", "london", "paris", "madrid"]


def jget(params):
    r = httpx.get(GAMMA, params=params, timeout=25.0, headers=H)
    r.raise_for_status()
    return r.json()


def bulk_event_map():
    """Per-slug gamma queries are intermittently empty (truncated responses);
    the bulk tag query is reliable. Fetch once, index by slug."""
    m = {}
    for tag in ("temperature", "weather"):
        try:
            for e in jget({"closed": "false", "limit": 300, "tag_slug": tag}):
                m[e.get("slug")] = e
        except Exception:
            pass
    return m


_BULK = {}


def station(desc):
    m = re.search(r"recorded (?:at|by) (?:the )?([^.]*?(?:Airport|Station|Observatory)[^.]*?) in degrees", desc or "")
    return m.group(1).strip()[:46] if m else "(see desc)"


def analyze(city, date="june-10-2026"):
    e = _BULK.get(f"highest-temperature-in-{city}-on-{date}")
    if not e:
        return None
    liq = round(e.get("liquidity") or 0)
    vol = round(e.get("volume") or 0)
    desc = e.get("description") or ""
    unit = "C" if "Celsius" in desc else ("F" if "Fahrenheit" in desc else "?")
    spreads = []
    live_mid = 0
    for m in e.get("markets", []):
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        if bb and ba and 0.12 <= ba <= 0.88:
            live_mid += 1
            spreads.append(round(ba - bb, 3))
    spreads.sort()
    med = spreads[len(spreads) // 2] if spreads else None
    return {"city": city, "vol": vol, "liq": liq, "unit": unit,
            "station": station(desc), "live_mid": live_mid,
            "spread_med": med, "spread_min": (spreads[0] if spreads else None),
            "spread_max": (spreads[-1] if spreads else None)}


def main():
    global _BULK
    _BULK = bulk_event_map()
    print("=" * 78)
    print(f"NEXT-BOOK DISCOVERY — Polymarket daily high-temp ({len(_BULK)} live weather events)")
    print("=" * 78)
    print(f"{'city':<14}{'unit':>5}{'vol$':>9}{'liq$':>9}{'live_mid':>9}{'spread(min/med/max)':>22}  station")
    rows = []
    for c in CANDIDATES:
        try:
            r = analyze(c)
        except Exception as e:
            print(f"{c:<14} ERR {str(e)[:40]}")
            continue
        if not r:
            print(f"{c:<14} (no live event today)")
            continue
        rows.append(r)
        sp = (f"{r['spread_min']:.2f}/{r['spread_med']:.2f}/{r['spread_max']:.2f}"
              if r["spread_med"] is not None else "(resolved-out: no live middle)")
        print(f"{r['city']:<14}{r['unit']:>5}{r['vol']:>9}{r['liq']:>9}{r['live_mid']:>9}{sp:>22}  {r['station']}")

    # rank: tradeable = has a live uncertain middle with tight spread, scored by liq
    print("\nRANK (tradeable now = live middle present, tight spread; by liquidity):")
    tradeable = [r for r in rows if r["live_mid"] >= 2 and r["spread_med"] is not None and r["spread_med"] <= 0.05]
    tradeable.sort(key=lambda r: -r["liq"])
    for i, r in enumerate(tradeable, 1):
        print(f"  {i}. {r['city']:<12} liq=${r['liq']:<7} med-spread={r['spread_med']:.2f}  unit={r['unit']}  {r['station']}")
    resolved = [r["city"] for r in rows if r["live_mid"] < 2]
    if resolved:
        print(f"  (resolved-out at probe time — re-check during their pre-peak window: {', '.join(resolved)})")


if __name__ == "__main__":
    main()
