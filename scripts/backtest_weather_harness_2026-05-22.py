#!/usr/bin/env python3
"""
Historical backtest harness for the weather signal generator.

Scope (2026-05-22 first cut):
  - Replays the bot's model probability calc against the actual ensemble
    forecast Open-Meteo would have produced for the trade's date.
  - Compares the model's prediction to the recorded settlement.
  - Outputs:
      (a) per-trade replay (model_p, market_p, settled_yes, model_correct?)
      (b) overall Brier score against the YES outcome
      (c) calibration curve: bin model_p into deciles, compare avg model_p
          vs realized win rate per bin
      (d) Brier score sensitivity to an ensemble-std inflation factor
          (sweeps 1.0 .. 2.5 in 0.25 steps) — informs STEP 3 item #1
          from the Trade #1 deep dive.

Data sources:
  - tradingbot.db: settled weather trades + their original signals
  - Open-Meteo /v1/archive (free, no key): historical ensemble forecast
    re-fetched at the same lead time the bot would have had.

Read-only. No DB writes. No trades. Idempotent.

Run from project root:
  .venv/bin/python scripts/backtest_weather_harness_2026-05-22.py
  .venv/bin/python scripts/backtest_weather_harness_2026-05-22.py --max-trades 5
  .venv/bin/python scripts/backtest_weather_harness_2026-05-22.py --inflate 1.5

LIMITATIONS (today's scaffold):
  - Open-Meteo archive returns the ERA5 reanalysis, NOT the GFS ensemble
    that the live bot uses (open-meteo's ensemble endpoint is forecast-only,
    no history). This harness uses ERA5 mean ± a synthetic std as a stand-in
    for the ensemble's mean / spread. It's a known approximation — the right
    next step is to add a GFS hindcast source (NOAA NOMADS archive or
    Open-Meteo's seasonal model for cross-check).
  - Polymarket-only for now. Kalshi adds bucket-semantics complexity that
    duplicates the live bot's recently-fixed logic; defer to next iteration.
  - archive-api.open-meteo.com is intermittently TCP-unreachable from
    consumer ISPs (Cowork sandbox: 403 through the tunnel; operator's
    home network 2026-05-23: connection timed out, server at 5.9.98.184
    on port 443 unresponsive). The NOMADS swap is the documented next-
    step source and removes this dependency.

NOMADS HINDCAST (added 2026-05-27)
  Pass --source nomads (default) to use the GEFS hindcast adapter at
  scripts/nomads_gfs_hindcast.py. Pulls 31 ensemble members from NOAA's
  S3 mirror per (date, city), uses the empirical mean + std across
  members. Pass --source openmeteo for the legacy ERA5 stand-in.

  --report PATH writes a Markdown calibration report covering:
    1. Summary
    2. Per-cohort Brier (direction × bucket)
    3. 5-bin calibration curve
    4. Std-inflation sweep
    5. Comparison to the live bot's open-meteo model prob (from DB)

OUTSTANDING (next session):
  - GFS hindcast source (NOMADS or paid Open-Meteo)
  - Kalshi support (re-use the bucket-aware path from weather_signals.py)
  - Brier-by-city + Brier-by-season slices
  - Persist results to a small SQLite file for diffing across runs
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sqlite3
import sys
import urllib.parse
import urllib.request
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# NOMADS S3 hindcast adapter (added 2026-05-27)
try:
    from nomads_gfs_hindcast import fetch_gefs_ensemble_hindcast
    HAVE_NOMADS = True
except Exception as _e:
    HAVE_NOMADS = False
    _NOMADS_IMPORT_ERROR = _e
sys.path.insert(0, str(ROOT))

# Local imports
from backend.data.weather import CITY_CONFIG  # noqa: E402

DB_PATH = ROOT / "tradingbot.db"


# ---------- City + market parsing ----------

def city_key_from_event_slug(slug: Optional[str]) -> Optional[str]:
    """
    'highest-temperature-in-chicago-on-may-22-2026' -> 'chicago'
    'highest-temperature-in-los-angeles-on-...'     -> 'los_angeles'
    'highest-temperature-in-nyc-on-...'             -> 'nyc'
    """
    if not slug:
        return None
    s = slug.lower()
    for k, cfg in CITY_CONFIG.items():
        token = cfg.get("slug_token") or k.replace("_", "-")
        if f"in-{token}-on" in s:
            return k
    # Fallback
    if "los-angeles" in s: return "los_angeles"
    if "nyc" in s or "new-york" in s: return "nyc"
    for k in ("chicago", "miami", "denver"):
        if k in s: return k
    return None


def strike_and_direction_from_reasoning(reasoning: str):
    """
    Extract (threshold_f, direction) from the bot's persisted reasoning string.

    Examples:
      'Chicago high above 66F on 2026-05-22 | ...'
      'Chicago high below 59F on 2026-05-23 | ...'
    Returns: (66, 'above') / (59, 'below') / (None, None) if unparseable.
    """
    if not reasoning:
        return None, None
    import re
    m = re.search(r"high\s+(above|below)\s+(\d+)F", reasoning)
    if not m:
        return None, None
    return int(m.group(2)), m.group(1)


def settled_to_yes(settlement_value, direction_traded=None):
    """
    settled_to_yes -> 1.0 if the market resolved YES, 0.0 if NO, None if void.

    NOTE 2026-05-27: this function previously flipped on direction_traded
    under the assumption that settlement_value was "value paid for the
    side the bot traded." In reality, backend/core/settlement.py's
    _parse_market_resolution stores settlement_value canonically in
    YES-terms: 1.0 = YES won, 0.0 = NO won, regardless of which side the
    bot bet. The flip caused NO-side trades to report inverted outcomes,
    silently breaking Brier calculations for ~half the book. Removed.
    `direction_traded` kept as an unused kwarg for backwards compat.
    """
    if settlement_value is None:
        return None
    return float(settlement_value)


# ---------- ERA5 archive (stand-in for GFS ensemble while we wait on a hindcast source) ----------

def fetch_era5_max_for_date(lat: float, lon: float, target_date: date,
                            timezone: str = "auto") -> Optional[float]:
    """
    Returns the ERA5 reanalyzed daily-max temperature in °F for target_date.
    No API key, free.
    """
    base = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": timezone,
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read())
        return float(j["daily"]["temperature_2m_max"][0])
    except Exception as e:
        print(f"  ERA5 fetch failed for {target_date}: {e}", file=sys.stderr)
        return None


def fetch_climatology_std(lat: float, lon: float, target_date: date,
                          years: int = 5) -> Optional[float]:
    """
    Synthetic 'ensemble std' = std of daily-max across the last `years` years
    on the same calendar date (±3 days window). Stand-in for the GFS ensemble
    std until we wire up a real hindcast source. May under- or over-state
    depending on local synoptic regime — empirical, not theoretical.
    """
    samples = []
    for yr_back in range(1, years + 1):
        for day_off in range(-3, 4):
            d = target_date.replace(year=target_date.year - yr_back) + timedelta(days=day_off)
            v = fetch_era5_max_for_date(lat, lon, d)
            if v is not None:
                samples.append(v)
    if len(samples) < 5:
        return None
    mean = sum(samples) / len(samples)
    return math.sqrt(sum((x - mean) ** 2 for x in samples) / (len(samples) - 1))


# ---------- Model replay ----------

def model_probability_high_above(mean_f: float, std_f: float, threshold_f: float,
                                 std_inflation: float = 1.0,
                                 clip_lo: float = 0.05, clip_hi: float = 0.95) -> float:
    """
    Same shape as the live bot's probability_high_above: P(high > threshold)
    under a normal approximation to the ensemble distribution. Returns the
    POST-clip value to match what the bot would have used.
    """
    sigma = max(std_f * std_inflation, 0.01)
    z = (mean_f - threshold_f) / sigma
    # P(high > threshold) = P(Z > -z) = 1 - Φ(-z) = Φ(z)
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(clip_lo, min(clip_hi, p))


def model_probability_high_below(mean_f: float, std_f: float, threshold_f: float,
                                 std_inflation: float = 1.0,
                                 clip_lo: float = 0.05, clip_hi: float = 0.95) -> float:
    p_above = model_probability_high_above(
        mean_f, std_f, threshold_f, std_inflation,
        clip_lo=0.0, clip_hi=1.0,  # unclamp inside, then clamp the below
    )
    return max(clip_lo, min(clip_hi, 1.0 - p_above))


# ---------- Backtest ----------

def pull_settled_polymarket_weather_trades(con: sqlite3.Connection,
                                            max_trades: Optional[int]) -> List[sqlite3.Row]:
    cur = con.cursor()
    q = """
        SELECT t.id, t.market_ticker, t.platform, t.event_slug, t.direction,
               t.entry_price, t.size, t.timestamp, t.settlement_value, t.pnl,
               t.result, t.model_probability AS bot_model_p,
               t.market_price_at_entry AS bot_market_p, t.edge_at_entry,
               s.reasoning AS signal_reasoning, s.timestamp AS signal_ts
        FROM trades t
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE t.market_type='weather' AND t.platform='polymarket'
          AND t.settled=1 AND t.settlement_value IS NOT NULL
        ORDER BY t.id
    """
    if max_trades:
        q += f" LIMIT {int(max_trades)}"
    return list(cur.execute(q))


def brier(predictions: List[float], outcomes: List[float]) -> float:
    if not predictions:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def calibration_bins(predictions: List[float], outcomes: List[float], n_bins: int = 5):
    bins = [[] for _ in range(n_bins)]
    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, o))
    out = []
    for i, b in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if not b:
            out.append((lo, hi, None, None, 0))
            continue
        avg_p = sum(x[0] for x in b) / len(b)
        win_rate = sum(x[1] for x in b) / len(b)
        out.append((lo, hi, avg_p, win_rate, len(b)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-trades", type=int, default=None,
                    help="Cap the number of trades replayed (smoke-test fast).")
    ap.add_argument("--inflate", type=float, default=1.0,
                    help="Single ensemble-std inflation factor for the headline replay.")
    ap.add_argument("--no-sweep", action="store_true",
                    help="Skip the 1.0 .. 2.5 inflation sweep.")
    ap.add_argument("--source", choices=["nomads", "openmeteo"], default="nomads",
                    help="Forecast data source. 'nomads' = NOAA GEFS hindcast via S3 "
                         "(added 2026-05-27, default). 'openmeteo' = legacy ERA5 stand-in.")
    ap.add_argument("--report", type=str, default=None,
                    help="If set, write a Markdown calibration report to this path.")
    args = ap.parse_args()

    if args.source == "nomads" and not HAVE_NOMADS:
        print(f"ERROR: --source nomads requires nomads_gfs_hindcast module: {_NOMADS_IMPORT_ERROR}",
              file=sys.stderr)
        sys.exit(1)

    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    trades = pull_settled_polymarket_weather_trades(con, args.max_trades)
    print(f"Pulled {len(trades)} settled Polymarket weather trades.")

    if not trades:
        print("No trades to replay. Exiting.")
        return

    # Build (city, threshold, direction, target_date, settled_yes, bot_model_p)
    replay_rows = []
    for t in trades:
        city = city_key_from_event_slug(t["event_slug"])
        if not city or city not in CITY_CONFIG:
            print(f"  skip #{t['id']}: city not parseable from slug {t['event_slug']!r}")
            continue

        # Date from event_slug: 'highest-temperature-in-X-on-may-DD-YYYY'
        import re
        m = re.search(r"on-([a-z]+)-(\d+)-(\d{4})", t["event_slug"] or "")
        if not m:
            print(f"  skip #{t['id']}: date not parseable from slug")
            continue
        month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        months = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]
        try:
            month = months.index(month_name[:3]) + 1
            target_date = date(year, month, day)
        except ValueError:
            print(f"  skip #{t['id']}: bad date {m.group(0)}")
            continue

        threshold, direction = strike_and_direction_from_reasoning(t["signal_reasoning"])
        if threshold is None:
            print(f"  skip #{t['id']}: strike not parseable from reasoning")
            continue

        settled_yes = settled_to_yes(t["settlement_value"], t["direction"])
        if settled_yes is None or t["result"] == "void":
            continue

        replay_rows.append({
            "trade_id": t["id"],
            "city": city,
            "target_date": target_date,
            "threshold": threshold,
            "direction": direction,
            "settled_yes": float(settled_yes),
            "bot_model_p": t["bot_model_p"],
            "bot_market_p": t["bot_market_p"],
            "bot_edge": t["edge_at_entry"],
            "direction_traded": t["direction"],
            "pnl": t["pnl"],
        })

    print(f"Parseable: {len(replay_rows)} trades enter replay.")

    skipped_in_fetch: list[tuple[int, str]] = []

    def run_replay(std_inflation: float, source: str, skip_log: Optional[list] = None):
        """source='nomads' uses the GEFS hindcast adapter against S3;
        source='openmeteo' uses ERA5 + climatology stand-in (legacy)."""
        results = []
        for r in replay_rows:
            city_cfg = CITY_CONFIG[r["city"]]
            lat, lon = city_cfg["lat"], city_cfg["lon"]

            if source == "openmeteo":
                src_mean = fetch_era5_max_for_date(lat, lon, r["target_date"])
                src_std = fetch_climatology_std(lat, lon, r["target_date"])
                if src_mean is None or src_std is None:
                    if skip_log is not None:
                        skip_log.append((r["trade_id"], "ERA5 unavailable"))
                    continue
                src_n = 1
            elif source == "nomads":
                date_str = r["target_date"].strftime("%Y-%m-%d")
                ens = fetch_gefs_ensemble_hindcast(date_str, city_cfg["name"])
                if ens is None:
                    if skip_log is not None:
                        skip_log.append((r["trade_id"], f"GEFS hindcast unavailable for {date_str} {city_cfg['name']}"))
                    continue
                src_mean = sum(ens.members) / len(ens.members)
                src_std = ens.spread_std
                src_n = ens.n_members_used
            else:
                raise ValueError(f"unknown source {source!r}")

            if r["direction"] == "above":
                model_p = model_probability_high_above(
                    src_mean, src_std, r["threshold"], std_inflation=std_inflation
                )
            else:
                model_p = model_probability_high_below(
                    src_mean, src_std, r["threshold"], std_inflation=std_inflation
                )
            results.append({**r, "source_mean": src_mean, "source_std": src_std,
                            "source_n": src_n,
                            "replay_model_p": model_p, "inflation": std_inflation})
        return results

    def per_cohort_brier(results):
        """Group by (direction_traded ∈ {yes,no}, direction ∈ {above,below})
        and compute Brier per cohort."""
        from collections import defaultdict
        groups = defaultdict(list)
        for r in results:
            groups[(r["direction_traded"], r["direction"])].append(
                (r["replay_model_p"], r["settled_yes"])
            )
        out = []
        for key in sorted(groups.keys()):
            preds = [p for p, _ in groups[key]]
            outs = [o for _, o in groups[key]]
            out.append((key[0], key[1], len(groups[key]), brier(preds, outs)))
        return out

    print(f"\n=== HEADLINE REPLAY ({args.source}, std inflation = {args.inflate}) ===")
    headline = run_replay(args.inflate, args.source, skip_log=skipped_in_fetch)
    print(f"  {'#':>3} {'city':<13} {'date':<10} {'strike':<7} {'dir':<6} "
          f"{'src_n':>5} {'src_mean':>9} {'src_std':>8} {'replay_p':>9} "
          f"{'set_yes':>7} {'bot_p':>7} {'mkt_p':>7} {'pnl':>8}")
    for r in headline:
        print(f"  #{r['trade_id']:>2} {r['city']:<13} {r['target_date']!s:<10} "
              f"{r['threshold']:<7} {r['direction']:<6} "
              f"{r['source_n']:>5} {r['source_mean']:>9.1f} {r['source_std']:>8.2f} "
              f"{r['replay_model_p']:>9.3f} {r['settled_yes']:>7.0f} "
              f"{(r['bot_model_p'] or 0):>7.3f} {(r['bot_market_p'] or 0):>7.3f} "
              f"{(r['pnl'] or 0):>+8.2f}")
    if skipped_in_fetch:
        print(f"\n  Skipped {len(skipped_in_fetch)} trades in fetch:")
        for tid, why in skipped_in_fetch:
            print(f"    #{tid}  {why}")

    preds = [r["replay_model_p"] for r in headline]
    outs = [r["settled_yes"] for r in headline]
    headline_brier = brier(preds, outs)
    print(f"\n  Brier score (replay model vs YES outcome): {headline_brier:.4f}")
    print(f"  (lower = better; 0.25 = always-50%; 0 = perfect)")

    print("\n=== PER-COHORT BRIER (direction_traded × bucket) ===")
    cohort_rows = per_cohort_brier(headline)
    print(f"  {'dir_traded':<11} {'bucket':<8} {'n':>4} {'brier':>8}")
    for dir_t, bucket, n, b in cohort_rows:
        print(f"  {dir_t:<11} {bucket:<8} {n:>4} {b:>8.4f}")

    print("\n=== 5-BIN CALIBRATION CURVE ===")
    print(f"  {'bin':<12} {'n':>4} {'avg_predicted':>14} {'actual_yes_rate':>16}")
    cal_rows = calibration_bins(preds, outs, n_bins=5)
    for lo, hi, avg_p, win_rate, n in cal_rows:
        if n == 0:
            print(f"  [{lo:.1f}-{hi:.1f}]    {n:>4} {'':>14} {'':>16}")
        else:
            print(f"  [{lo:.1f}-{hi:.1f}]    {n:>4} {avg_p:>14.3f} {win_rate:>16.3f}")

    sweep_rows = []
    if not args.no_sweep:
        print("\n=== INFLATION SWEEP ===")
        print(f"  {'inflation':>10} {'brier':>8}  (lower better)")
        for f in [round(x * 0.25 + 1.0, 2) for x in range(7)]:
            replay = run_replay(f, args.source)
            preds_s = [r["replay_model_p"] for r in replay]
            outs_s = [r["settled_yes"] for r in replay]
            b = brier(preds_s, outs_s)
            sweep_rows.append((f, len(replay), b))
            print(f"  {f:>10.2f} {b:>8.4f}")

    print("\n=== LIVE-BOT vs REPLAY COMPARISON ===")
    bot_preds = [r["bot_model_p"] for r in headline if r["bot_model_p"] is not None]
    bot_outs = [r["settled_yes"] for r in headline if r["bot_model_p"] is not None]
    bot_brier = brier(bot_preds, bot_outs)
    print(f"  Live-bot (Open-Meteo at trade entry) Brier: {bot_brier:.4f}  (n={len(bot_preds)})")
    print(f"  Replay ({args.source}) Brier:              {headline_brier:.4f}  (n={len(headline)})")
    # mean-abs-diff between bot's prob and replay's prob
    if headline:
        diffs = [abs((r["bot_model_p"] or 0) - r["replay_model_p"]) for r in headline if r["bot_model_p"] is not None]
        if diffs:
            print(f"  mean |bot_p − replay_p| across {len(diffs)} trades: {sum(diffs)/len(diffs):.3f}")

    # ── Markdown report ──────────────────────────────────────────────────
    if args.report:
        report_path = Path(args.report).expanduser()
        with open(report_path, "w") as f:
            f.write(f"# Weather-Bot Backtest Report — {args.source.upper()}\n\n")
            f.write(f"**Source:** `{args.source}`  \n")
            f.write(f"**Headline inflation:** {args.inflate}  \n")
            f.write(f"**Generated:** {datetime.utcnow().isoformat()}Z  \n\n")

            f.write("## 1. Summary\n\n")
            f.write(f"- Replay rows entered:       **{len(replay_rows)}**\n")
            f.write(f"- Trades scored:             **{len(headline)}**\n")
            f.write(f"- Trades skipped in fetch:   **{len(skipped_in_fetch)}**\n")
            f.write(f"- Headline Brier (replay):   **{headline_brier:.4f}**\n")
            f.write(f"- Live-bot Brier (DB):       **{bot_brier:.4f}** (n={len(bot_preds)})\n")
            f.write(f"- Reference: 0 = perfect, 0.25 = always-50% baseline\n\n")
            if skipped_in_fetch:
                f.write("Skipped trades:\n\n")
                for tid, why in skipped_in_fetch:
                    f.write(f"- #{tid}: {why}\n")
                f.write("\n")

            f.write("## 2. Per-cohort Brier (direction_traded × bucket)\n\n")
            f.write("| direction_traded | bucket | n | Brier |\n|---|---|---:|---:|\n")
            for dir_t, bucket, n, b in cohort_rows:
                f.write(f"| {dir_t} | {bucket} | {n} | {b:.4f} |\n")
            f.write("\n")

            f.write("## 3. 5-bin calibration curve\n\n")
            f.write("| Predicted-prob bin | n | mean predicted | actual YES rate |\n|---|---:|---:|---:|\n")
            for lo, hi, avg_p, win_rate, n in cal_rows:
                if n == 0:
                    f.write(f"| [{lo:.1f}, {hi:.1f}] | 0 | — | — |\n")
                else:
                    f.write(f"| [{lo:.1f}, {hi:.1f}] | {n} | {avg_p:.3f} | {win_rate:.3f} |\n")
            f.write("\n")

            if sweep_rows:
                f.write("## 4. Std-inflation sweep\n\n")
                f.write("| inflation | n | Brier |\n|---:|---:|---:|\n")
                for fctr, nn, b in sweep_rows:
                    f.write(f"| {fctr:.2f} | {nn} | {b:.4f} |\n")
                best = min(sweep_rows, key=lambda x: x[2])
                f.write(f"\nBest-Brier inflation: **{best[0]:.2f}** (Brier {best[2]:.4f}).\n\n")
            else:
                f.write("## 4. Std-inflation sweep\n\nSkipped (--no-sweep).\n\n")

            f.write("## 5. Comparison vs live\n\n")
            f.write("Per-trade comparison of the live bot's open-meteo model probability (recorded "
                    "at trade entry) against the replay model probability under "
                    f"`{args.source}`.\n\n")
            f.write("| trade | city | date | dir | strike | settled_yes | bot_p | replay_p | |delta| |\n")
            f.write("|---:|---|---|---|---:|---:|---:|---:|---:|\n")
            for r in headline:
                bp = r["bot_model_p"] or 0.0
                f.write(f"| #{r['trade_id']} | {r['city']} | {r['target_date']} | "
                        f"{r['direction']} | {r['threshold']} | "
                        f"{int(r['settled_yes'])} | {bp:.3f} | {r['replay_model_p']:.3f} | "
                        f"{abs(bp - r['replay_model_p']):.3f} |\n")
            f.write("\n")

        print(f"\n[report] wrote {report_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
