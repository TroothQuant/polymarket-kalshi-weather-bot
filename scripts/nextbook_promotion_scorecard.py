#!/usr/bin/env python3
"""
nextbook_promotion_scorecard — READ-ONLY mechanical judge of the next-book paper
harness against the PRE-COMMITTED GO bar.

Reads ~/.local/state/trooth/nextbook_paper.db ONLY (the harness's own sandbox db).
Writes its own dated markdown report to ~/.local/state/trooth/. It NEVER touches the
live weather bot, tradingbot.db, config, flags, or the harness itself. Runnable on
demand (e.g. at the ~2026-07-01 review).

WHY THIS EXISTS
  The promotion bar was pre-committed on 2026-06-10 (weather_nextbook_calibration_
  2026-06-10.md, "GATE 4 — PRE-COMMITTED GO bar", written before any data). This script
  MECHANIZES that fixed bar so the review is push-button, not eyeballed. No pre-committed
  threshold is moved. The ONLY place the 6/10 bar was qualitative is criterion 2's
  "no systematic overconfidence (win rate broadly consistent with the model
  probabilities)"; that is pinned below to a concrete, documented test (OVERCONF_MARGIN).
  That one threshold is Code's operationalization (2026-06-21), flagged as such.

THE BAR (a city earns a live-GO recommendation only if ALL four hold over its sample):
  C1 SAMPLE       n_settled >= 15  AND  total settled across cities >= 25
  C2 CALIBRATION  within-1-bucket rate >= 73%  AND  not systematically overconfident
  C3 EDGE         mean realized edge >= +0.05  AND  aggregate realized P&L > 0
  C4 PRE-PEAK     100% of entries inside the pre-peak window (prepeak_lead_h > 0)
  Verdict: fail SAMPLE only -> EXTEND (keep observing, do NOT lower the bar);
           n>=bar and fail CALIBRATION or EDGE or PRE-PEAK -> NO-GO/iterate;
           all four pass -> PROMOTE (recommend Jonathon's live-GO).

STRATEGY NOTE (why a low hit rate is NOT a failure here):
  The harness BUYS YES on the model's favored bucket when model_p - ask >= 0.08 — a
  POSITIVE-SKEW value buy, not the live book's NO-fade. So losing most days and winning
  big occasionally is the EXPECTED shape; a hit-rate floor or an "ex-best-day" haircut
  would wrongly reject a working book. The robustness gate is forecast CALIBRATION
  (within-1 + overconfidence), which a single lucky big win cannot manufacture.

SMALL-n HONESTY:
  Even at n=15 these are thin binary samples. A PROMOTE here is PROVISIONAL — it goes
  live conservatively sized and stays monitored; re-confirm at n>=30 before treating a
  city as a permanent book member. The script prints this caveat in its report.
"""
import sqlite3, os, datetime

DB = os.path.expanduser("~/.local/state/trooth/nextbook_paper.db")
OUT_DIR = os.path.expanduser("~/.local/state/trooth")

# --- PRE-COMMITTED thresholds (2026-06-10 GATE-4 bar; DO NOT MOVE) ---
N_CITY_MIN = 15       # C1 per-city settled trades
N_TOTAL_MIN = 25      # C1 total settled across active cities
WITHIN1_MIN = 0.73    # C2a within-1-bucket calibration floor (existing-5-city control band)
EDGE_MIN = 0.05       # C3 mean realized edge per settled trade
# --- Code's operationalization of the one qualitative clause (2026-06-21, flagged) ---
OVERCONF_MARGIN = 0.10  # C2b: overconfident if win_rate < mean_model_p - this margin.
                        # Preponderance flag at small n (NOT a significance test); a
                        # binomial test has ~no power at n=10-15, so a point-estimate
                        # gap is the honest mechanization. Conservative; adjustable.


def within1(lo, hi, settle):
    """Actual settlement high within one bucket-width of the entered bucket."""
    if lo is None or hi is None or settle is None:
        return False
    w = hi - lo  # 2 for degF buckets, 1 for degC buckets
    return (lo - w) <= settle <= (hi + w)


def score_city(rows):
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    within = sum(1 for r in rows if within1(r["bucket_lo"], r["bucket_hi"], r["settle_high"]))
    within_rate = within / n
    mean_model_p = sum(r["model_p"] for r in rows) / n
    win_rate = wins / n
    mean_edge = sum(r["realized_edge"] for r in rows) / n
    total_pnl = sum(r["realized_pnl"] for r in rows)
    prepeak_ok = all((r["prepeak_lead_h"] or 0) > 0 for r in rows)
    overconf = win_rate < (mean_model_p - OVERCONF_MARGIN)
    return dict(n=n, wins=wins, win_rate=win_rate, within_rate=within_rate,
                mean_model_p=mean_model_p, mean_edge=mean_edge, total_pnl=total_pnl,
                prepeak_ok=prepeak_ok, overconf=overconf, within=within)


