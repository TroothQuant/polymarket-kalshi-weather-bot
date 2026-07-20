# NOTE (2026-06-17): bespoke audit re-score, READ-ONLY. Known fidelity gaps (Gaussian bucket prob + distance-to-edge conviction-z proxy) — numbers are PROVISIONAL, do NOT count toward the Kalshi re-enable bar until superseded by the canonical validated harness (build D). See session_log_2026-06-16.md.
"""
Read-only Kalshi station-divergence re-score (coord-change-ALONE).

Grading truth = Kalshi's resolved winning bucket (lock #1). Re-score changes ONLY the
forecast coordinate: corrected pick = bucket containing (recorded ensemble mean + GFS
coord delta between the bot's current coord and Kalshi's documented settlement station).
Validity check: reconstruct the ~30% current baseline two ways before trusting anything.
No DB writes, no env changes, no trades.
"""
import asyncio, sys, re, json, urllib.request, statistics, sqlite3, datetime
sys.path.insert(0, "/home/trooth/Projects/trooth-weather-bot")
from backend.data.kalshi_client import KalshiClient
from backend.data.kalshi_markets import CITY_SERIES

ROOT="/home/trooth/Projects/trooth-weather-bot"
# bot's CURRENT forecast coords (CITY_CONFIG lat/lon) — DO NOT change (lock #2)
CUR={"nyc":(40.7128,-74.0060),"chicago":(41.8781,-87.6298),"miami":(25.7617,-80.1918),
     "los_angeles":(33.9425,-118.4081),"denver":(39.7392,-104.9903)}
# known station-name -> lat/lon; filled/verified from rules_primary at runtime
STATION_COORD={
 "central park":(40.7789,-73.9692),"chicago midway":(41.7860,-87.7524),
 "midway":(41.7860,-87.7524),"miami international airport":(25.7959,-80.2870),
 "los angeles airport":(33.9425,-118.4081),"lax":(33.9425,-118.4081),
 "denver":(39.8466,-104.6562),"denver international":(39.8466,-104.6562),
}
MON=["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
def suf(d): return f"{d.year%100:02d}{MON[d.month-1]}{d.day:02d}"

def om_forecast_maxes(lat,lon,d0,d1):
    url=(f"https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         f"&start_date={d0}&end_date={d1}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto")
    for _ in range(3):
        try:
            j=json.loads(urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"x"}),timeout=40).read())
            dd=j.get("daily",{}); return dict(zip(dd.get("time",[]),dd.get("temperature_2m_max",[])))
        except Exception: pass
    return {}

def parse_station(rules):
    m=re.search(r"recorded (?:in|at)\s+(.+?)(?:,|\s+for\b)", rules or "")
    return (m.group(1).strip().lower() if m else None)

def bucket_contains(t, mk):
    ty=(mk.get("strike_type") or "").lower(); f=mk.get("floor_strike"); c=mk.get("cap_strike")
    if t is None: return False
    if ty=="less":    return c is not None and t < c
    if ty=="greater": return f is not None and t >= f+1
    if ty=="between": return (f is not None and c is not None and f <= t < c+1)
    return False

async def fetch_series(c, series):
    allm=[]; cur=None
    while True:
        p={"series_ticker":series,"limit":200}
        if cur: p["cursor"]=cur
        d=await c.get_markets(p)
        allm+=d.get("markets",[]) or []
        cur=d.get("cursor")
        if not cur: break
    return allm

