# NOTE (2026-06-17): bespoke audit re-score, READ-ONLY. Known fidelity gaps (Gaussian bucket prob + distance-to-edge conviction-z proxy) — numbers are PROVISIONAL, do NOT count toward the Kalshi re-enable bar until superseded by the canonical validated harness (build D). See session_log_2026-06-16.md.
"""
Read-only OOS profitability test: conviction-gated NO-fade on resolved Kalshi weather
buckets, using CORRECTED-station forecasts. This is how the Polymarket book actually
makes money (fade overpriced tails), not bucket-nailing. Pre-committed gates (edge band
[0.25,0.50], entry [0.10,0.70], z>=1.0) => genuinely out-of-sample on Kalshi.
Entry = EARLIEST scan per bucket per day (no look-ahead). Net of Kalshi fees. No writes.
"""
import asyncio, sys, re, json, urllib.request, math, statistics, sqlite3, datetime
sys.path.insert(0, "/home/trooth/Projects/trooth-weather-bot")
from backend.data.kalshi_client import KalshiClient
from backend.data.kalshi_markets import CITY_SERIES

ROOT="/home/trooth/Projects/trooth-weather-bot"
CUR={"nyc":(40.7128,-74.0060),"chicago":(41.8781,-87.6298),"miami":(25.7617,-80.1918),
     "los_angeles":(33.9425,-118.4081),"denver":(39.7392,-104.9903)}
STATION={"central park":(40.7789,-73.9692),"chicago midway":(41.7860,-87.7524),
 "miami international airport":(25.7959,-80.2870),"los angeles airport":(33.9425,-118.4081),
 "denver":(39.8466,-104.6562)}
EDGE_LO,EDGE_HI=0.25,0.50; ENTRY_LO,ENTRY_HI=0.10,0.70; ZGATE=1.0
MON=["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
def suf(d): return f"{d.year%100:02d}{MON[d.month-1]}{d.day:02d}"
def Phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))

def om(lat,lon,d0,d1):
    url=(f"https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         f"&start_date={d0}&end_date={d1}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto")
    for _ in range(3):
        try:
            j=json.loads(urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"x"}),timeout=40).read())
            dd=j.get("daily",{}); return dict(zip(dd.get("time",[]),dd.get("temperature_2m_max",[])))
        except Exception: pass
    return {}
def parse_station(r):
    m=re.search(r"recorded (?:in|at)\s+(.+?)(?:,|\s+for\b)", r or ""); return m.group(1).strip().lower() if m else None

def bucket_range(mk):
    ty=(mk.get("strike_type") or "").lower(); f=mk.get("floor_strike"); c=mk.get("cap_strike")
    if ty=="between": return (f, c+1)            # [floor, cap+1)  ~2F
    if ty=="greater": return (f+1, None)          # [floor+1, inf)
    if ty=="less":    return (None, c)            # (-inf, cap)
    return (None,None)
def model_yes(mu,sd,mk):
    lo,hi=bucket_range(mk)
    if sd<=0: sd=0.5
    plo=Phi((lo-mu)/sd) if lo is not None else 0.0
    phi=Phi((hi-mu)/sd) if hi is not None else 1.0
    return max(0.0,min(1.0,phi-plo))
def conv_z(mu,sd,mk):  # distance from mean to the bucket region, in std (NO-fade confidence)
    lo,hi=bucket_range(mk)
    if sd<=0: sd=0.5
    if lo is not None and hi is not None:
        if lo<=mu<hi: return 0.0
        return min(abs(mu-lo),abs(mu-hi))/sd
    if lo is not None: return max(0.0,(lo-mu))/sd   # greater
    if hi is not None: return max(0.0,(mu-hi))/sd   # less
    return 0.0

async def fetch_series(c,series):
    allm=[]; cur=None
    while True:
        p={"series_ticker":series,"limit":200}
        if cur:p["cursor"]=cur
        d=await c.get_markets(p); allm+=d.get("markets",[]) or []; cur=d.get("cursor")
        if not cur:break
    return allm

