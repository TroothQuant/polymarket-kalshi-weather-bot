# VERDICT 2026-06-17: refined-#8 KILLED. Calm-summer Gaussian screen (exact-z, CLOB-max prices, real spreads) = NEGATIVE net (ALL -7.1% ROI, z>=2.5 band -11.1%, non-NYC breadth net -$0.64). Per the locked optimism-asymmetry rule, negative-on-easy-regime = robust kill. READ-ONLY. Durable gotchas: (1) gamma closed events are OLDEST-first -> use order=endDate&ascending=false; (2) weather markets are negRisk -> orderbook subgraph MISSES fills, use CLOB prices-history?interval=max (1d/1h return empty); (3) forecast lookup needs city ALIAS not key. See session_log_2026-06-16.md.
"""(c) SCREEN — Polymarket internal-bucket conviction-gated NO-fade, READ-ONLY.
Gaussian model_yes from REAL logged mean+std (screen only; decides kill-or-escalate, never money).
conviction-z EXACT (logged mean+std). Entry prices from Goldsky subgraph (earliest fill on target date,
no look-ahead). REAL per-bucket spreads from a live per-(city,price-band) profile. Settle vs gamma.
Discipline: independent CITY-DAYS, OOS split, per-z-band + per-city; tail-realization flagged.
CALM-SUMMER ONLY (logged forecasts start ~late May) -> negative=ROBUST KILL; positive=escalate to GEFS-S3 exact."""
import urllib.request, json, re, math, statistics, sqlite3, datetime
from collections import defaultdict

ROOT="/home/trooth/Projects/trooth-weather-bot"
CITY={"nyc":"nyc-daily-weather","chicago":"chicago-daily-weather","miami":"miami-daily-weather",
      "los_angeles":"los-angeles-daily-weather","denver":"denver-daily-weather"}
SUB="https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/prod/gn"
EDGE_LO,EDGE_HI=0.25,0.50; ENTRY_LO,ENTRY_HI=0.10,0.70; ZGATE=1.0
def get(u):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"x"}),timeout=30).read())
    except Exception: return None
def gql(q):
    try:
        r=urllib.request.Request(SUB,data=json.dumps({"query":q}).encode(),headers={"Content-Type":"application/json","User-Agent":"x"})
        return json.loads(urllib.request.urlopen(r,timeout=45).read()).get("data")
    except Exception: return None
def Phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def parr(v):
    if isinstance(v,list): return v
    if isinstance(v,str):
        try: return json.loads(v)
        except: return None
    return None
def bounds(q):
    m=re.search(r'between\s*(\d+)\s*-\s*(\d+)',q.lower())
    if m: return float(m.group(1)), float(m.group(2))+1
    return None,None

# --- live per-(city, price-band) spread profile (real spreads, applied by band) ---
def spread_profile():
    prof={}
    for ck,slug in CITY.items():
        ev=get(f"https://gamma-api.polymarket.com/events?closed=false&limit=5&series_slug={slug}")
        bands=defaultdict(list)
        if ev:
            for m in ev[0].get("markets",[]):
                q=m.get("question") or ""; fl,cp=bounds(q)
                if fl is None: continue
                op=parr(m.get("outcomePrices")) or []
                yes=float(op[0]) if op else None
                spr=m.get("spread")
                try: spr=float(spr)
                except: spr=None
                if yes is None or spr is None: continue
                b=("lo" if yes<0.10 else "mid" if yes<0.55 else "hi")
                bands[b].append(spr)
        prof[ck]={b:(statistics.median(v) if v else 0.05) for b,v in bands.items()}
    return prof
def spr_for(prof,ck,yes):
    b=("lo" if yes<0.10 else "mid" if yes<0.55 else "hi")
    return prof.get(ck,{}).get(b, 0.06)