async def main():
    con=sqlite3.connect(f"{ROOT}/tradingbot.db")
    c=KalshiClient()
    # date range = resolved probe period
    d0=datetime.date(2026,5,26); d1=datetime.date(2026,6,15)
    dates=[d0+datetime.timedelta(days=i) for i in range((d1-d0).days+1)]
    recs=[]; station_map={}
    for ck in CUR:
        series=CITY_SERIES[ck]
        kms=await fetch_series(c,series)
        by_date={}
        for m in kms:
            tk=m.get("ticker") or ""
            mm=re.match(rf"{series}-(\d\d[A-Z]{{3}}\d\d)-",tk)
            if mm: by_date.setdefault(mm.group(1),[]).append(m)
        # station from any market's rules
        st=None
        for m in kms[:50]:
            st=parse_station(m.get("rules_primary"));
            if st: break
        scoord=STATION_COORD.get(st)
        station_map[ck]=(st,scoord)
        if not scoord:
            print(f"!! {ck}: UNMAPPED station '{st}' — skipping"); continue
        # open-meteo GFS forecast maxes at both coords across range
        cur_fc=om_forecast_maxes(*CUR[ck],dates[0].isoformat(),dates[-1].isoformat())
        st_fc =om_forecast_maxes(*scoord,dates[0].isoformat(),dates[-1].isoformat())
        for d in dates:
            sfx=suf(d); mks=by_date.get(sfx,[])
            if not mks: continue
            winner=next((m for m in mks if (m.get("result") or "").lower() in ("yes","yes_win")),None)
            if not winner: continue
            prefix=f"{series}-{sfx}-"
            rows=con.execute("SELECT market_ticker, model_probability, reasoning FROM signals "
                             "WHERE market_ticker LIKE ? AND platform='kalshi'",(prefix+"%",)).fetchall()
            if not rows: continue
            # current method A: argmax model_prob bucket
            best=max(rows,key=lambda r:(r[1] or 0)); topA=best[0]
            # ensemble mean from reasoning
            mean=None
            for _,_,rz in rows:
                mm=re.search(r"Ensemble:\s*([\d.]+)\s*F",rz or "")
                if mm: mean=float(mm.group(1)); break
            if mean is None: continue
            iso=d.isoformat(); delta=None
            if iso in cur_fc and iso in st_fc and cur_fc[iso] is not None and st_fc[iso] is not None:
                delta=st_fc[iso]-cur_fc[iso]
            if delta is None: continue
            # method B current = bucket containing mean ; corrected = bucket containing mean+delta
            pickB=next((m["ticker"] for m in mks if bucket_contains(mean,m)),None)
            pickC=next((m["ticker"] for m in mks if bucket_contains(mean+delta,m)),None)
            recs.append(dict(city=ck,date=iso,winner=winner["ticker"],mean=round(mean,1),delta=round(delta,2),
                             hitA=(topA==winner["ticker"]),hitB=(pickB==winner["ticker"]),hitC=(pickC==winner["ticker"])))
    con.close()
    recs.sort(key=lambda r:r["date"])
    print("\nstation map:",{k:(v[0],v[1]) for k,v in station_map.items()})
    n=len(recs)
    def rate(key,rs):
        rs=[r for r in rs if r.get(key) is not None];
        return (sum(1 for r in rs if r[key]),len(rs))
    a=rate("hitA",recs); b=rate("hitB",recs); cc=rate("hitC",recs)
    print(f"\ncity-days scored: {n}")
    print(f"CURRENT  method A (probe metric, argmax-prob):  {a[0]}/{a[1]} = {100*a[0]/max(a[1],1):.0f}%  (validity vs ~30% CSV)")
    print(f"CURRENT  method B (bucket@mean):                {b[0]}/{b[1]} = {100*b[0]/max(b[1],1):.0f}%  (coord-alone baseline)")
    print(f"CORRECTED (bucket@mean+GFSdelta, coord-alone):  {cc[0]}/{cc[1]} = {100*cc[0]/max(cc[1],1):.0f}%")
    # rolling-10 / recent-5 on corrected (by city-day, chronological)
    seq=[r["hitC"] for r in recs]
    r10=seq[-10:]; r5=seq[-5:]
    print(f"\nCORRECTED rolling-last-10: {sum(r10)}/{len(r10)}  | recent-5: {sum(r5)}/{len(r5)}  (GATE: >=7/10 AND >=4/5)")
    # per-city + mean |delta|
    from collections import defaultdict
    pc=defaultdict(lambda:[0,0,0,0,[]])
    for r in recs:
        pc[r["city"]][0]+=1; pc[r["city"]][1]+=r["hitA"]; pc[r["city"]][2]+=r["hitB"]; pc[r["city"]][3]+=r["hitC"]; pc[r["city"]][4].append(abs(r["delta"]))
    print("\nper-city  n  curA curB corr  mean|delta|F")
    for ck,v in pc.items():
        print(f"  {ck:12} {v[0]:2}  {v[1]:3} {v[2]:4} {v[3]:4}   {statistics.mean(v[4]):.2f}")
    json.dump(recs,open("/tmp/kalshi_rescore_recs.json","w"))
    print("\n(records -> /tmp/kalshi_rescore_recs.json)")

asyncio.run(main())
