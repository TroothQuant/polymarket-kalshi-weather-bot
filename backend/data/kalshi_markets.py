"""Kalshi weather temperature market fetcher."""
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
from backend.data.weather_markets import WeatherMarket

logger = logging.getLogger("trading_bot")


def _parse_dollars(value: Any) -> float:
    """Parse a Kalshi `*_dollars` quote field.

    Kalshi 2026-Q2 rename: all quote fields are now decimal-string dollars
    (e.g. '0.0100' = 1¢ = 1% probability), and the legacy integer-cent
    fields (yes_ask, no_ask, last_price, volume) no longer appear in the
    payload. Confirmed by inspect_kalshi_markets_2026-05-20.py against a
    real live market: both single-ticker and series-list endpoints carry
    yes_ask_dollars / no_ask_dollars / last_price_dollars as decimal
    strings, and the obsolete integer fields are gone.

    Note: the payload includes `response_price_units: 'usd_cent'` as
    metadata which is MISLEADING -- the `*_dollars` values are NOT cents,
    they are dollars (0.01 dollars = 1 cent).

    Returns 0.0 on missing/empty/unparseable values so the caller can fall
    through to the bid-side or no-trade path.
    """
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

# Kalshi series tickers for high-temperature markets by city
CITY_SERIES: Dict[str, str] = {
    "nyc": "KXHIGHNY",
    "chicago": "KXHIGHCHI",
    "miami": "KXHIGHMIA",
    "los_angeles": "KXHIGHLAX",
    "denver": "KXHIGHDEN",
}

CITY_NAMES: Dict[str, str] = {
    "nyc": "New York",
    "chicago": "Chicago",
    "miami": "Miami",
    "los_angeles": "Los Angeles",
    "denver": "Denver",
}

# Month abbreviation mapping for ticker parsing
MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_kalshi_ticker(ticker: str, city_key: str) -> Optional[dict]:
    """
    Parse a Kalshi bracket ticker into market parameters.

    Format: KXHIGHNY-26MAR01-B45.5
      - 26MAR01 = 2026-03-01
      - B45.5 = bracket boundary at 45.5°F (above)
      - T45.5 would be "at or below" (top boundary)
    """
    # Match: SERIES-YYMONDD-B/Tnn.n
    match = re.match(
        r'^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-([BT])([\d.]+)$',
        ticker,
    )
    if not match:
        return None

    yy = int(match.group(1))
    mon_str = match.group(2)
    dd = int(match.group(3))
    boundary_type = match.group(4)
    threshold = float(match.group(5))

    month = MONTH_ABBR.get(mon_str)
    if not month:
        return None

    year = 2000 + yy
    try:
        target_date = date(year, month, dd)
    except ValueError:
        return None

    # B = bottom boundary → "above" threshold; T = top boundary → "below" threshold
    direction = "above" if boundary_type == "B" else "below"

    return {
        "target_date": target_date,
        "threshold_f": threshold,
        "metric": "high",
        "direction": direction,
    }


