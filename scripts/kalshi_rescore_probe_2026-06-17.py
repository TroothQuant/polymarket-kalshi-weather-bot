import asyncio, sys
sys.path.insert(0, "/home/trooth/Projects/trooth-weather-bot")
from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
from backend.data.kalshi_markets import CITY_SERIES, CITY_NAMES

async def dump(c, city_key, date_suffix):
    series = CITY_SERIES[city_key]
    prefix = f"{series}-{date_suffix}-"
    allm=[]; cur=None
    while True:
        p={"series_ticker":series,"limit":200}
        if cur: p["cursor"]=cur
        d=await c.get_markets(p)
        allm += d.get("markets",[]) or []
        cur=d.get("cursor")
        if not cur: break
    today=[m for m in allm if (m.get("ticker") or "").startswith(prefix)]
    print(f"\n=== {city_key} {series} {date_suffix}: {len(today)} markets ===")
    winner=None
    for m in sorted(today, key=lambda x: (x.get("floor_strike") or 0)):
        st=(m.get("status") or "").lower(); res=(m.get("result") or "").lower()
        flag = "  <-- WINNER" if res in ("yes","yes_win") else ""
        if flag: winner=m
        print(f"  {m.get('ticker'):28} st={st:10} res={res:8} floor={m.get('floor_strike')} cap={m.get('cap_strike')} type={m.get('strike_type')}{flag}")
    if today:
        # settlement station documented in rules?
        rp = (today[0].get("rules_primary") or "")
        print("  RULES_PRIMARY (first 260 chars):", rp[:260].replace("\n"," "))
    return winner

async def main():
    print("kalshi creds present:", kalshi_credentials_present())
    c = KalshiClient()
    # probe two cities, two past dates (resolved)
    await dump(c, "nyc", "26JUN10")
    await dump(c, "chicago", "26JUN10")

asyncio.run(main())