async def main():
    con=sqlite3.connect(f"{ROOT}/tradingbot.db"); c=KalshiClient()
    d0=datetime.date(2026,5,26); d1=datetime.date(2026,6,15)
    dates=[d0+datetime.timedelta(days=i) for i in range((d1-d0).days+1)]
    out={}
    for ck in CUR:
        series=CITY_SERIES[ck]; kms=await fetch_series(c,series)
        st=next((parse_station(m.get("rules_primary")) for m in kms[:50] if parse_station(m.get("rules_primary"))),None)
        scoord=STATION.get(st)
        if not scoord: print(f"!! {ck} unmapped '{st}'"); continue
        cur_fc=om(*CUR[ck],dates[0].isoformat(),dates[-1].isoformat())
        st_fc =om(*scoord,dates[0].isoformat(),dates[-1].isoformat())
        by_date={}
        for m in kms:
            mm=re.match(rf"{series}-(\d\d[A-Z]{{3}}\d\d)-",m.get("ticker") or "")
            if mm: by_date.setdefault(mm.group(1),[]).append(m)
        trades=[]
        for d in dates:
            iso=d.isoformat(); sfx=suf(d); mks=by_date.get(sfx,[])
            if not mks: continue
            if iso not in cur_fc or iso not in st_fc or cur_fc[iso] is None or st_fc[iso] is None: continue
            delta=st_fc[iso]-cur_fc[iso]
            for m in mks:
                tk=m.get("ticker"); res=(m.get("result") or "").lower()
                if res not in ("yes","yes_win","no","no_win"): continue
                won = res in ("yes","yes_win")
                # earliest signal this day for this bucket = entry (no look-ahead)
                row=con.execute("SELECT market_price, reasoning FROM signals WHERE market_ticker=? AND platform='kalshi' "
                                "ORDER BY timestamp ASC LIMIT 1",(tk,)).fetchone()
                if not row or row[0] is None: continue
                mkt_yes=float(row[0]); rz=row[1] or ""
                mm=re.search(r"Ensemble:\s*([\d.]+)\s*F\s*\+/-\s*([\d.]+)",rz)
                if not mm: continue
                mean=float(mm.group(1))+delta; sd=float(mm.group(2))   # CORRECTED-station mean (coord shift), recorded spread
                myes=model_yes(mean,sd,m); z=conv_z(mean,sd,m)
                edge=myes-mkt_yes
                if edge>=0: continue                       # NO-fade only: market overprices YES
                if not (EDGE_LO<=abs(edge)<=EDGE_HI): continue
                q=1.0-mkt_yes                               # NO entry cost
                if not (ENTRY_LO<=q<=ENTRY_HI): continue
                if z<ZGATE: continue
                gross = (mkt_yes if not won else -q)        # win if bucket did NOT hit
                fee = 0.07*q*(1-q)
                trades.append(dict(date=iso,tk=tk,mkt_yes=round(mkt_yes,3),myes=round(myes,3),
                                   z=round(z,2),q=round(q,3),won=won,net=round(gross-fee,4)))
        out[ck]=trades
    con.close()
    print("\n==== CONVICTION-GATED NO-FADE, CORRECTED-STATION, OOS (resolved Kalshi, entry=earliest scan, net fees) ====")
    print(f"gates: edge|{EDGE_LO}-{EDGE_HI}|  entry|{ENTRY_LO}-{ENTRY_HI}|  z>={ZGATE}\n")
    tot=0
    for ck,tr in out.items():
        if not tr: print(f"{ck:12} no qualifying trades"); continue
        n=len(tr); wins=sum(1 for t in tr if not t["won"]); net=sum(t["net"] for t in tr); staked=sum(t["q"] for t in tr)
        roi=100*net/staked if staked else 0
        # holdout = last 1/3 of dates
        cut=dates[int(len(dates)*0.67)].isoformat(); ho=[t for t in tr if t["date"]>=cut]
        hnet=sum(t["net"] for t in ho); hn=len(ho)
        tot+=net
        print(f"{ck:12} n={n:3} hit(NO-win)={100*wins/n:4.0f}%  net=${net:+7.2f}  ROI={roi:+5.1f}%  | holdout n={hn:2} net=${hnet:+6.2f}")
    print(f"\nTOTAL net (per $1 contracts): ${tot:+.2f}")
    json.dump(out,open("/tmp/kalshi_profit_trades.json","w"))
    print("(trades -> /tmp/kalshi_profit_trades.json)")

asyncio.run(main())
