#!/usr/bin/env python3
"""Probe: settlement stations for the 5 existing temp-book cities (read-only)."""
import re
import httpx

H = {"User-Agent": "(trading-bot, contact@example.com)"}


def jget(params):
    r = httpx.get("https://gamma-api.polymarket.com/events", params=params, timeout=25.0, headers=H)
    r.raise_for_status()
    return r.json()


def station(desc):
    m = re.search(r"recorded (?:at|by) (?:the )?([^.]*?(?:Airport|Station|Observatory|Park)[^.]*?) in degrees", desc or "")
    return m.group(1).strip() if m else "(?)"


# bulk fetch (per-slug is flaky)
bulk = {}
for tag in ("temperature", "weather"):
    try:
        for e in jget({"closed": "false", "limit": 300, "tag_slug": tag}):
            bulk[e.get("slug")] = e
    except Exception:
        pass

for city in ("nyc", "chicago", "miami", "los-angeles", "denver"):
    e = bulk.get(f"highest-temperature-in-{city}-on-june-10-2026")
    if not e:
        print(f"{city}: no event"); continue
    d = e.get("description") or ""
    unit = "F" if "Fahrenheit" in d else ("C" if "Celsius" in d else "?")
    print(f"{city:<14} unit={unit}  station= {station(d)}")
