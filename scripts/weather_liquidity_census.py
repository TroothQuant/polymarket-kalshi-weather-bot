#!/usr/bin/env python3
"""Weather liquidity census — read-only reconnaissance (BUILD 1, 2026-07-19).

Discovers EVERY active Polymarket daily-temperature market (all cities, not
just our trading 5; °F US + °C international), and for each tradeable EDGE
bucket (the "X or below" / "X or higher" longshot buckets the rest-price
ladder will target) snapshots the CLOB order book for BOTH tokens (YES + NO).

Per token it records best bid/ask + size-at-best, mid, spread, and summed
depth within 4.5c of mid on each side (the 4.5c band = the LP-rewards zone).

STRICTLY READ-ONLY: hits gamma + CLOB public read endpoints only. Writes ONE
csv (~/.local/state/trooth/weather_liquidity_census.csv) and nothing else —
never the bot DB, config, or any live state. Runs fine on the NYC paper box
(book/gamma reads work there; only order PLACEMENT is geoblocked).

Scope note: internal (non-edge, ~50/50 favorite) buckets are intentionally
excluded — the threshold model never trades them and censusing all 11 buckets
per event would ~10x the book-call load. Edge buckets are where fills live for
our strategy. Row count ≈ 2 x (edge markets discovered).

Timer: trooth-liquidity-census.timer (hourly at :30 UTC, offset from the
bot's scan cadence).
"""
import csv
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
REWARDS_BAND = 0.045  # 4.5c LP-rewards zone, each side of mid
BOOK_SLEEP = 0.15     # polite pacing between CLOB book calls

STATE_DIR = os.path.expanduser("~/.local/state/trooth")
CSV_PATH = os.path.join(STATE_DIR, "weather_liquidity_census.csv")
CSV_COLS = [
    "ts_utc", "event_slug", "city", "market_ticker", "bucket_label", "side",
    "best_bid", "bid_sz", "best_ask", "ask_sz", "mid", "spread",
    "depth_bid_45c", "depth_ask_45c",
]

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

# Daily-temp event slug: highest|lowest-temperature-in-<city>-on-<month>-<day>-<year>
EVENT_SLUG = re.compile(
    r"^(highest|lowest)-temperature-in-(.+)-on-([a-z]+)-(\d{1,2})-(\d{4})$")

# Edge-bucket phrasing (unit-agnostic — matches both °C and °F titles).
RANGE_RE = re.compile(r"\d+\s*-\s*\d+\s*°?\s*[cf]|between", re.IGNORECASE)
LOW_EDGE = re.compile(r"or\s+(below|lower|less)\b", re.IGNORECASE)
HIGH_EDGE = re.compile(r"or\s+(higher|above|more|greater)\b", re.IGNORECASE)
THRESH_RE = re.compile(r"(\d+)\s*°?\s*([cf])", re.IGNORECASE)


def discover_events(client):
    """Paginate the gamma 'weather' tag; return future-dated daily-temp events."""
    events, offset, today = [], 0, datetime.now(timezone.utc).date()
    while offset <= 2000:
        r = client.get(f"{GAMMA}/events", params={
            "closed": "false", "limit": 100, "offset": offset,
            "tag_slug": "weather"})
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        events.extend(page)
        offset += 100
    out = []
    for e in events:
        m = EVENT_SLUG.match(e.get("slug", "") or "")
        if not m:
            continue
        metric, city, mon, day, yr = m.groups()
        if mon not in MONTHS:
            continue
        try:
            td = date(int(yr), MONTHS[mon], int(day))
        except ValueError:
            continue
        if td < today:
            continue
        out.append((e, city, td))
    return out


def classify_edge(question):
    """Return (bucket_label, is_edge) for a bucket question, or (None, False)."""
    q = question or ""
    if RANGE_RE.search(q):
        return None, False  # internal range bucket
    tm = THRESH_RE.search(q)
    if not tm:
        return None, False
    thr, unit = tm.group(1), tm.group(2).upper()
    if LOW_EDGE.search(q):
        return f"<={thr}{unit}", True
    if HIGH_EDGE.search(q):
        return f">={thr}{unit}", True
    return None, False  # internal exact-degree bucket


def get_tokens(client, condition_id):
    """CLOB market -> [(outcome, token_id), ...] (YES/NO)."""
    r = client.get(f"{CLOB}/markets/{condition_id}")
    if r.status_code != 200:
        return []
    toks = r.json().get("tokens", []) or []
    return [(t.get("outcome"), t.get("token_id")) for t in toks
            if t.get("token_id")]


def snapshot_book(client, token_id):
    """Return the per-token book metrics dict, or None on failure."""
    for attempt in range(2):
        try:
            r = client.get(f"{CLOB}/book", params={"token_id": token_id})
            if r.status_code != 200:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                return None
            b = r.json()
            break
        except Exception:
            if attempt == 0:
                time.sleep(0.3)
                continue
            return None
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    bid_sz = round(sum(s for p, s in bids if p == best_bid), 2) if best_bid is not None else 0.0
    ask_sz = round(sum(s for p, s in asks if p == best_ask), 2) if best_ask is not None else 0.0
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2, 4)
        spread = round(best_ask - best_bid, 4)
    else:
        mid, spread = None, None
    ref = mid if mid is not None else (best_bid if best_bid is not None else best_ask)
    if ref is None:
        depth_bid = depth_ask = 0.0
    else:
        depth_bid = round(sum(s for p, s in bids if p >= ref - REWARDS_BAND), 2)
        depth_ask = round(sum(s for p, s in asks if p <= ref + REWARDS_BAND), 2)
    return {
        "best_bid": best_bid, "bid_sz": bid_sz, "best_ask": best_ask,
        "ask_sz": ask_sz, "mid": mid, "spread": spread,
        "depth_bid_45c": depth_bid, "depth_ask_45c": depth_ask,
    }


def main():
    t0 = time.time()
    os.makedirs(STATE_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = httpx.Client(timeout=20.0)

    events = discover_events(client)
    n_high = sum(1 for e, _, _ in events if e["slug"].startswith("highest"))
    n_low = len(events) - n_high
    print(f"[census] discovered {len(events)} future daily-temp events "
          f"(highs={n_high} lows={n_low})", flush=True)

    rows, markets, book_ok, book_fail = [], 0, 0, 0
    for event, city, td in events:
        slug = event.get("slug", "")
        for md in event.get("markets", []):
            label, is_edge = classify_edge(
                md.get("question") or md.get("groupItemTitle") or "")
            if not is_edge:
                continue
            cond = md.get("conditionId")
            if not cond:
                continue
            ticker = md.get("slug") or cond
            tokens = get_tokens(client, cond)
            time.sleep(BOOK_SLEEP)
            if not tokens:
                continue
            markets += 1
            for outcome, tid in tokens:
                snap = snapshot_book(client, tid)
                time.sleep(BOOK_SLEEP)
                if snap is None:
                    book_fail += 1
                    continue
                book_ok += 1
                rows.append({
                    "ts_utc": ts, "event_slug": slug, "city": city,
                    "market_ticker": ticker, "bucket_label": label,
                    "side": outcome, **snap,
                })

    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow(row)

    print(f"[census] markets={markets} rows={len(rows)} "
          f"book_ok={book_ok} book_fail={book_fail} "
          f"elapsed={time.time()-t0:.1f}s -> {CSV_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
