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
    threshold_f: float       # Temperature threshold in Fahrenheit (legacy field, kept for Polymarket)
    metric: str              # "high" or "low"
    direction: str           # "above" or "below" (legacy field, kept for Polymarket)
    yes_price: float         # Price the bot would PAY to buy YES (the ask)
    no_price: float          # Price the bot would PAY to buy NO  (the ask)
    volume: float = 0.0
    closed: bool = False
    # Kalshi bucket-semantics fields (2026-05-20 — diagnostic in
    # scripts/inspect_kalshi_bucket_semantics_2026-05-20.py confirmed
    # Kalshi's KXHIGH series uses narrow point-buckets, not cumulative
    # "above X" markets — e.g. KXHIGHNY-26MAY20-B89.5 asks "will NYC high
    # land in 89-90°F", not "will it exceed 89.5°F"). Populating these
    # fields tells weather_signals.py to use the right probability calc:
    #   strike_type='between' -> probability_high_between(floor, cap)
    #   strike_type='greater' -> probability_high_above(floor)
    #   strike_type='less'    -> probability_high_below(cap)
    # When strike_type is None the legacy `direction`+`threshold_f` path
    # is used (Polymarket markets behave that way).
    strike_type: Optional[str] = None       # "greater" | "less" | "between" | None
    floor_strike: Optional[float] = None    # lower bound (greater + between)
    cap_strike: Optional[float] = None      # upper bound (less + between)
    # Audit 2026-05-19 HIGH #15: separate fields for fill-side asks
    # (yes_price / no_price above) and implied midpoint probability used for
    # edge math. On Polymarket the two are the same — outcomePrices is the
    # implied probability. On Kalshi yes_ask + no_ask typically sum > 1, so
    # using yes_ask alone biases the edge calc; we set implied_yes_prob to
    # (yes_ask + (1 - no_ask)) / 2 instead. Default = yes_price for backward
    # compatibility with existing Polymarket call sites.
    implied_yes_prob: float = -1.0  # sentinel; resolved on access via .implied_or_yes()

    def implied_or_yes(self) -> float:
        """Return the implied YES probability for edge math.

        Falls back to yes_price when implied_yes_prob wasn't set explicitly,
        which is the correct Polymarket behavior (outcomePrices already IS
        the implied probability).
        """
        return self.implied_yes_prob if self.implied_yes_prob >= 0 else self.yes_price


def _parse_weather_market_title(title: str) -> Optional[dict]:
    """
    Parse a weather market title into (city, threshold, metric, direction, date).

    Polymarket daily-temp events host 11 mutually-exclusive bucket conditions
    per event: nine internal ranges plus one low-edge ("X°F or below") and one
    high-edge ("X°F or higher"). The threshold-style signal model used by this
    bot only fits the two edge buckets — internal ranges are rejected here.

    Live question shapes observed 2026-05-15:
      - Range  (SKIP): "...be between 66-67°F on May 15?"
      - Low    (KEEP): "...be 55°F or below on May 15?"   → direction="below"
      - High   (KEEP): "...be 74°F or higher on May 15?"  → direction="above"
    Variants tolerated defensively: "or lower"/"or less" (low),
      "or above"/"more than"/"above " (high).
    """
    title_lower = title.lower()

    # 1. Reject internal range buckets — these don't fit a single-threshold model.
    if re.search(r'\d+\s*-\s*\d+\s*°?\s*f', title_lower) or "between" in title_lower:
        return None

    # 2. Must be temperature-related.
    if not any(kw in title_lower for kw in ["temperature", "temp", "°f", "degrees", "high", "low"]):
        return None

    # 3. Extract city from the question text.
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

    # 4. Classify edge type — direction comes from the bucket phrasing, not
    #    from substring-matching "low" (which also matches "below").
    LOW_EDGE = re.compile(r'°?\s*f?\s*or\s+(below|lower|less)\b', re.IGNORECASE)
    HIGH_EDGE = re.compile(
        r'(°?\s*f?\s*or\s+(higher|above|more|greater)\b'
        r'|\babove\s+\d+\s*°?\s*f'
        r'|\bmore\s+than\s+\d+\s*°?\s*f)',
        re.IGNORECASE,
    )
    if LOW_EDGE.search(title_lower):
        direction = "below"
    elif HIGH_EDGE.search(title_lower):
        direction = "above"
    else:
        logger.warning(f"weather parser: unrecognised bucket pattern, skipping: {title!r}")
        return None

    # 5. Threshold temperature — first integer followed by an optional °F.
    temp_match = re.search(r'(\d+)\s*°?\s*f', title_lower)
    if not temp_match:
        temp_match = re.search(r'(\d+)\s*degrees', title_lower)
    if not temp_match:
        return None
    threshold_f = float(temp_match.group(1))

    # 6. Metric: derive from "highest"/"lowest" phrase in title to avoid the
    #    "below" → contains-"low" substring bug. Daily-high events always
    #    contain "highest temperature".
    if "lowest temperature" in title_lower:
        metric = "low"
    else:
        metric = "high"

    # 7. Date.
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
