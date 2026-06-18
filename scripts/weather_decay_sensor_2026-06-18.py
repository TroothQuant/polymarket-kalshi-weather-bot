#!/usr/bin/env python3
"""
weather_decay_sensor — READ-ONLY leading indicator that the weather edge is eroding.

Reads tradingbot.db (signals + trades) ONLY. Writes a dated markdown report to
~/.local/state/trooth/. Does NOT touch the live bot, config, flags, DB schema, or the
next-book harness. Runnable on demand.

PRIMARY METRIC (per the 2026-06-18 entry-band finding — our edge lives in the market_yes
0.30-0.50 INFORMATION-EDGE band, NOT the FLB longshot band the 0.70 cap excludes):
  rolling weekly mean( model_probability - market_price ) for z>=1 signals with
  market_yes in [0.30, 0.50], per platform and per city + pooled. This is the model-vs-market
  disagreement that IS our information edge; if its magnitude (and the in-band signal COUNT)
  SHRINK over time, fast forecast-bots are arbitraging our edge away => the decay we most need
  to see coming. (not settlement-limited — uses EVERY logged signal, traded or not.)

SECONDARY METRIC: generic high-z (z>=1) tail overpricing mean( market_price - model_probability )
  pooled across all bands — the broader FLB-style tail-overpricing gauge.

Also: OLS gap-on-time slope (+t-stat) on the weekly series, and a CUSUM on rolling
realized ROI from settled trades (where they exist).

POWER CAVEAT (printed in the report): the PM *settled* sample is small (~45) and only ~5
weeks of data exist, so the slope / CUSUM are EARLY-WARNING flags, not proofs. The
signal-level mispricing series has large n (88k Kalshi / 10k PM) and is the trustworthy part.

VENUE NOTE: per the 2026-06-18 parser finding, the live PM market stream is degraded/
intermittent, so the continuous live signal comes mostly from KALSHI (still ~23 sig/cycle;
its FLB is the cleanest-documented version of our edge). PM and Kalshi are computed and
labeled SEPARATELY and never conflated.
"""
import sqlite3, re, os, math, statistics, datetime

