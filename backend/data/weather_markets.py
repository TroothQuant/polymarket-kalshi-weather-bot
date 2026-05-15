"""Weather temperature market fetcher from Polymarket."""
import httpx
import re
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

logger = logging.getLogger("trading_bot")

# Map city names/variants found in market titles to our city keys
CITY_ALIASES = {
    "new york": "nyc",
    "nyc": "nyc",
    "new york city": "nyc",
    "chicago": "chicago",
    "miami": "miami",
    "los angeles": "los_angeles",
    "la": "los_angeles",
    "denver": "denver",
}

# Gamma /events filter: `series_slug=<value>` (snake_case) returns all open
# events in a daily series. camelCase `seriesSlug` and `slug_contains` are
# silently ignored and return the default top-100 list — confirmed live
# 2026-05-15 via probe.
CITY_SERIES_MAP = {
    "nyc":         "nyc-daily-weather",
    "chicago":     "chicago-daily-weather",
    "miami":       "miami-daily-weather",
    "los_angeles": "los-angeles-daily-weather",
    "denver":      "denver-daily-weather",
}

# Month name to number
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class WeatherMarket:
    """A weather temperature prediction market."""
    slug: str
    market_id: str
    platform: str
    title: str
    city_key: str
    city_name: str
    target_date: date
    threshold_f: float       # Temperature threshold in Fahrenheit
    metric: str              # "high" or "low"
    direction: str           # "above" or "below"
    yes_price: float         # Price of YES outcome (0-1)
    no_price: float          # Price of NO outcome (0-1)
    volume: float = 0.0
    closed: bool = False


def _parse_weather_market_title(title: str) -> Optional[dict]:
    """
    Parse a weather market title to extract city, threshold, metric, date.

    Handles patterns like:
    - "Will the high temperature in New York exceed 75°F on March 5?"
    - "NYC high temperature above 80°F on March 10, 2026"
    - "Chicago daily high over 60°F on March 3"
    - "Will Miami's low be above 65°F on March 7?"
    - "Temperature in Denver above 70°F on March 5, 2026"
    """
    title_lower = title.lower()

    # Must be temperature-related
    if not any(kw in title_lower for kw in ["temperature", "temp", "°f", "degrees", "high", "low"]):
        return None

    # Extract city
    city_key = None
    city_name = None
    for alias, key in sorted(CITY_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in title_lower:
            city_key = key
            from backend.data.weather import CITY_CONFIG
            city_name = CITY_CONFIG[key]["name"]
            break

    if not city_key:
        return None

    # Extract threshold temperature
    temp_match = re.search(r'(\d+)\s*°?\s*f', title_lower)
    if not temp_match:
        temp_match = re.search(r'(\d+)\s*degrees', title_lower)
    if not temp_match:
        return None
    threshold_f = float(temp_match.group(1))

    # Determine metric (high vs low)
    metric = "high"  # default
    if "low" in title_lower:
        metric = "low"

    # Determine direction
    direction = "above"  # default
    if any(kw in title_lower for kw in ["below", "under", "less than", "drop below"]):
        direction = "below"

    # Extract date
    target_date = _extract_date(title_lower)
    if not target_date:
        return None

    return {
        "city_key": city_key,
        "city_name": city_name,
        "threshold_f": threshold_f,
        "metric": metric,
        "direction": direction,
        "target_date": target_date,
    }


def _extract_date(text: str) -> Optional[date]:
    """Extract a date from market title text."""
    today = date.today()

    # Build month name pattern for precise matching
    month_names = "|".join(MONTH_MAP.keys())

    # Pattern: "March 5, 2026" or "March 5 2026" or "March 5"
    for match in re.finditer(rf'({month_names})\s+(\d{{1,2}})(?:\s*,?\s*(\d{{4}}))?', text):
        month_str = match.group(1)
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year

        month = MONTH_MAP.get(month_str)
        if month and 1 <= day <= 31:
            try:
                return date(year, month, day)
            except ValueError:
                continue

    # Pattern: "3/5/2026" or "03/05"
    match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


async def fetch_polymarket_weather_markets(city_keys: Optional[List[str]] = None) -> List[WeatherMarket]:
    """
    Fetch Polymarket daily-high-temperature events per configured city.
    Iterates CITY_SERIES_MAP and queries one /events?series_slug= per city,
    then flattens every event's markets[] (11 buckets per event) and parses
    each via _parse_polymarket_weather.
    """
    markets: List[WeatherMarket] = []
    target_cities = city_keys if city_keys else list(CITY_SERIES_MAP.keys())
    events_seen = 0

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for city_key in target_cities:
                series_slug = CITY_SERIES_MAP.get(city_key)
                if not series_slug:
                    logger.debug(f"No series_slug mapping for city '{city_key}', skipping")
                    continue
                try:
                    response = await client.get(
                        "https://gamma-api.polymarket.com/events",
                        params={
                            "closed": "false",
                            "limit": 10,
                            "series_slug": series_slug,
                        },
                    )
                    response.raise_for_status()
                    events = response.json()
                    events_seen += len(events)

                    for event in events:
                        event_slug = event.get("slug", "")
                        for market_data in event.get("markets", []):
                            market = _parse_polymarket_weather(market_data, event_slug, city_keys)
                            if market and not any(m.market_id == market.market_id for m in markets):
                                markets.append(market)

                except Exception as e:
                    logger.debug(f"Weather discovery for '{city_key}' ({series_slug}) failed: {e}")

    except Exception as e:
        logger.warning(f"Failed to fetch weather markets: {e}")

    logger.info(
        f"Found {len(markets)} weather temperature markets "
        f"({events_seen} events across {len(target_cities)} cities)"
    )
    return markets


def _parse_polymarket_weather(
    market_data: dict,
    event_slug: str,
    city_keys: Optional[List[str]] = None,
) -> Optional[WeatherMarket]:
    """Parse a Polymarket market dict into a WeatherMarket if it's a temp market."""
    question = market_data.get("question", "") or market_data.get("groupItemTitle", "")
    if not question:
        return None

    parsed = _parse_weather_market_title(question)
    if not parsed:
        return None

    # Filter by requested cities
    if city_keys and parsed["city_key"] not in city_keys:
        return None

    # Only trade markets for dates in the future (or today)
    if parsed["target_date"] < date.today():
        return None

    # Parse prices
    outcome_prices = market_data.get("outcomePrices", [])
    if isinstance(outcome_prices, str):
        import json
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []

    if not outcome_prices or len(outcome_prices) < 2:
        return None

    try:
        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1])
    except (ValueError, IndexError):
        return None

    # Skip resolved markets
    if market_data.get("closed", False):
        return None
    if yes_price > 0.98 or yes_price < 0.02:
        return None

    volume = float(market_data.get("volume", 0) or 0)

    return WeatherMarket(
        slug=event_slug,
        market_id=str(market_data.get("id", "")),
        platform="polymarket",
        title=question,
        city_key=parsed["city_key"],
        city_name=parsed["city_name"],
        target_date=parsed["target_date"],
        threshold_f=parsed["threshold_f"],
        metric=parsed["metric"],
        direction=parsed["direction"],
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
    )
