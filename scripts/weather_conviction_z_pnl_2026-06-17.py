# G (2026-06-17, READ-ONLY): re-validate the armed live WEATHER_MIN_CONVICTION_Z=1.0 on realized
# P&L/ROI (not win-rate). Bins the 45 live Polymarket-weather trades (pre/post arm; pre-arm span both
# z bins, no gate censoring) by EXACT conviction-z derived from each trade's signal reasoning
# (|ensemble_mean - threshold|/std). RESULT: z=1.0 validated on P&L (z<1.0 = -52% ROI / z>=1.0 = +38%);
# optimum looks higher (~z>=1.5; the 1.0-1.5 band is -40%, n=9, calm-only = suggestive not conclusive).
# Guardrail 1: read-only — no live-gate change without shadow/paper-first + Jonathon in the loop.
import sqlite3,re
from collections import defaultdict
con=sqlite3.connect('tradingbot.db'); con.row_factory=sqlite3.Row
rows=con.execute("SELECT t.pnl,t.size,t.event_slug,date(t.timestamp) d, s.reasoning FROM trades t JOIN signals s ON t.signal_id=s.id WHERE t.platform='polymarket' AND t.market_type='weather' AND t.pnl IS NOT NULL ORDER BY t.timestamp").fetchall()
def zof(rz):
    me=re.search(r'Ensemble:\s*([\d.]+)F\s*\+/-\s*([\d.]+)',rz or ''); th=re.search(r'(above|below)\s*([\d.]+)\s*F',rz or '')
    if me and th and float(me.group(2))>0: return abs(float(me.group(1))-float(th.group(2)))/float(me.group(2))
    return None
def cityday(slug):
    m=re.search(r'in-([a-z-]+)-on-(\w+-\d+)',slug or ''); return (m.group(1),m.group(2)) if m else (slug,'')
recs=[dict(z=zof(r['reasoning']),pnl=r['pnl'],size=r['size'] or abs(r['pnl']),won=(r['pnl']>0),cd=cityday(r['event_slug'])) for r in rows]
recs=[r for r in recs if r['z'] is not None]
def agg(rs,lab):
    if not rs: print(f'  {lab:16} n=0'); return
    cd=len(set(r['cd'] for r in rs)); pnl=sum(r['pnl'] for r in rs); st=sum(r['size'] for r in rs); w=sum(1 for r in rs if r['won'])
    print(f'  {lab:16} n={len(rs):2} city-days={cd:2} P&L=${pnl:+8.2f} ROI={100*pnl/st if st else 0:+6.1f}% win%={100*w/len(rs):3.0f}')
for lab,lo,hi in (('z<0.5',0,.5),('z0.5-1.0',.5,1),('z1.0-1.5',1,1.5),('z1.5-2.5',1.5,2.5),('z>=2.5',2.5,99)): agg([r for r in recs if lo<=r['z']<hi],lab)
print('  --- THE CUT z=1.0 ---'); agg([r for r in recs if r['z']<1],'z<1.0 gated-out'); agg([r for r in recs if r['z']>=1],'z>=1.0 kept')
con.close()
