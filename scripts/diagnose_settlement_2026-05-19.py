"""
Diagnose the weather-bot settlement bug. For every weather ticker in
tradingbot.db, fetch the raw Gamma payload and print:
  - whether the underlying market reports closed=true
  - the raw outcomePrices array
  - what the bot's _parse_market_resolution() would return
  - what the bot SHOULD do given the data (so we can spot parser bugs)

Run from project root:
    .venv/bin/python scripts/diagnose_settlement_2026-05-19.py
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend.core.settlement import (  # noqa: E402
    _parse_market_resolution,
    fetch_polymarket_resolution,
)


async def fetch_event_raw(slug: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://gamma-api.polymarket.com/events", params={"slug": slug}
        )
        if r.status_code != 200:
            return None
        events = r.json()
        if not events:
            return None
        return events[0] if isinstance(events, list) else events


async def fetch_market_direct(market_id: str) -> tuple[int, dict | None]:
    """Probe step 2 of the bot's waterfall: GET /markets/{id} directly.
    Returns (status_code, body_or_None)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}"
        )
        if r.status_code != 200:
            return r.status_code, None
        try:
            return 200, r.json()
        except Exception:
            return 200, None


async def main() -> int:
    db_path = ROOT / "tradingbot.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, market_ticker, event_slug, direction, entry_price,
               timestamp, settled
          FROM trades
         WHERE market_type='weather'
      ORDER BY id
        """
    ).fetchall()

    # Group by ticker so we only hit Gamma once per market
    by_ticker: dict[str, list] = {}
    for r in rows:
        by_ticker.setdefault(r["market_ticker"], []).append(r)

    print(f"Probing {len(by_ticker)} unique weather tickers ({len(rows)} trade rows):\n")

    for ticker, trade_rows in by_ticker.items():
        slug = trade_rows[0]["event_slug"]

        # Bot's-eye view: run the live settlement code end-to-end (steps 1+2+3).
        # This tells us the bottom-line answer the production bot would compute.
        bot_resolved, bot_value = await fetch_polymarket_resolution(
            str(ticker), event_slug=slug
        )

        # Raw probe of step 2: direct GET /markets/{ticker}.
        # If step 1 (events?slug=...) returns nothing, the bot falls through here.
        m_status, m_body = await fetch_market_direct(str(ticker))
        if m_body is None:
            step2_summary = f"HTTP {m_status} (no body)"
        else:
            step2_summary = (
                f"closed={m_body.get('closed')}  "
                f"outcomePrices={m_body.get('outcomePrices')}"
            )

        event = await fetch_event_raw(slug)
        if event is None:
            print(f"[{ticker}] event '{slug}': NOT FOUND in Gamma (step 1)")
            print(f"  step 2 /markets/{ticker}:   {step2_summary}")
            print(f"  bot resolves to:    resolved={bot_resolved}  value={bot_value}")
            print(f"    (step 3 in-events search runs inside fetch_polymarket_resolution)")
            for r in trade_rows:
                sett = "settled" if r["settled"] else "OPEN"
                print(f"    trade #{r['id']:>2}  {r['direction']:>3} @ "
                      f"{r['entry_price']:.3f}  opened {(r['timestamp'] or '')[:10]}  [{sett}]")
            print()
            continue

        markets = event.get("markets", [])
        target = next((m for m in markets if str(m.get("id")) == ticker), None)
        if target is None:
            print(f"[{ticker}] event '{slug}': condition id not in {len(markets)}-condition event")
            print(f"  event closed={event.get('closed')}, "
                  f"resolved={event.get('umaResolutionStatus')}")
            print()
            continue

        closed = target.get("closed")
        prices_raw = target.get("outcomePrices")
        prices_parsed = prices_raw
        if isinstance(prices_raw, str):
            try:
                prices_parsed = json.loads(prices_raw)
            except Exception:
                prices_parsed = "<parse error>"

        parser_resolved, parser_value = _parse_market_resolution(target)

        print(f"[{ticker}] {slug}")
        print(f"  question:          {target.get('question', '')[:80]}")
        print(f"  market.closed:     {closed}")
        print(f"  outcomePrices raw: {prices_raw}")
        print(f"  outcomePrices:     {prices_parsed}")
        print(f"  parser says:       resolved={parser_resolved}  value={parser_value}")
        print(f"  step 2 /markets/{ticker}: {step2_summary}")
        print(f"  bot resolves to:   resolved={bot_resolved}  value={bot_value}")
        for r in trade_rows:
            sett = "settled" if r["settled"] else "OPEN"
            print(f"    trade #{r['id']:>2}  {r['direction']:>3} @ {r['entry_price']:.3f}  "
                  f"opened {(r['timestamp'] or '')[:10]}  [{sett}]")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