def verdict(s, total_n):
    c1 = (s["n"] >= N_CITY_MIN) and (total_n >= N_TOTAL_MIN)
    c2 = (s["within_rate"] >= WITHIN1_MIN) and (not s["overconf"])
    c3 = (s["mean_edge"] >= EDGE_MIN) and (s["total_pnl"] > 0)
    c4 = s["prepeak_ok"]
    if s["n"] < N_CITY_MIN or total_n < N_TOTAL_MIN:
        v = "EXTEND"          # sample short — keep observing, do not lower the bar
    elif c2 and c3 and c4:
        v = "PROMOTE"
    else:
        v = "NO-GO"
    return v, (c1, c2, c3, c4)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT city, bucket_lo, bucket_hi, model_p, ask, prepeak_lead_h, "
        "settle_high, won, realized_pnl, realized_edge "
        "FROM positions WHERE status='settled' AND realized_pnl IS NOT NULL"
    ).fetchall()
    con.close()

    by = {}
    for r in rows:
        by.setdefault(r["city"], []).append(r)
    total_n = len(rows)

    L = []
    def P(s=""):
        L.append(s)

    P(f"# Next-Book Promotion Scorecard — run {datetime.date.today().isoformat()}")
    P()
    P("READ-ONLY. Mechanizes the PRE-COMMITTED 2026-06-10 GATE-4 bar "
      "(`weather_nextbook_calibration_2026-06-10.md`). No pre-committed threshold moved.")
    P(f"Bar: C1 n>={N_CITY_MIN}/city & total>={N_TOTAL_MIN} | "
      f"C2 within-1>={WITHIN1_MIN:.0%} & not overconfident | "
      f"C3 mean realized edge>=+{EDGE_MIN:.2f} & P&L>0 | C4 100% pre-peak.")
    P(f"Verdict: fail C1 only -> EXTEND; n>=bar & fail C2/C3/C4 -> NO-GO; all pass -> PROMOTE.")
    P(f"Total settled across cities: **{total_n}** (C1 total floor {N_TOTAL_MIN}: "
      f"{'MET' if total_n >= N_TOTAL_MIN else 'NOT MET'}).")
    P()
    P("| city | n | verdict | C1 sample | C2 within-1 | C2 overconf | C3 edge | C3 P&L | C4 pre-peak |")
    P("|---|---|---|---|---|---|---|---|---|")
    promote, extend, nogo = [], [], []
    for city in sorted(by, key=lambda c: -len(by[c])):
        s = score_city(by[city])
        v, (c1, c2a, c3, c4) = verdict(s, total_n)
        {"PROMOTE": promote, "EXTEND": extend, "NO-GO": nogo}[v].append(city)
        within_cell = f"{s['within_rate']:.0%} ({s['within']}/{s['n']}) {'PASS' if s['within_rate']>=WITHIN1_MIN else 'FAIL'}"
        overc_cell = (f"win {s['win_rate']:.0%} vs model {s['mean_model_p']:.0%} "
                      f"{'OVERCONF' if s['overconf'] else 'ok'}")
        edge_cell = f"{s['mean_edge']:+.3f} {'PASS' if s['mean_edge']>=EDGE_MIN else 'FAIL'}"
        pnl_cell = f"{s['total_pnl']:+.0f} {'PASS' if s['total_pnl']>0 else 'FAIL'}"
        pp_cell = "PASS" if s["prepeak_ok"] else "FAIL"
        c1_cell = f"{s['n']}/{N_CITY_MIN} {'PASS' if s['n']>=N_CITY_MIN else 'short'}"
        P(f"| {city} | {s['n']} | **{v}** | {c1_cell} | {within_cell} | {overc_cell} "
          f"| {edge_cell} | {pnl_cell} | {pp_cell} |")
    P()
    P(f"**PROMOTE:** {', '.join(promote) or '(none)'}  |  "
      f"**EXTEND (sample short):** {', '.join(extend) or '(none)'}  |  "
      f"**NO-GO (edge/calibration fail at adequate n):** {', '.join(nogo) or '(none)'}")
    P()
    P("## Read")
    P("- A PROMOTE is PROVISIONAL: go live conservatively sized + monitored; re-confirm at "
      "n>=30 before treating the city as a permanent book member.")
    P("- EXTEND = not enough settled trades yet (the bar is NOT lowered); a city can pass "
      "every quality criterion and still be EXTEND purely on sample — that's the live "
      "candidate to watch, distinct from a NO-GO.")
    P("- Low hit rate is EXPECTED (positive-skew value buy); the robustness gate is "
      "within-1 calibration + overconfidence, which one lucky big win cannot fake.")
    P("- C2b overconfidence margin (0.10) is Code's mechanization of the 6/10 bar's one "
      "qualitative clause; flagged, conservative, adjustable. All other thresholds are the "
      "fixed pre-committed bar.")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"nextbook_promotion_scorecard_{datetime.date.today().isoformat()}.md")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n[written: {out}]")


if __name__ == "__main__":
    main()