async def fetch_kalshi_weather_markets(
    city_keys: Optional[List[str]] = None,
) -> List[WeatherMarket]:
    """
    Fetch open weather temperature markets from Kalshi.

    Queries the KXHIGH{city} series for each configured city,
    handles cursor-based pagination, and returns WeatherMarket objects.
    """
    if not kalshi_credentials_present():
        return []

    client = KalshiClient()
    markets: List[WeatherMarket] = []
    today = date.today()

    cities = city_keys or list(CITY_SERIES.keys())

    for city_key in cities:
        series = CITY_SERIES.get(city_key)
        if not series:
            continue

        city_name = CITY_NAMES.get(city_key, city_key)
        cursor = None

        try:
            while True:
                params = {
                    "series_ticker": series,
                    "status": "open",
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor

                data = await client.get_markets(params)
                raw_markets = data.get("markets", [])

                for m in raw_markets:
                    ticker = m.get("ticker", "")
                    parsed = _parse_kalshi_ticker(ticker, city_key)
                    if not parsed:
                        continue

                    if parsed["target_date"] < today:
                        continue

                    # Kalshi 2026-Q2 field rename: read the new *_dollars
                    # decimal-string fields (see _parse_dollars docstring +
                    # diagnostic at scripts/inspect_kalshi_markets_2026-05-20.py).
                    # The legacy integer-cent fields (yes_ask, no_ask,
                    # last_price) silently returned None under the old code,
                    # collapsing every market to the 0.50 fallback and
                    # producing fictional 45% edges across every Kalshi
                    # signal in the 2026-05-19 / 2026-05-20 ledger.
                    yes_price = _parse_dollars(m.get("yes_ask_dollars"))
                    no_price = _parse_dollars(m.get("no_ask_dollars"))

                    # Fallback ladder if the ask side is empty:
                    #   1. last_price_dollars (most recent trade)
                    #   2. opposing bid mirror (yes from 1 - no_bid)
                    #   3. SKIP -- don't trade against a fictional price.
                    if yes_price <= 0:
                        yes_price = _parse_dollars(m.get("last_price_dollars"))
                    if no_price <= 0:
                        # Try bid-mirror before giving up.
                        no_bid = _parse_dollars(m.get("no_bid_dollars"))
                        if no_bid > 0:
                            no_price = no_bid
                    if yes_price <= 0:
                        yes_bid = _parse_dollars(m.get("yes_bid_dollars"))
                        if yes_bid > 0:
                            yes_price = yes_bid

                    # Audit 2026-05-20 follow-up: skip markets with no real
                    # ask data. The old fallback to 0.50 created fictional
                    # entries (see ledger trades #15/#16/#19). It is safer
                    # for the bot to look at fewer markets than to size a
                    # position against a price that isn't real.
                    if yes_price <= 0 or no_price <= 0:
                        logger.debug(
                            f"Skipping Kalshi {ticker}: no usable quote "
                            f"(yes={yes_price}, no={no_price})"
                        )
                        continue

                    # Skip fully resolved or illiquid
                    if yes_price > 0.98 or yes_price < 0.02:
                        continue

                    # Audit 2026-05-19 HIGH #15: yes_ask + no_ask sum > 1 on
                    # Kalshi (the bid/ask spread). Using yes_ask alone as
                    # the implied probability biases the edge calc by
                    # (yes_ask - true_mid). Compute the implied midpoint:
                    #   implied_yes_prob = (yes_ask + (1 - no_ask)) / 2
                    # Clamp to [0.01, 0.99] for numerical sanity.
                    implied_yes_prob = (yes_price + (1.0 - no_price)) / 2.0
                    implied_yes_prob = max(0.01, min(0.99, implied_yes_prob))

                    # Kalshi 2026-Q2: volume is now `volume_fp` (float-string).
                    # Keep a legacy fallback on `volume` so a future rollback
                    # doesn't zero out the field.
                    volume = _parse_dollars(
                        m.get("volume_fp") or m.get("volume_24h_fp") or m.get("volume")
                    )

                    markets.append(WeatherMarket(
                        slug=ticker,
                        market_id=ticker,
                        platform="kalshi",
                        title=m.get("title", ticker),
                        city_key=city_key,
                        city_name=city_name,
                        target_date=parsed["target_date"],
                        threshold_f=parsed["threshold_f"],
                        metric=parsed["metric"],
                        direction=parsed["direction"],
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=volume,
                        implied_yes_prob=implied_yes_prob,
                    ))

                # Handle pagination
                cursor = data.get("cursor")
                if not cursor or not raw_markets:
                    break

        except Exception as e:
            logger.warning(f"Failed to fetch Kalshi markets for {city_key} ({series}): {e}")

    logger.info(f"Found {len(markets)} Kalshi weather markets")
    return markets
