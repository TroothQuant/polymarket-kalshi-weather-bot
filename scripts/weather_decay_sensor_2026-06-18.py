#!/usr/bin/env python3
"""
weather_decay_sensor — READ-ONLY leading indicator that the weather edge is eroding.

Reads tradingbot.db (signals + trades) ONLY. Writes a dated markdown report to
~/.local/state/trooth/. Does NOT touch the live bot, config, flags, DB schema, or the
next-book harness. Runnable on demand.

PRIMARY METRIC (corrected 2026-06-18): the IN-BAND FRACTION of high-z signals =
  (# z>=1 signals with market_yes in [0.30, 0.50]) / (# z>=1 signals), per week,
  per platform and per city + pooled. A FALLING fraction over time = the market is
  pricing tails OUT of our information-edge band (correcting toward our model) = decay.

  WHY this replaced the old primary (mean(model−market) inside the band): that mean was
  SELF-CENSORING — when the market corrects a tail, that signal's price drops below 0.30
  and EXITS the band, so the in-band mean stays ~flat by construction and under-detects
  decay. The fraction catches exactly the signals the mean loses. (Mean-gap kept as a
  confirming SECONDARY.)

Plus a CUSUM on rolling realized ROI from settled trades.

POWER CAVEAT (in the report): PM settled n≈45 and only ~5 weeks exist → slope/CUSUM are
EARLY-WARNING flags, not proofs. The signal-level fraction has large n (88k Kalshi / 10k PM).

VENUE NOTE: the live PM market stream is parser-/variance-degraded, so the continuous live
signal comes from KALSHI (its FLB is the cleanest-documented version of our edge). PM and
Kalshi are computed and labeled SEPARATELY, never conflated.
"""
import sqlite3, re, os, math, statistics, datetime

DB = os.path.expanduser("~/Projects/trooth-weather-bot/tradingbot.db")
OUT_DIR = os.path.expanduser("~/.local/state/trooth")
ZGATE = 1.0
BAND = (0.30, 0.50)

def parse_z(rz):
    me = re.search(r'Ensemble:\s*([\d.]+)\s*F\s*\+/-\s*([\d.]+)', rz or '')
    th = re.search(r'(above|below)\s*([\d.]+)\s*F', rz or '')
    if me and th and float(me.group(2)) > 0:
        return abs(float(me.group(1)) - float(th.group(2))) / float(me.group(2))
    return None

def city_of(rz):
    m = re.search(r'\]\s*([A-Za-z .]+?)\s+high\s+(?:above|below)', rz or '')
    return m.group(1).strip().lower() if m else 'unknown'

def isoweek(ts):
    iy, iw, _ = datetime.date.fromisoformat(ts[:10]).isocalendar()
    return f"{iy}-W{iw:02d}"

def ols_slope_t(xs, ys):
    n = len(xs)
    if n < 3: return None, None, n
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0: return None, None, n
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    sse = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    if sse == 0: return b, float('inf'), n
    se = math.sqrt(sse / (n - 2) / sxx)
    return b, (b / se if se > 0 else None), n

