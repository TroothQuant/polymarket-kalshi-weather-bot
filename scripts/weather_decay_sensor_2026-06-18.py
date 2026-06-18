#!/usr/bin/env python3
"""
weather_decay_sensor — READ-ONLY leading indicator that the weather edge is eroding.

Reads tradingbot.db (signals + trades) ONLY. Writes a dated markdown report to
~/.local/state/trooth/. Does NOT touch the live bot, config, flags, DB schema, or the
next-book harness. Runnable on demand.

CORE METRIC (not settlement-limited — uses EVERY logged signal, traded or not):
  rolling weekly mean( market_price - model_probability ) for high-z tail buckets (z>=1),
  per platform and per city + pooled. The harvestable overpricing of the tail; if its
  magnitude SHRINKS over time, the market is getting efficient => our edge is decaying.

Also: OLS edge-on-time slope (+t-stat) on the weekly series, and a CUSUM on rolling
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

    # platform -> week -> list[(mispricing, city, overpriced_bool)]
    data = {"kalshi": {}, "polymarket": {}}
    wk_signal_counts = {"kalshi": {}, "polymarket": {}}
    for plat, tk, ts, mkt, mod, rz in rows:
        if plat not in data: continue
        wk_signal_counts[plat][isoweek(ts)] = wk_signal_counts[plat].get(isoweek(ts), 0) + 1
        z = parse_z(rz)
        if z is None or z < ZGATE:
            continue
        mis = mkt - mod                 # harvestable tail overpricing
        wk = isoweek(ts)
        data[plat].setdefault(wk, []).append((mis, city_of(rz), mkt > mod))

    lines = []
    def P(s=""): lines.append(s)
    P(f"# Weather Edge Decay Sensor — run {datetime.date.today().isoformat()}")
    P()
    P("READ-ONLY. Metric = weekly mean(market_price − model_probability) for high-z (z≥1) tail signals.")
    P("Positive = market overprices the tail (our harvest); **shrinking toward 0 over weeks = edge decaying**.")
    P("Power caveat: ~5 weeks of data + small PM settled n → treat slope/CUSUM as EARLY-WARNING flags, not proofs.")
    P()
    for plat in ("kalshi", "polymarket"):
        tag = "CONTINUOUS live stream" if plat == "kalshi" else "HISTORICAL / parser-degraded (do not read as live)"
        P(f"## {plat.upper()} — {tag}")
        weeks = sorted(data[plat].keys())
        if not weeks:
            P("  (no z≥1 tail signals)"); P(); continue
        P(f"| week | z≥1 sigs | all-gap | overpriced-only gap | n_overpriced | total wk sigs |")
        P(f"|---|---|---|---|---|---|")
        series_x, series_y = [], []
        for i, wk in enumerate(weeks):
            vals = data[plat][wk]
            allgap = statistics.mean(v[0] for v in vals)
            over = [v[0] for v in vals if v[2]]
            ovgap = statistics.mean(over) if over else 0.0
            series_x.append(i); series_y.append(ovgap)
            P(f"| {wk} | {len(vals)} | {allgap:+.3f} | {ovgap:+.3f} | {len(over)} | {wk_signal_counts[plat].get(wk,0)} |")
        b, t, n = ols_slope_t(series_x, series_y)
        if b is None:
            P(f"  trend: n={n} weeks — too few to regress.")
        else:
            direction = "DECAYING ↓" if b < 0 else "stable/strengthening ↑"
            tstr = f"{t:+.2f}" if t not in (None, float('inf')) else "n/a"
            P(f"  **trend (overpriced-gap on week): slope={b:+.4f}/wk, t={tstr}, n={n} weeks → {direction}**")
            P(f"  first week {series_y[0]:+.3f} → last week {series_y[-1]:+.3f}")
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
    # quick pooled verdict
    kw = sorted(data["kalshi"].keys())
    if len(kw) >= 3:
        ky = [statistics.mean([v[0] for v in data["kalshi"][w] if v[2]] or [0]) for w in kw]
        kb, kt, _ = ols_slope_t(list(range(len(kw))), ky)
        if kb is not None:
            P(f"- Kalshi (live) overpriced-tail gap is currently {ky[-1]:+.3f}; slope {kb:+.4f}/wk "
              f"→ {'watch: trending down' if kb < 0 else 'holding'}. The leading indicator to watch week-over-week.")
    P("- Re-run this on demand; the value is the TREND across many runs, not any single reading.")
    P("- If the overpriced-tail gap compresses toward 0 over successive weeks (esp. on Kalshi, the live stream), "
      "that is sophistication entering — shrink size before realized P&L confirms it.")
    con.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"weather_decay_sensor_{datetime.date.today().isoformat()}.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n[written: {out}]")

if __name__ == "__main__":
    main()
