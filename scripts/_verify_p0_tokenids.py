#!/usr/bin/env python3
"""P0 verification (READ-ONLY): fetch a live Polymarket weather market, parse it
with the bot's own parser, and confirm the captured token IDs == the raw gamma
clobTokenIds (which ARE what Polymarket's CLOB uses to post an order)."""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.data.weather_markets import _parse_polymarket_weather

H = {"User-Agent": "(weather-bot p0-verify, read-only)"}


def main():
    seen, events = set(), []
    for tag in ("weather", "temperature"):
        for off in range(0, 600, 100):
            try:
                r = httpx.get("https://gamma-api.polymarket.com/events",
                              params={"closed": "false", "limit": 100, "offset": off, "tag_slug": tag},
                              headers=H, timeout=25)
                page = r.json()
            except Exception as e:
                print(f"fetch {tag}@{off} failed: {e}")
                continue
            if not page:
                break
            for ev in page:
                if ev.get("id") not in seen:
                    seen.add(ev.get("id"))
                    events.append(ev)
    print(f"fetched {len(events)} unique live weather/temperature events")

    shown = 0
    for ev in events:
        slug = ev.get("slug", "")
        for mkt in ev.get("markets", []):
            wm = _parse_polymarket_weather(mkt, slug)
            if wm is None or not wm.token_id_yes:
                continue
            raw = mkt.get("clobTokenIds")
            print("\n" + "=" * 78)
            print(f"event slug : {slug}")
            print(f"question   : {wm.title}")
            print(f"city/dir   : {wm.city_key} / {wm.direction} {wm.threshold_f:.0f}F  (yes={wm.yes_price} no={wm.no_price})")
            print(f"PARSED  token_id_yes : {wm.token_id_yes}")
            print(f"PARSED  token_id_no  : {wm.token_id_no}")
            print(f"PARSED  condition_id : {wm.condition_id}")
            print(f"RAW gamma clobTokenIds: {raw}")
            print(f"RAW gamma conditionId : {mkt.get('conditionId')}")
            import json as _j
            raw_list = _j.loads(raw) if isinstance(raw, str) else raw
            match = (str(raw_list[0]) == wm.token_id_yes and str(raw_list[1]) == wm.token_id_no
                     and str(mkt.get("conditionId")) == wm.condition_id)
            print(f"MATCH (parsed == raw gamma): {match}")
            shown += 1
            if shown >= 2:
                return
    if shown == 0:
        print("no parseable edge-bucket weather market with token IDs found this fetch")


if __name__ == "__main__":
    main()
