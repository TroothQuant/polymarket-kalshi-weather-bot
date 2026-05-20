"""
Read-only diagnostic for the Kalshi 0.50 fallback bug (queued 2026-05-20).

Background
----------
Every Kalshi signal the bot has produced in the last 24 hours shows
`market_price = 0.50` (8 tickers across NY/MIA/DEN, including the 3 open
positions #15/#16/#19). The bot's data loader in
`backend/data/kalshi_markets.py` reads:

    yes_price = (m.get("yes_ask") or 0) / 100.0
    no_price  = (m.get("no_ask") or 0) / 100.0
    if yes_price <= 0:
        yes_price = (m.get("last_price") or 50) / 100.0
    if no_price <= 0:
        no_price = 1.0 - yes_price

If `yes_ask` is missing OR zero AND `last_price` is missing OR zero, the
fallback becomes `50 / 100 = 0.50`. The systematic 0.50 across every market
strongly suggests the parse is missing a field that Kalshi actually does
return -- either the field was renamed (e.g. `yes_ask_subcents`, nested
inside a `quote`/`pricing` object) or the bot is calling a different
endpoint than the one that carries the orderbook quotes.

What this script does (READ-ONLY, NO DB WRITES, NO TRADES)
----------------------------------------------------------
1. Fetches a single known-live ticker via `KalshiClient.get_market(ticker)`
   and prints the FULL JSON payload, then highlights the relevant fields
   the parser expects.
2. Fetches the NY weather series via `KalshiClient.get_markets(...)` using
   the same query the bot uses every cycle, then prints the first 3 market
   objects' relevant fields side-by-side.
3. Cross-references field names against what `kalshi_markets.py` reads.
   Anything missing is flagged with a [FIX] tag.

Run from project root:
    .venv/bin/python scripts/inspect_kalshi_markets_2026-05-20.py
    .venv/bin/python scripts/inspect_kalshi_markets_2026-05-20.py --ticker KXHIGHNY-26MAY20-T96
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present  # noqa: E402

# Fields the parser in kalshi_markets.py reads off each market.
PARSER_FIELDS = ["ticker", "yes_ask", "no_ask", "last_price", "volume"]
# Fields also worth showing as candidate alternatives (Kalshi has historically
# had several spellings of bid/ask in different API versions).
ALSO_INSPECT = [
    "yes_bid", "no_bid",
    "yes_subtitle", "no_subtitle",
    "status", "result",
    "open_interest", "liquidity",
    "settlement_value",
    "close_time", "expiration_time", "expected_expiration_time",
    "title", "subtitle",
    "rules_primary", "rules_secondary",
    "previous_price", "previous_yes_bid", "previous_yes_ask",
    "ohlc", "candlestick_data",
    "pricing", "quote", "book",  # nested-object candidates
]

DEFAULT_TICKER = "KXHIGHNY-26MAY20-T96"  # one of the open positions
DEFAULT_SERIES = "KXHIGHNY"


def _summarise_fields(payload: dict, banner: str) -> None:
    print(f"\n{'='*72}\n{banner}\n{'='*72}")
    parser_view = {}
    for f in PARSER_FIELDS:
        v = payload.get(f, "<MISSING>")
        flag = " [FIX]" if v in (None, 0, "<MISSING>") and f in ("yes_ask", "no_ask", "last_price") else ""
        parser_view[f] = f"{v!r}{flag}"
    print("Parser reads:")
    for k, v in parser_view.items():
        print(f"  {k:>18}: {v}")

    print("Also present:")
    other = {}
    for f in ALSO_INSPECT:
        if f in payload:
            other[f] = payload[f]
    if not other:
        print("  (none of the candidate alt fields are at the top level)")
    else:
        for k, v in other.items():
            preview = json.dumps(v) if isinstance(v, (dict, list)) else repr(v)
            if len(preview) > 200:
                preview = preview[:197] + "..."
            print(f"  {k:>18}: {preview}")

    # Anything in the payload that wasn't already shown
    seen = set(PARSER_FIELDS) | set(ALSO_INSPECT)
    leftovers = {k: v for k, v in payload.items() if k not in seen}
    if leftovers:
        print("Other fields in payload (not yet considered):")
        for k, v in leftovers.items():
            preview = json.dumps(v) if isinstance(v, (dict, list)) else repr(v)
            if len(preview) > 120:
                preview = preview[:117] + "..."
            print(f"  {k:>18}: {preview}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ticker",
        default=DEFAULT_TICKER,
        help=f"Specific Kalshi ticker to inspect (default: {DEFAULT_TICKER})",
    )
    parser.add_argument(
        "--series",
        default=DEFAULT_SERIES,
        help=f"Series ticker for list query (default: {DEFAULT_SERIES})",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="Also print the FULL raw JSON for each market (verbose)",
    )
    args = parser.parse_args()

    if not kalshi_credentials_present():
        print("ERROR: Kalshi credentials not configured. Check .env for "
              "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
        return 1

    client = KalshiClient()

    # 1) Single-ticker probe
    print("\n" + "#" * 72)
    print(f"# Probe 1 — single-ticker GET /markets/{args.ticker}")
    print("#" * 72)
    try:
        single = await client.get_market(args.ticker)
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 2

    # Kalshi may wrap the market in {"market": {...}} or return it raw.
    market = single.get("market", single)
    if args.dump_raw:
        print("RAW response (top level):")
        print(json.dumps(single, indent=2, default=str))
    _summarise_fields(market, f"Field summary — ticker {args.ticker}")

    # 2) Series list probe — exact same query the bot uses every cycle
    print("\n" + "#" * 72)
    print(f"# Probe 2 — list GET /markets?series_ticker={args.series}&status=open")
    print("#" * 72)
    try:
        listed = await client.get_markets({
            "series_ticker": args.series,
            "status": "open",
            "limit": 10,
        })
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 3

    markets = listed.get("markets", []) or []
    print(f"Got {len(markets)} markets back.")
    if not markets:
        print("EMPTY -- this is itself a smoking gun; the bot would have nothing to "
              "trade. Try a different series_ticker or status filter.")
        return 0

    if args.dump_raw:
        print("RAW first market in list:")
        print(json.dumps(markets[0], indent=2, default=str))

    for i, m in enumerate(markets[:3]):
        _summarise_fields(m, f"Field summary — list market #{i+1} ({m.get('ticker','?')})")

    # 3) Diagnostic conclusions
    print("\n" + "#" * 72)
    print("# Diagnostic conclusions")
    print("#" * 72)
    first = markets[0]
    yes_ask = first.get("yes_ask")
    no_ask = first.get("no_ask")
    last_price = first.get("last_price")
    if yes_ask in (None, 0) and no_ask in (None, 0) and last_price in (None, 0):
        print("All three of yes_ask / no_ask / last_price are missing or zero on the")
        print("LIST endpoint. The bot's loader will fall back to 0.50 for every market,")
        print("which matches the observed signals table (every market_price = 0.50).")
        print()
        print("Look above at 'Also present' and 'Other fields' for the first market.")
        print("Likely culprits:")
        print("  - yes_ask_subcents / no_ask_subcents (newer Kalshi naming)")
        print("  - nested under 'pricing' or 'quote' or 'book'")
        print("  - moved to a different endpoint (e.g. /markets/{ticker}/orderbook)")
        print()
        print("Compare with Probe 1 (single-ticker GET /markets/{ticker}); the")
        print("single-market endpoint sometimes carries quote data the list does not.")
    elif yes_ask in (None, 0) and no_ask in (None, 0):
        print("Asks are missing/zero on the list endpoint but a last_price exists.")
        print("Two options for the fix:")
        print("  - Trust last_price (already the fallback path).")
        print("  - Skip markets without a real ask (safer; bot doesn't trade on")
        print("    fictional prices).")
    else:
        print("Asks are present on the list endpoint -- the 0.50 issue is somewhere")
        print("else (maybe the per-cycle scan path differs from this script's path).")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