def clob_entry_price(token, target_date_str):
    # CLOB prices-history (interval=max works for recent negRisk weather tokens; subgraph misses negRisk fills).
    # earliest price ON the target day = no-look-ahead morning entry; fallback = last price before target day.
    r=get(f"https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=60")
    if not isinstance(r,dict): return None
    h=r.get("history",[]) or []
    pts=sorted((p["t"],p["p"]) for p in h if p.get("p") is not None)
    if not pts: return None
    tgt=[(t,p) for t,p in pts if datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")==target_date_str]
    if tgt: return tgt[0][1]
    before=[(t,p) for t,p in pts if datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")<target_date_str]
    return before[-1][1] if before else None

con=sqlite3.connect(f"{ROOT}/tradingbot.db")
CITY_ALIAS={"nyc":"new york","chicago":"chicago","miami":"miami","los_angeles":"los angeles","denver":"denver"}
def forecast_for(city_key, dstr):
    # mean+std from the EARLIEST logged PM-weather signal for that city that day (forecast is per city-day)
    alias=CITY_ALIAS[city_key]
    rows=con.execute("SELECT reasoning FROM signals WHERE platform='polymarket' AND market_type='weather' "
                     "AND date(timestamp)=? AND lower(reasoning) LIKE ? ORDER BY timestamp ASC",(dstr,f"%{alias}%")).fetchall()
    for (rz,) in rows:
        m=re.search(r'Ensemble:\s*([\d.]+)F\s*\+/-\s*([\d.]+)',rz or "")
        if m: return float(m.group(1)), float(m.group(2))
    return None,None

prof=spread_profile()
print("spread profile (median by band):",{k:{b:round(s,3) for b,s in v.items()} for k,v in prof.items()})
trades=[]
for ck,slug in CITY.items():
    rev=get(f"https://gamma-api.polymarket.com/events?closed=true&limit=80&order=endDate&ascending=false&series_slug={slug}")
    if not rev: continue
    for e in rev:
        end=(e.get("endDate") or "")[:10]
        if not end or end<"2026-05-26" or end>"2026-06-16": continue   # logged-forecast (calm summer) window
        mu,sd=forecast_for(ck,end)
        if mu is None or sd is None or sd<=0: continue
        try: day0=datetime.datetime.strptime(end,"%Y-%m-%d").timestamp()-8*3600   # ~local morning
        except: continue
        for m in e.get("markets",[]):
            q=m.get("question") or ""; fl,cp=bounds(q)
            if fl is None: continue
            toks=parr(m.get("clobTokenIds")); op=parr(m.get("outcomePrices"))
            if not toks or not op: continue
            won = float(op[0])>0.5
            yes_tok=str(toks[0])
            myes=max(0.0,min(1.0, Phi((cp-mu)/sd)-Phi((fl-mu)/sd)))
            mid=(fl+cp-1)/2.0; z=abs(mu-mid)/sd
            mkt=clob_entry_price(yes_tok, end)
            if mkt is None: continue
            edge=myes-mkt
            if edge>=0: continue
            if not (EDGE_LO<=abs(edge)<=EDGE_HI): continue
            q_no=1.0-mkt
            if not (ENTRY_LO<=q_no<=ENTRY_HI): continue
            if z<ZGATE: continue
            sp=spr_for(prof,ck,mkt); cost=q_no+sp/2.0
            net=(1.0-cost) if not won else -cost
            trades.append(dict(city=ck,date=end,fl=int(fl),cp=int(cp-1),mkt=round(mkt,3),myes=round(myes,3),
                               z=round(z,2),qno=round(q_no,3),sp=round(sp,3),won=won,net=round(net,4)))
con.close()

print(f"\n==== (c) SCREEN: PM internal-bucket NO-fade — Gaussian model_yes, exact-z, real spreads, net (CALM-SUMMER ONLY) ====")
print(f"raw trades={len(trades)}")
if not trades:
    print("NO qualifying internal-bucket fades in window."); raise SystemExit
def block(rs,label):
    if not rs: print(f"  {label}: none"); return
    cd=len(set((t['city'],t['date']) for t in rs)); wins=sum(1 for t in rs if not t['won'])
    net=sum(t['net'] for t in rs); staked=sum(t['qno']+t['sp']/2 for t in rs)
    realized=any(t['won'] for t in rs)  # tail-realization present?
    print(f"  {label:22} trades={len(rs):3} city-days={cd:3} NO-win={100*wins/len(rs):4.0f}% net=${net:+7.2f} "
          f"ROI={100*net/staked if staked else 0:+5.1f}% tail-realized={realized}")
block(trades,"ALL")
print(" -- per city --")
for ck in CITY: block([t for t in trades if t['city']==ck], ck)
print(" -- per z-band --")
for lab,lo,hi in (("z 1.0-1.5",1.0,1.5),("z 1.5-2.5",1.5,2.5),("z>=2.5",2.5,99)):
    block([t for t in trades if lo<=t['z']<hi], lab)
print(" -- non-NYC net-tradeable count --")
nn=[t for t in trades if t['city']!='nyc']; nncd=len(set((t['city'],t['date']) for t in nn))
print(f"  non-NYC fades={len(nn)} across {nncd} city-days; net=${sum(t['net'] for t in nn):+.2f}")
json.dump(trades,open("/tmp/pm_internal_screen_trades.json","w"))