DB = os.path.expanduser("~/Projects/trooth-weather-bot/tradingbot.db")
OUT_DIR = os.path.expanduser("~/.local/state/trooth")
ZGATE = 1.0

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
    con = sqlite3.connect(DB)  # read-only usage; no writes to this connection
    rows = con.execute(
        "SELECT platform, market_ticker, timestamp, market_price, model_probability, reasoning "
        "FROM signals WHERE market_type='weather' AND market_price IS NOT NULL AND model_probability IS NOT NULL"
    ).fetchall()

    # platform -> week -> list[(market_yes, model_prob, city)]  for z>=1
    data = {"kalshi": {}, "polymarket": {}}
    wk_signal_counts = {"kalshi": {}, "polymarket": {}}
    for plat, tk, ts, mkt, mod, rz in rows:
        if plat not in data: continue
        wk = isoweek(ts)
        wk_signal_counts[plat][wk] = wk_signal_counts[plat].get(wk, 0) + 1
        z = parse_z(rz)
        if z is None or z < ZGATE:
            continue
        data[plat].setdefault(wk, []).append((mkt, mod, city_of(rz)))

    BAND = (0.30, 0.50)
    lines = []
    def P(s=""): lines.append(s)
    P(f"# Weather Edge Decay Sensor — run {datetime.date.today().isoformat()}")
    P()
    P("READ-ONLY. **PRIMARY** = weekly mean(model_prob − market_yes) for z≥1 signals in the")
    P("market_yes **0.30–0.50 information-edge band** (where the 2026-06-18 entry-band analysis showed our edge lives).")
    P("Gap is negative (model<market = our forecast disagreement). **Magnitude shrinking toward 0, OR the in-band COUNT")
    P("shrinking, = fast forecast-bots arbing our edge = the decay we most need to see.** SECONDARY = generic z≥1 tail overpricing.")
    P("Power caveat: ~5 weeks + small PM settled n → slope/CUSUM are EARLY-WARNING flags, not proofs.")
    P()
    for plat in ("kalshi", "polymarket"):
        tag = "CONTINUOUS live stream" if plat == "kalshi" else "HISTORICAL / parser-degraded (do not read as live)"
        P(f"## {plat.upper()} — {tag}")
        weeks = sorted(data[plat].keys())
        if not weeks:
            P("  (no z≥1 signals)"); P(); continue
        flat = [r for wk in weeks for r in data[plat][wk]]
        P("### PRIMARY — 0.30–0.50 information-edge band, mean(model − market)")
        P("| week | in-band n | mean(model−market) | mean\\|gap\\| | total wk sigs |")
        P("|---|---|---|---|---|")
        bx, by = [], []
        for i, wk in enumerate(weeks):
            inb = [(mkt, mod) for (mkt, mod, c) in data[plat][wk] if BAND[0] <= mkt < BAND[1]]
            if inb:
                gaps = [mod - mkt for (mkt, mod) in inb]
                mg = statistics.mean(gaps); bx.append(i); by.append(mg)
                P(f"| {wk} | {len(inb)} | {mg:+.3f} | {statistics.mean(abs(g) for g in gaps):.3f} | {wk_signal_counts[plat].get(wk,0)} |")
            else:
                P(f"| {wk} | 0 | — | — | {wk_signal_counts[plat].get(wk,0)} |")
        b, t, n = ols_slope_t(bx, by)
        if b is None:
            P(f"  trend: n={n} in-band weeks — too few to regress.")
        else:
            tstr = f"{t:+.2f}" if t not in (None, float('inf')) else "n/a"
            P(f"  **trend (gap on week): slope={b:+.4f}/wk, t={tstr}, n={n} → "
              f"{'DECAYING — gap closing toward 0 ↓' if b > 0 else 'gap holding/widening ↑'}**")
            P(f"  first in-band week {by[0]:+.3f} → last {by[-1]:+.3f}")
        cities = sorted({c for (_, _, c) in flat})
        pc = []
        for c in cities:
            g = [mod - mkt for (mkt, mod, cc) in flat if cc == c and BAND[0] <= mkt < BAND[1]]
            if g: pc.append(f"{c}={statistics.mean(g):+.2f}(n{len(g)})")
        P("  per-city in-band gap: " + (", ".join(pc) if pc else "none"))
        P()
        P("### SECONDARY — generic z≥1 tail overpricing, mean(market − model)")
        P("| week | z≥1 n | mean(market−model) |")
        P("|---|---|---|")
        for wk in weeks:
            vals = data[plat][wk]
            P(f"| {wk} | {len(vals)} | {statistics.mean(mkt - mod for (mkt, mod, c) in vals):+.3f} |")
        P()

    # CUSUM on settled PM trade ROI (small n — early warning only)
    P("## CUSUM — realized ROI on settled PM weather trades (small n; early-warning only)")
    tr = con.execute(
        "SELECT pnl, size FROM trades WHERE platform='polymarket' AND market_type='weather' "
        "AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    rois = [p / (s or 50) for p, s in tr]
    if len(rois) >= 5:
        mu = statistics.mean(rois)
        cs, path = 0.0, []
        for r in rois:
            cs += (r - mu); path.append(cs)
        P(f"  n={len(rois)} settled, mean ROI={mu:+.3f}. CUSUM range [{min(path):+.2f}, {max(path):+.2f}].")
        recent = path[-1] - path[max(0, len(path) - 6)]
        P(f"  recent drift (last ~6 trades): {recent:+.2f}  ({'down → possible erosion' if recent < 0 else 'up/flat'}).")
        P("  (A sustained one-directional CUSUM drift flags a regime break; at n≈45 it is suggestive, not proof.)")
    else:
        P(f"  only {len(rois)} settled trades — insufficient for CUSUM.")
    P()
    P("## Read")
    kw = sorted(data["kalshi"].keys())
    ky = []
    for w in kw:
        inb = [mod - mkt for (mkt, mod, c) in data["kalshi"][w] if BAND[0] <= mkt < BAND[1]]
        if inb: ky.append((w, statistics.mean(inb), len(inb)))
    if len(ky) >= 3:
        kb, _, _ = ols_slope_t(list(range(len(ky))), [g for (_, g, _) in ky])
        w, cur, cn = ky[-1]
        verdict = "WATCH: gap closing toward 0 = possible decay" if (kb is not None and kb > 0) else "holding"
        P(f"- Kalshi (live) in-band gap currently {cur:+.3f} (n={cn} in-band sigs, wk {w}); "
          f"slope {kb:+.4f}/wk → {verdict}.")
        P(f"  In-band COUNT trend also matters: {[(w_, n_) for (w_, _, n_) in ky]} — a shrinking count means the "
          f"market is pricing these tails below 0.30 (correcting toward our model) = the same decay.")
    P("- Re-run on demand; the value is the TREND across many runs, not any single reading.")
    P("- If the in-band gap magnitude compresses toward 0 OR the in-band signal count shrinks over successive weeks "
      "(esp. Kalshi, the live stream), that is fast forecast-bots arbing our information edge — shrink size before realized P&L confirms it.")
    con.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"weather_decay_sensor_{datetime.date.today().isoformat()}.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n[written: {out}]")

if __name__ == "__main__":
    main()
