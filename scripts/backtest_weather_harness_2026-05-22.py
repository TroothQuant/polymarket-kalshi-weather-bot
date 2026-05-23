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


def settled_to_yes(settlement_value, direction_traded):
    """
    settled_to_yes -> 1.0 if the market resolved YES, 0.0 if NO, None if void.
    settlement_value is the value paid for the SIDE TRADED, not the YES side.
    To get the YES outcome we flip if we traded NO.
    """
    if settlement_value is None:
        return None
    if direction_traded == "yes":
        return float(settlement_value)
    elif direction_traded == "no":
        return 1.0 - float(settlement_value)
    return None


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
    args = ap.parse_args()

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

    def run_replay(std_inflation: float):
        results = []
        for r in replay_rows:
            city_cfg = CITY_CONFIG[r["city"]]
            lat, lon = city_cfg["lat"], city_cfg["lon"]
            era5_mean = fetch_era5_max_for_date(lat, lon, r["target_date"])
            era5_std = fetch_climatology_std(lat, lon, r["target_date"])
            if era5_mean is None or era5_std is None:
                continue
            if r["direction"] == "above":
                model_p = model_probability_high_above(
                    era5_mean, era5_std, r["threshold"], std_inflation=std_inflation
                )
            else:
                model_p = model_probability_high_below(
                    era5_mean, era5_std, r["threshold"], std_inflation=std_inflation
                )
            results.append({**r, "era5_mean": era5_mean, "era5_std": era5_std,
                            "replay_model_p": model_p, "inflation": std_inflation})
        return results

    print(f"\n=== HEADLINE REPLAY (std inflation = {args.inflate}) ===")
    headline = run_replay(args.inflate)
    print(f"  {'#':>3} {'city':<12} {'date':<10} {'strike':<7} {'dir':<6} "
          f"{'era5_mean':>9} {'era5_std':>8} {'replay_p':>9} {'settled_yes':>11} "
          f"{'bot_p':>7} {'mkt_p':>7} {'pnl':>8}")
    for r in headline:
        print(f"  #{r['trade_id']:>2} {r['city']:<12} {r['target_date']!s:<10} "
              f"{r['threshold']:<7} {r['direction']:<6} "
              f"{r['era5_mean']:>9.1f} {r['era5_std']:>8.2f} "
              f"{r['replay_model_p']:>9.3f} {r['settled_yes']:>11.0f} "
              f"{(r['bot_model_p'] or 0):>7.3f} {(r['bot_market_p'] or 0):>7.3f} "
              f"{(r['pnl'] or 0):>+8.2f}")

    preds = [r["replay_model_p"] for r in headline]
    outs = [r["settled_yes"] for r in headline]
    print(f"\n  Brier score (replay model vs YES outcome): {brier(preds, outs):.4f}")
    print(f"  (lower = better; 0.25 = always-50%; 0 = perfect)")

    print("\n  Calibration bins (replay_model_p decile vs actual YES rate):")
    for lo, hi, avg_p, win_rate, n in calibration_bins(preds, outs, n_bins=5):
        if n == 0:
            print(f"    [{lo:.1f}-{hi:.1f}]  n=0")
        else:
            print(f"    [{lo:.1f}-{hi:.1f}]  n={n:>2}  avg_predicted={avg_p:.3f}  actual_yes_rate={win_rate:.3f}")

    if not args.no_sweep:
        print("\n=== INFLATION SWEEP ===")
        print(f"  {'inflation':>10} {'brier':>8}  (lower better)")
        for f in [round(x * 0.25 + 1.0, 2) for x in range(7)]:
            replay = run_replay(f)
            preds = [r["replay_model_p"] for r in replay]
            outs = [r["settled_yes"] for r in replay]
            print(f"  {f:>10.2f} {brier(preds, outs):>8.4f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
