#!/usr/bin/env python3
"""Next-book PAPER harness (GATE 4) — standalone, paper-only, sandboxed.

Forward-measures the executable edge of the daily high-temp EXPANSION cities
(Austin, Dallas; London/Paris config-gated; Buenos Aires excluded) that we could
NOT backtest because Polymarket archives no order books. Each run:
  SETTLE — fill yesterday's open paper bets from the settlement station's actual
           high (IEM ASOS), compute realized paper P&L + per-trade edge.
  SCAN   — for each enabled city IN its pre-peak window, pull the bot's open-meteo
           GFS ensemble -> P(bucket), compare to the EXECUTABLE ask, and (if edge
           >= min_edge) paper-enter; selection ranks by edge and stops at the live
           caps (max_new_per_day, max_alloc_usd) — same risk posture as the live book.

ISOLATION: writes ONLY to its own SQLite + ledger CSV (config). NEVER touches the
live weather service, its tradingbot.db, or its config. Read-only on all markets.

Usage: nextbook_paper_harness.py [--config PATH] [--dry]
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys

import httpx
import numpy as np

H = {"User-Agent": "(trading-bot, contact@example.com)"}


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path):
    with open(os.path.expanduser(path)) as f:
        c = json.load(f)
    c["state_db"] = os.path.expanduser(c["state_db"])
    c["ledger_csv"] = os.path.expanduser(c["ledger_csv"])
    return c


def db_connect(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY, city TEXT, target_date TEXT, entered_at_utc TEXT,
        prepeak_lead_h REAL, bucket_label TEXT, bucket_lo REAL, bucket_hi REAL,
        model_p REAL, ask REAL, bid REAL, size_usd REAL, shares REAL,
        status TEXT, settle_high REAL, winning_bucket TEXT, won INTEGER,
        realized_pnl REAL, realized_edge REAL, settled_at TEXT,
        UNIQUE(city, target_date))""")
    con.commit()
    return con


# ---------- market + forecast (read-only) ----------
def ensemble_members(lat, lon, unit):
    p = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
         "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
         "start_date": dt.date.today().isoformat(), "end_date": dt.date.today().isoformat(),
         "models": "gfs_seamless"}
    d = httpx.get("https://ensemble-api.open-meteo.com/v1/ensemble", params=p, headers=H, timeout=30).json()["daily"]
    return [d[k][0] for k in d if "temperature_2m_max" in k and d[k][0] is not None]


def gamma_event(city_slug):
    bulk = {}
    for tag in ("temperature", "weather"):
        try:
            for e in httpx.get("https://gamma-api.polymarket.com/events",
                               params={"closed": "false", "limit": 300, "tag_slug": tag},
                               headers=H, timeout=25).json():
                bulk[e.get("slug")] = e
        except Exception:
            pass
    months = ["january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december"]
    t = dt.date.today()
    slug = f"highest-temperature-in-{city_slug}-on-{months[t.month-1]}-{t.day}-{t.year}"
    return bulk.get(slug)


def parse_bucket(q, unit):
    m = re.search(r"(\d+)\s*-\s*(\d+)", q)
    if m:
        return (float(m.group(1)), float(m.group(2)) + 1)
    m = re.search(r"(\d+).{0,12}(?:or higher|or above|or more)", q)
    if m:
        return (float(m.group(1)), 200.0)
    m = re.search(r"(\d+).{0,12}(?:or below|or lower|or less)", q)
    if m:
        return (-200.0, float(m.group(1)) + (1 if unit == "F" else 0.5))
    m = re.search(r"(\d+)\s*°?[CF]", q)
    if m:
        x = float(m.group(1))
        return (x - 0.5, x + 0.5) if unit == "C" else (x, x + 1)
    return None


def iem_station_high(iem, target_date):
    st, net = iem
    u = f"https://mesonet.agron.iastate.edu/api/1/daily.json?station={st}&network={net}"
    try:
        for r in httpx.get(u, headers=H, timeout=40).json().get("data", []):
            if r.get("date") == target_date and r.get("max_tmpf") is not None:
                return float(r["max_tmpf"])
    except Exception as e:
        log(f"  IEM fail {st}: {e}")
    return None


# ---------- settle ----------
def settle(con, cfg, dry):
    today = dt.date.today().isoformat()
    rows = con.execute("SELECT id, city, target_date, bucket_label, bucket_lo, bucket_hi, "
                       "ask, size_usd, shares FROM positions WHERE status='open' AND target_date < ?",
                       (today,)).fetchall()
    n = 0
    for pid, city, tdate, blabel, lo, hi, ask, size, shares in rows:
        ccfg = cfg["cities"][city]
        unit = ccfg["unit"]
        high = iem_station_high(ccfg["iem"], tdate)
        if high is None:
            continue  # station data not yet posted; retry next run
        high_u = high if unit == "F" else (high - 32) * 5 / 9
        won = 1 if (lo <= high_u < hi) else 0
        pnl = shares * (1.0 - ask) if won else -size
        redge = (1.0 if won else 0.0) - ask
        if not dry:
            con.execute("UPDATE positions SET status='settled', settle_high=?, won=?, "
                        "realized_pnl=?, realized_edge=?, settled_at=? WHERE id=?",
                        (round(high_u, 1), won, round(pnl, 2), round(redge, 4),
                         dt.datetime.now(dt.timezone.utc).isoformat(), pid))
        log(f"  SETTLE {city} {tdate}: high={high_u:.1f}{unit} bucket[{blabel}] "
            f"{'WON' if won else 'lost'} pnl=${pnl:+.2f} edge={redge:+.2f}")
        n += 1
    con.commit()
    return n