def main():
    con = sqlite3.connect(DB)  # read-only usage
    rows = con.execute(
        "SELECT platform, timestamp, market_price, model_probability, reasoning "
        "FROM signals WHERE market_type='weather' AND market_price IS NOT NULL AND model_probability IS NOT NULL"
    ).fetchall()

    data = {"kalshi": {}, "polymarket": {}}  # plat -> week -> list[(market_yes, model_prob, city)] for z>=1
    for plat, ts, mkt, mod, rz in rows:
        if plat not in data: continue
        z = parse_z(rz)
        if z is None or z < ZGATE: continue
        data[plat].setdefault(isoweek(ts), []).append((mkt, mod, city_of(rz)))

    L = []
    def P(s=""): L.append(s)
    P(f"# Weather Edge Decay Sensor — run {datetime.date.today().isoformat()}")
    P()
    P("READ-ONLY. **PRIMARY = in-band fraction** = (z≥1 signals with market_yes∈[0.30,0.50]) / (all z≥1 signals), weekly.")
    P("A **FALLING** fraction = market pricing tails OUT of our information-edge band (correcting toward our model) = **decay**.")
    P("(Replaces the old in-band mean-gap primary, which self-censored: corrected tails exit the band, so the mean stayed flat")
    P("and under-detected decay. Mean-gap retained as confirming SECONDARY.) Power caveat: ~5 weeks + small PM settled n →")
    P("slope/CUSUM are EARLY-WARNING flags, not proofs; the fraction itself has large n.")
    P()
    for plat in ("kalshi", "polymarket"):
        tag = "CONTINUOUS live stream" if plat == "kalshi" else "HISTORICAL / parser-degraded (do not read as live)"
        P(f"## {plat.upper()} — {tag}")
        weeks = sorted(data[plat].keys())
        if not weeks:
            P("  (no z≥1 signals)"); P(); continue
        flat = [r for wk in weeks for r in data[plat][wk]]
        P("### PRIMARY — in-band fraction (FALLING = decay)")
        P("| week | z≥1 total | in-band n | **in-band fraction** | mean(model−market) [secondary] |")
        P("|---|---|---|---|---|")
        fx, fy = [], []
        for i, wk in enumerate(weeks):
            tot = len(data[plat][wk])
            inb = [(mkt, mod) for (mkt, mod, c) in data[plat][wk] if BAND[0] <= mkt < BAND[1]]
            frac = len(inb) / tot if tot else 0.0
            fx.append(i); fy.append(frac)
            mg = statistics.mean(mod - mkt for (mkt, mod) in inb) if inb else 0.0
            P(f"| {wk} | {tot} | {len(inb)} | **{100*frac:.1f}%** | {mg:+.3f} |")
        b, t, n = ols_slope_t(fx, fy)
        if b is None:
            P(f"  trend: n={n} weeks — too few to regress.")
        else:
            tstr = f"{t:+.2f}" if t not in (None, float('inf')) else "n/a"
            verdict = "DECAYING — fraction falling ↓" if b < 0 else "no decay — fraction flat/rising ↑"
            P(f"  **PRIMARY trend: in-band-fraction slope={100*b:+.2f}pp/wk, t={tstr}, n={n} → {verdict}**")
            P(f"  first week {100*fy[0]:.1f}% → last {100*fy[-1]:.1f}%")
        # per-city pooled in-band fraction
        pc = []
        for c in sorted({c for (_, _, c) in flat}):
            ct = [(mkt, mod) for (mkt, mod, cc) in flat if cc == c]
            ci = [1 for (mkt, mod) in ct if BAND[0] <= mkt < BAND[1]]
            if ct: pc.append(f"{c}={100*len(ci)/len(ct):.0f}% (n{len(ct)})")
        P("  per-city pooled in-band fraction: " + ", ".join(pc))
        P()
    # CUSUM on settled PM trade ROI
    P("## SECONDARY — CUSUM on realized ROI, settled PM weather trades (small n; early-warning only)")
    tr = con.execute(
        "SELECT pnl, size FROM trades WHERE platform='polymarket' AND market_type='weather' "
        "AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    rois = [p / (s or 50) for p, s in tr]
    if len(rois) >= 5:
        mu = statistics.mean(rois); cs, path = 0.0, []
        for r in rois: cs += (r - mu); path.append(cs)
        P(f"  n={len(rois)} settled, mean ROI={mu:+.3f}. CUSUM range [{min(path):+.2f}, {max(path):+.2f}]; "
          f"recent drift (last ~6) {path[-1]-path[max(0,len(path)-6)]:+.2f}.")
    else:
        P(f"  only {len(rois)} settled — insufficient.")
    P()
    P("## Read")
    kw = sorted(data["kalshi"].keys())
    kf = [(w, len([1 for (mkt, _, _) in data["kalshi"][w] if BAND[0] <= mkt < BAND[1]]) / len(data["kalshi"][w]))
          for w in kw if data["kalshi"][w]]
    if len(kf) >= 3:
        kb, _, _ = ols_slope_t(list(range(len(kf))), [f for (_, f) in kf])
        P(f"- Kalshi (live) PRIMARY in-band fraction: {[(w, f'{100*f:.0f}%') for (w, f) in kf]}; "
          f"slope {100*kb:+.2f}pp/wk → {'WATCH: falling = decay' if (kb is not None and kb < 0) else 'flat/rising = NO decay evident'}.")
    P("- Decay = the fraction FALLING week-over-week (tails priced out of [0.30,0.50] toward our model). Re-run on demand; trend > any single reading.")
    P("- If the in-band fraction trends down on Kalshi (the live stream), fast forecast-bots are arbing our information edge — shrink size before realized P&L confirms it.")
    con.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"weather_decay_sensor_{datetime.date.today().isoformat()}.md")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n[written: {out}]")

if __name__ == "__main__":
    main()
