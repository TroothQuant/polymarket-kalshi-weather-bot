"""Pre-validation for the (c) Gaussian screen, READ-ONLY:
 (1) Gaussian-vs-empirical error bound — today's live 31 members vs Normal(mean,std) per bucket (condition 2).
 (2) Subgraph fill availability for resolved PM weather INTERNAL buckets (the price-source linchpin)."""
import urllib.request, json, re, math, statistics

CITY={"nyc":(40.7128,-74.0060,"nyc-daily-weather"),"chicago":(41.8781,-87.6298,"chicago-daily-weather"),
      "miami":(25.7617,-80.1918,"miami-daily-weather"),"los_angeles":(33.9425,-118.4081,"los-angeles-daily-weather"),
      "denver":(39.7392,-104.9903,"denver-daily-weather")}
SUB="https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/prod/gn"
def get(u):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"x"}),timeout=30).read())
    except Exception as e: return None
def gql(q):
    try:
        r=urllib.request.Request(SUB,data=json.dumps({"query":q}).encode(),headers={"Content-Type":"application/json","User-Agent":"x"})
        return json.loads(urllib.request.urlopen(r,timeout=40).read()).get("data")
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
    if m: return float(m.group(1)), float(m.group(2))+1   # [floor, cap+1) integer convention
    return None,None

print("=== (1) GAUSSIAN vs EMPIRICAL error bound (today's live 31 members) ===")
allerr=[]; tailerr=[]
for ck,(lat,lon,slug) in CITY.items():
    today="2026-06-17"
    em=get(f"https://ensemble-api.open-meteo.com/v1/ensemble?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&start_date={today}&end_date={today}&models=gfs_seamless")
    if not em: print(f"  {ck}: no ensemble"); continue
    d=em.get("daily",{}); members=[d[k][0] for k in d if "temperature_2m_max" in k and d.get(k) and d[k][0] is not None]
    if len(members)<10: print(f"  {ck}: only {len(members)} members"); continue
    mu=statistics.mean(members); sd=statistics.stdev(members)
    ev=get(f"https://gamma-api.polymarket.com/events?closed=false&limit=5&series_slug={slug}")
    buckets=[]
    if ev:
        for m in ev[0].get("markets",[]):
            q=m.get("question") or ""; fl,cp=bounds(q)
            if fl is None: continue
            emp=sum(1 for h in members if fl<=h<cp)/len(members)
            gau=Phi((cp-mu)/sd)-Phi((fl-mu)/sd) if sd>0 else 0
            err=gau-emp; buckets.append((fl,cp,emp,gau,err))
            allerr.append(abs(err))
            if emp<0.15: tailerr.append(err)   # tail buckets
    print(f"  {ck}: mu={mu:.1f} sd={sd:.2f} n_members={len(members)} buckets={len(buckets)}")
    for fl,cp,emp,gau,err in sorted(buckets,key=lambda x:x[0]):
        tag=" TAIL" if emp<0.15 else ""
        print(f"      [{int(fl)}-{int(cp-1)}] emp={emp:.3f} gauss={gau:.3f} err(g-e)={err:+.3f}{tag}")
if allerr:
    print(f"  --> |error| median={statistics.median(allerr):.3f} max={max(allerr):.3f}")
if tailerr:
    print(f"  --> TAIL buckets (emp<0.15): mean signed err(gauss-emp)={statistics.mean(tailerr):+.3f} "
          f"(NEGATIVE = Gaussian UNDER-states tail prob = OVERSTATES NO-fade edge = optimistic screen, as expected); n={len(tailerr)}")

print("\n=== (2) SUBGRAPH fill availability for resolved PM weather INTERNAL buckets ===")
import datetime
for ck,(lat,lon,slug) in list(CITY.items())[:2]:
    rev=get(f"https://gamma-api.polymarket.com/events?closed=true&limit=4&series_slug={slug}")
    if not rev: print(f"  {ck}: no resolved events"); continue
    e=rev[0]; print(f"  {ck} resolved event: {e.get('slug')}")
    end=(e.get("endDate") or "")[:10]
    try: entry_ts=datetime.datetime.strptime(end,"%Y-%m-%d").timestamp()-12*3600
    except: entry_ts=None
    n=0
    for m in e.get("markets",[]):
        q=m.get("question") or ""; fl,cp=bounds(q)
        if fl is None: continue   # internal only
        toks=parr(m.get("clobTokenIds"));
        if not toks: continue
        yes_tok=str(toks[0])
        # query subgraph fills near entry for this token
        lo=int(entry_ts-3*86400); hi=int(entry_ts+3*86400)
        fills=0; price=None; best=1e18
        for fld in ("makerAssetId","takerAssetId"):
            dd=gql('{ orderFilledEvents(first:1000,where:{%s:"%s",timestamp_gte:%d,timestamp_lte:%d}){ makerAssetId takerAssetId makerAmountFilled takerAmountFilled timestamp } }'%(fld,yes_tok,lo,hi))
            if dd:
                for f in dd["orderFilledEvents"]:
                    mA,tA=f["makerAssetId"],f["takerAssetId"]; ma,ta=int(f["makerAmountFilled"]),int(f["takerAmountFilled"])
                    p=None
                    if mA=="0" and tA==yes_tok and ta: p=ma/ta
                    elif tA=="0" and mA==yes_tok and ma: p=ta/ma
                    if p is not None:
                        fills+=1; dt=abs(int(f["timestamp"])-entry_ts)
                        if dt<best: best=dt; price=round(p,4)
        if n<4:
            op=parr(m.get("outcomePrices")); print(f"      [{int(fl)}-{int(cp-1)}] tok=…{yes_tok[-6:]} fills={fills} entry_px={price} resolved={op}")
        n+=1