# ---------- scan ----------
def scan(con, cfg, dry):
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.today().isoformat()
    uhour = now.hour
    todays = con.execute("SELECT COUNT(*) FROM positions WHERE entered_at_utc LIKE ?", (today + "%",)).fetchone()[0]
    alloc = con.execute("SELECT COALESCE(SUM(size_usd),0) FROM positions WHERE status='open'").fetchone()[0]

    candidates = []
    for city, c in cfg["cities"].items():
        if not c.get("enabled") or c.get("excluded"):
            continue
        if con.execute("SELECT 1 FROM positions WHERE city=? AND target_date=?", (city, today)).fetchone():
            log(f"  {city}: already has a position today — skip")
            continue
        lo_u, hi_u = c["prepeak_utc"]
        if not (lo_u <= uhour < hi_u):
            log(f"  {city}: outside pre-peak window (UTC {uhour} not in [{lo_u},{hi_u})) — skip")
            continue
        try:
            mem = np.array(ensemble_members(c["lat"], c["lon"], c["unit"]))
            ev = gamma_event(c["slug"])
        except Exception as e:
            log(f"  {city}: fetch fail {e}")
            continue
        if ev is None or len(mem) < 10:
            log(f"  {city}: no live event / ensemble (members={len(mem)})")
            continue
        live_mid = sum(1 for m in ev.get("markets", [])
                       if m.get("bestBid") and m.get("bestAsk") and 0.10 <= m["bestAsk"] <= 0.90)
        if live_mid < 2:
            log(f"  {city}: market resolved-out (live-middle={live_mid}) — pre-peak filter skip")
            continue
        best = None
        for m in ev.get("markets", []):
            rng = parse_bucket(m.get("groupItemTitle") or m.get("question") or "", c["unit"])
            ask = m.get("bestAsk")
            if rng is None or not ask:
                continue
            pm = float(np.mean((mem >= rng[0]) & (mem < rng[1])))
            edge = pm - ask
            if best is None or edge > best["edge"]:
                best = {"city": city, "edge": edge, "label": m.get("groupItemTitle") or "",
                        "lo": rng[0], "hi": rng[1], "p": pm, "ask": ask, "bid": m.get("bestBid"),
                        "lead_h": hi_u - uhour}
        if best and best["edge"] >= cfg["min_edge"]:
            candidates.append(best)
        elif best:
            log(f"  {city}: best edge {best['edge']:+.2f} < min {cfg['min_edge']} — no entry")

    # selection: rank by edge, honor caps (same risk posture as live book)
    candidates.sort(key=lambda x: -x["edge"])
    size = cfg["trade_size_usd"]
    entered = 0
    for cand in candidates:
        if todays + entered >= cfg["max_new_per_day"]:
            log(f"  CAP: max_new_per_day={cfg['max_new_per_day']} reached — {cand['city']} deferred")
            break
        if alloc + size > cfg["max_alloc_usd"]:
            log(f"  CAP: max_alloc_usd={cfg['max_alloc_usd']} reached — {cand['city']} deferred")
            break
        shares = size / cand["ask"]
        if not dry:
            con.execute("INSERT OR IGNORE INTO positions (city,target_date,entered_at_utc,prepeak_lead_h,"
                        "bucket_label,bucket_lo,bucket_hi,model_p,ask,bid,size_usd,shares,status) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                        (cand["city"], today, now.isoformat(), cand["lead_h"], cand["label"],
                         cand["lo"], cand["hi"], round(cand["p"], 3), cand["ask"], cand["bid"],
                         size, round(shares, 2)))
        alloc += size
        entered += 1
        log(f"  ENTER {cand['city']} [{cand['label']}] model_p={cand['p']:.2f} ask={cand['ask']} "
            f"edge={cand['edge']:+.2f} lead={cand['lead_h']}h size=${size}")
    con.commit()
    return entered


def dump_ledger(con, path):
    cols = ["id", "city", "target_date", "entered_at_utc", "prepeak_lead_h", "bucket_label",
            "model_p", "ask", "bid", "size_usd", "shares", "status", "settle_high",
            "winning_bucket", "won", "realized_pnl", "realized_edge", "settled_at"]
    rows = con.execute(f"SELECT {','.join(cols)} FROM positions ORDER BY id").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return len(rows)


def summary(con):
    s = con.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), COALESCE(AVG(realized_edge),0), "
                    "COALESCE(SUM(won),0) FROM positions WHERE status='settled'").fetchone()
    n, pnl, avg_edge, wins = s
    op = con.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    log(f"SUMMARY: settled={n} (won={wins}) paper_pnl=${pnl:+.2f} avg_realized_edge={avg_edge:+.3f} | open={op}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "nextbook_cities.json"))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    log(f"=== next-book paper harness {dt.datetime.now(dt.timezone.utc).isoformat()} {'(DRY)' if args.dry else ''} ===")
    con = db_connect(cfg["state_db"])
    ns = settle(con, cfg, args.dry)
    ne = scan(con, cfg, args.dry)
    rows = dump_ledger(con, cfg["ledger_csv"])
    log(f"settled {ns}, entered {ne}, ledger rows {rows} -> {cfg['ledger_csv']}")
    summary(con)
    con.close()


if __name__ == "__main__":
    main()
