"""Trade settlement logic for BTC 5-min and weather markets using Polymarket API."""
import httpx
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.models.database import Trade, BotState, Signal

logger = logging.getLogger("trading_bot")


async def fetch_polymarket_resolution(market_id: str, event_slug: Optional[str] = None) -> Tuple[bool, Optional[float]]:
    """
    Fetch actual market resolution from Polymarket API.

    For BTC 5-min markets, uses event slug to find the market.

    Returns: (is_resolved, settlement_value)
        - settlement_value: 1.0 if Up won, 0.0 if Down won
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try event slug first (more reliable for BTC 5-min markets)
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug}
                )
                response.raise_for_status()
                events = response.json()

                if events:
                    event = events[0] if isinstance(events, list) else events
                    markets = event.get("markets", [])
                    # Find the specific condition matching market_id, not markets[0].
                    # markets[0] for a NegRisk weather event is always the lowest-temp
                    # bucket (e.g. "55°F or below"), which resolves to NO early in the
                    # day and would falsely settle every other bucket's trades.
                    target = next(
                        (m for m in markets if str(m.get("id")) == str(market_id)),
                        None,
                    )
                    if target is None:
                        logger.warning(
                            f"Market {market_id} not found in event {event_slug} "
                            f"(event has {len(markets)} conditions); treating as unresolved."
                        )
                        return False, None
                    return _parse_market_resolution(target)

            # Fallback: try market ID directly
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            response = await client.get(url)

            if response.status_code == 404:
                return await _search_market_in_events(market_id)

            response.raise_for_status()
            market = response.json()
            return _parse_market_resolution(market)

    except Exception as e:
        logger.warning(f"Failed to fetch resolution for {event_slug or market_id}: {e}")
        return False, None


async def _search_market_in_events(market_id: str) -> Tuple[bool, Optional[float]]:
    """Search for market in events (both active and closed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for closed in [True, False]:
                params = {
                    "closed": str(closed).lower(),
                    "limit": 200
                }
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params=params
                )
                response.raise_for_status()
                events = response.json()

                for event in events:
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market)

        return False, None

    except Exception as e:
        logger.warning(f"Failed to search for market {market_id}: {e}")
        return False, None


def _parse_market_resolution(market: dict) -> Tuple[bool, Optional[float]]:
    """
    Parse market data to determine if resolved and outcome.

    Handles both Yes/No and Up/Down outcomes.

    Rule: if Polymarket reports closed=True, the market is settled. The first
    outcome won if outcomePrices[0] >= 0.5, otherwise the second won.

    (Prior behavior used strict 0.99/0.01 thresholds and silently treated
    closed markets that resolved at e.g. 0.97/0.03 as "still pending" -- those
    positions then sat in pending forever and consumed the weather cap.
    Audit 2026-05-19 CRITICAL #3.)
    """
    is_closed = market.get("closed", False)

    if not is_closed:
        return False, None

    outcome_prices = market.get("outcomePrices", [])
    if not outcome_prices:
        return False, None

    try:
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        if len(outcome_prices) < 2:
            logger.warning(
                f"Market {market.get('id')} closed but outcomePrices has "
                f"<2 entries: {outcome_prices}"
            )
            return False, None

        first_price = float(outcome_prices[0])

        # Closed + first outcome ahead -> first outcome won (Up/Yes).
        # Closed + second outcome ahead -> second outcome won (Down/No).
        # The strict 0.99 / 0.01 buckets are kept only for the log message
        # detail; the settlement decision uses the >= 0.5 tiebreaker.
        if first_price >= 0.5:
            confidence = "high" if first_price > 0.99 else "soft"
            logger.info(
                f"Market {market.get('id')} resolved: UP/YES won "
                f"(first_price={first_price:.4f}, {confidence})"
            )
            return True, 1.0
        else:
            confidence = "high" if first_price < 0.01 else "soft"
            logger.info(
                f"Market {market.get('id')} resolved: DOWN/NO won "
                f"(first_price={first_price:.4f}, {confidence})"
            )
            return True, 0.0

    except (ValueError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse outcome prices: {e}")
        return False, None


def calculate_pnl(trade: Trade, settlement_value: float) -> float:
    """
    Calculate P&L for a trade using Polymarket share-purchase mechanics
    (migrated 2026-05-19; previously used a fictional CFD model that
    under-counted P&L by a factor of 1/entry_price).

    settlement_value: 1.0 if Up/Yes outcome, 0.0 if Down/No outcome

    Math:
      A `size` of $X buys X/p shares at entry price p, costing $X to enter.
      At settlement the shares pay $1 each if the side won, $0 if it lost.
      Realized PnL = shares * (settlement_value_for_my_side - entry_price)
                   = (size / entry_price) * (my_side_value - entry_price)

      For YES position, my_side_value = settlement_value.
      For NO position,  my_side_value = 1.0 - settlement_value.
    """
    # Map up/down to yes/no logic
    direction = trade.direction
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    if trade.entry_price is None or trade.entry_price <= 0:
        return 0.0

    shares = trade.size / trade.entry_price

    if direction == "yes":
        my_side_value = settlement_value
    else:  # NO position
        my_side_value = 1.0 - settlement_value

    pnl = shares * (my_side_value - trade.entry_price)
    return round(pnl, 2)


async def check_market_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a trade's market has settled.

    Returns: (is_settled, settlement_value, pnl)
    """
    is_resolved, settlement_value = await fetch_polymarket_resolution(
        trade.market_ticker,
        event_slug=trade.event_slug
    )

    if not is_resolved or settlement_value is None:
        return False, None, None

    pnl = calculate_pnl(trade, settlement_value)

    mapped_dir = "UP" if trade.direction in ("up", "yes") else "DOWN"
    outcome = "UP" if settlement_value == 1.0 else "DOWN"
    result = "WIN" if mapped_dir == outcome else "LOSS"

    logger.info(f"Trade {trade.id} settled: {mapped_dir} @ {trade.entry_price:.0%} -> "
                f"{result} P&L: ${pnl:+.2f}")

    return True, settlement_value, pnl


async def check_weather_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a weather trade's market has settled.
    Routes to the correct platform's resolution method.

    Return contract:
      - (False, None, None) — market still open, no settlement yet.
      - (True, settlement_value, pnl) — normal win/loss settlement.
      - (True, None, 0) — VOID: market resolved void/refund. Stake comes
        back, no PnL. Bug fix 2026-05-21: previously this branch was
        swallowed because the condition required `settlement_value is not
        None`, leaving voided trades stuck in pending forever (see trade #9
        which had to be manually voided via SQL).
    """
    platform = getattr(trade, 'platform', 'polymarket') or 'polymarket'

    if platform == "kalshi":
        is_resolved, settlement_value = await _fetch_kalshi_resolution(trade.market_ticker)
    else:
        is_resolved, settlement_value = await fetch_polymarket_resolution(
            trade.market_ticker,
            event_slug=trade.event_slug,
        )

    if is_resolved:
        if settlement_value is None:
            # Void / refund path. pnl=0 — stake refund is handled by
            # update_bot_state_with_settlements via payout = size + pnl = size.
            return True, None, 0
        pnl = calculate_pnl(trade, settlement_value)
        return True, settlement_value, pnl

    return False, None, None


async def _fetch_kalshi_resolution(ticker: str) -> Tuple[bool, Optional[float]]:
    """Fetch resolution status for a Kalshi market.

    Audit 2026-05-19 HIGH #14: the Kalshi API returns more `result` values
    than just yes/no. The full set we recognize:
      - "yes"     -> single-event YES win        -> 1.0
      - "yes_win" -> multi-leg series YES win    -> 1.0
      - "no"      -> single-event NO win         -> 0.0
      - "no_win"  -> multi-leg series NO win     -> 0.0
      - "all_no"  -> multi-leg "none of the above" series -> 0.0
      - "void"    -> market voided / cancelled   -> stake is returned (callers
                                                    must treat this differently
                                                    from a regular settlement)

    Previously only `yes` and `no` were recognized -- everything else fell
    through and the trade sat in pending forever, exactly the failure mode
    we fixed on the Polymarket side (CRITICAL #3).
    """
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

        if not kalshi_credentials_present():
            return False, None

        client = KalshiClient()
        data = await client.get_market(ticker)
        market = data.get("market", data)

        status = market.get("status", "")
        result = (market.get("result", "") or "").strip().lower()

        if status in ("finalized", "determined") and result:
            if result in ("yes", "yes_win"):
                return True, 1.0
            elif result in ("no", "no_win", "all_no"):
                return True, 0.0
            elif result == "void":
                # Market voided -- stake is returned, no PnL change.
                # Signal void to callers via (True, None): is_resolved=True
                # but no settlement_value. The settle_pending_trades caller
                # handles this as a void/refund settlement.
                # Bug fix 2026-05-21: previously returned (False, None) which
                # left voided trades stuck in pending forever (see trade #9
                # which had to be manually voided via SQL).
                logger.warning(
                    f"Kalshi market {ticker} resolved 'void' -- "
                    f"closing trade with stake refund, no PnL change."
                )
                return True, None
            else:
                # Finalized status with an unrecognized result -- this is the
                # bug surface we want LOUDLY visible, not silently swallowed.
                logger.warning(
                    f"Kalshi market {ticker} status={status!r} result={result!r}: "
                    f"unrecognized result code, treating as still pending."
                )
                return False, None

        return False, None

    except Exception as e:
        logger.warning(f"Failed to fetch Kalshi resolution for {ticker}: {e}")
        return False, None


async def settle_pending_trades(db: Session) -> List[Trade]:
    """
    Process all pending trades for settlement.
    Uses REAL market outcomes from Polymarket API.
    """
    try:
        pending = db.query(Trade).filter(Trade.settled == False).all()
    except Exception as e:
        logger.error(f"Failed to query pending trades: {e}")
        return []

    if not pending:
        logger.info("No pending trades to settle")
        return []

    logger.info(f"Checking {len(pending)} pending trades for settlement...")
    settled_trades = []

    for trade in pending:
        try:
            # Route settlement by market type
            market_type = getattr(trade, 'market_type', 'btc') or 'btc'
            if market_type == "weather":
                is_settled, settlement_value, pnl = await check_weather_settlement(trade)
            else:
                is_settled, settlement_value, pnl = await check_market_settlement(trade)

            # is_settled with settlement_value=None signals a void/refund
            # (added 2026-05-21). settlement_value=0.0 or 1.0 signals normal
            # win/loss resolution.
            if is_settled:
                trade.settled = True
                trade.settlement_time = datetime.utcnow()

                if settlement_value is None:
                    # Void / refund — stake comes back, no PnL.
                    trade.settlement_value = None
                    trade.pnl = 0
                    trade.result = "void"
                else:
                    trade.settlement_value = settlement_value
                    trade.pnl = pnl
                    if pnl is not None and pnl > 0:
                        trade.result = "win"
                    elif pnl is not None and pnl < 0:
                        trade.result = "loss"
                    else:
                        trade.result = "push"

                settled_trades.append(trade)

                # Update linked Signal with actual outcome for calibration.
                # Voids (settlement_value=None) are NOT a calibration data
                # point — the market never resolved, so we don't know if the
                # model was right. Skip the outcome_correct update for voids.
                if trade.signal_id and settlement_value is not None:
                    linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                    if linked_signal:
                        actual_outcome = "up" if settlement_value == 1.0 else "down"
                        linked_signal.actual_outcome = actual_outcome
                        linked_signal.outcome_correct = (linked_signal.direction == actual_outcome)
                        linked_signal.settlement_value = settlement_value
                        linked_signal.settled_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Failed to settle trade {trade.id}: {e}")
            continue

    if settled_trades:
        try:
            db.commit()
            logger.info(f"Settled {len(settled_trades)} trades")
        except Exception as e:
            logger.error(f"Failed to commit settlements: {e}")
            db.rollback()
            return []
    else:
        logger.info("No trades ready for settlement (markets still open)")

    return settled_trades


async def _fetch_kalshi_mark(market_ticker: str) -> Optional[Tuple[float, float]]:
    """Return (yes_price, no_price) for an open Kalshi market or None.

    Mirrors the entry-time parser in kalshi_markets.py (HIGH-priority
    parity fix 2026-05-20): reads yes_ask_dollars / no_ask_dollars with
    bid + last_price fallbacks, returns None if the market is finalized
    or has no usable quote. Until this function existed the stop-loss
    job had no live mark for Kalshi positions and silently skipped them.
    """
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present
    except Exception:
        return None
    if not kalshi_credentials_present():
        return None
    try:
        client = KalshiClient()
        data = await client.get_market(market_ticker)
        market = data.get("market", data) or {}
    except Exception as e:
        logger.debug(f"Kalshi mark fetch failed for {market_ticker}: {e}")
        return None

    # Don't return a mark for finalized markets — they belong to the
    # settlement path, not the stop-loss path.
    status = (market.get("status") or "").lower()
    if status in ("finalized", "determined", "settled"):
        return None

    def _pd(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    yes_p = _pd(market.get("yes_ask_dollars"))
    no_p = _pd(market.get("no_ask_dollars"))
    if yes_p is None or yes_p <= 0:
        yes_p = _pd(market.get("last_price_dollars")) or _pd(market.get("yes_bid_dollars"))
    if no_p is None or no_p <= 0:
        no_bid = _pd(market.get("no_bid_dollars"))
        if no_bid:
            no_p = no_bid
    if yes_p is None or no_p is None or yes_p <= 0 or no_p <= 0:
        return None
    return (yes_p, no_p)


async def fetch_current_weather_mark(market_ticker: str, event_slug: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """
    Fetch the current YES and NO prices for a still-open weather market.
    Returns (yes_price, no_price) or None if unavailable / market closed.

    Used by the stop-loss job to mark-to-market open weather positions.
    Dispatches by ticker shape: KX-prefixed tickers go to Kalshi, anything
    else uses the Polymarket Gamma path.
    """
    # Kalshi parity fix (2026-05-20): route KX tickers to their own mark
    # fetcher so the stop-loss job protects Kalshi positions too.
    if isinstance(market_ticker, str) and market_ticker.startswith("KX"):
        return await _fetch_kalshi_mark(market_ticker)

    def _extract(target: dict) -> Optional[Tuple[float, float]]:
        if target.get("closed", False):
            return None
        prices = target.get("outcomePrices") or []
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                return None
        if len(prices) < 2:
            return None
        try:
            return float(prices[0]), float(prices[1])
        except (ValueError, TypeError):
            return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug},
                )
                response.raise_for_status()
                events = response.json()
                if events:
                    event = events[0] if isinstance(events, list) else events
                    target = next(
                        (m for m in event.get("markets", []) if str(m.get("id")) == str(market_ticker)),
                        None,
                    )
                    if target is not None:
                        return _extract(target)

            # Polymarket renames weather event slugs; fall through to the
            # ticker-direct lookup the settlement path already uses.
            response = await client.get(
                f"https://gamma-api.polymarket.com/markets/{market_ticker}"
            )
            if response.status_code == 200:
                return _extract(response.json())
    except Exception as e:
        logger.warning(f"Failed to fetch mark for {event_slug or market_ticker}: {e}")
    return None


def compute_stop_loss_threshold(entry_price: float, size: float, fraction: float) -> float:
    """
    Return the unrealized-loss threshold (positive dollars) at which a position
    should be stopped out, under the share-purchase model (migrated 2026-05-19).

    Max-possible-loss is the full stake `size` (the position goes to $0).
    fraction=0.50 → trigger when half the stake has evaporated.
    """
    return float(fraction) * float(size)


def mark_to_market_loss(trade: Trade, yes_price: float, no_price: float) -> float:
    """
    Return the current unrealized loss (positive number if the position is down)
    for an open weather trade using current mid marks.

    Share-purchase math (migrated 2026-05-19): shares = size / entry_price,
    unrealized P&L = shares × (current_mark − entry_price). Loss is −unrealized.
    """
    direction = trade.direction
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    if trade.entry_price is None or trade.entry_price <= 0:
        return 0.0

    shares = trade.size / trade.entry_price
    if direction == "yes":
        unrealized = shares * (yes_price - trade.entry_price)
    else:
        unrealized = shares * (no_price - trade.entry_price)
    return -unrealized  # positive when at a loss, negative when at a gain


async def close_weather_trades_at_stop_loss(
    db: Session,
    fraction: float = 0.50,
) -> List[Trade]:
    """
    Iterate all open weather trades. For each, fetch current marks and
    short-circuit settle any whose mark-to-market loss is at or beyond
    fraction * max_possible_loss. The stopped-out trades carry:
        result          = "stop_loss"
        settlement_value = None   (no underlying market resolution yet)
        pnl             = current unrealized loss (negative)
        settlement_time = now
    """
    try:
        pending = (
            db.query(Trade)
            .filter(Trade.settled == False, Trade.market_type == "weather")
            .all()
        )
    except Exception as e:
        logger.error(f"stop-loss: failed to query weather trades: {e}")
        return []

    if not pending:
        return []

    stopped: List[Trade] = []
    for trade in pending:
        try:
            marks = await fetch_current_weather_mark(
                trade.market_ticker, event_slug=trade.event_slug
            )
            if marks is None:
                continue
            yes_price, no_price = marks
            loss = mark_to_market_loss(trade, yes_price, no_price)
            threshold = compute_stop_loss_threshold(trade.entry_price, trade.size, fraction)
            if loss < threshold:
                continue  # within tolerance, hold

            trade.settled = True
            trade.settlement_value = None  # mark as not-naturally-resolved
            trade.pnl = round(-loss, 2)
            trade.settlement_time = datetime.utcnow()
            trade.result = "stop_loss"

            logger.info(
                f"stop-loss: trade {trade.id} ({trade.event_slug}) "
                f"{trade.direction.upper()} @ {trade.entry_price:.3f} "
                f"-> mark yes={yes_price:.3f} no={no_price:.3f} "
                f"loss=${loss:.2f} (threshold ${threshold:.2f}); CLOSED"
            )
            stopped.append(trade)
        except Exception as e:
            logger.error(f"stop-loss: failed on trade {trade.id}: {e}")
            continue

    if stopped:
        try:
            db.commit()
            logger.info(f"stop-loss: closed {len(stopped)} weather positions early")
        except Exception as e:
            logger.error(f"stop-loss: commit failed: {e}")
            db.rollback()
            return []

    return stopped


async def update_bot_state_with_settlements(db: Session, settled_trades: List[Trade]) -> None:
    """
    Update bot state with realized payouts from settled trades.

    Cash-flow model — branched by market_type (audit 2026-05-19 HIGH #8):

    * Weather trades use the share-purchase model with stake deducted at
      trade-entry (see scheduler.weather_scan_and_trade_job). At settlement
      we add back the FULL payout: `size + pnl`. Examples:
        - win:       payout = shares * 1.0   (= size + pnl)
        - loss:      payout = shares * 0.0   (= 0,  pnl = -size)
        - stop_loss: payout = shares * mark  (= size + pnl, partial recovery)

    * BTC trades do NOT deduct stake at entry (legacy CFD-shaped accounting
      in scheduler.scan_and_trade_job). Crediting `size + pnl` here would
      double-count: the stake would land in bankroll having never come out.
      For BTC we credit just `pnl`.

    BTC_ENABLED is False in current config so this branch is dormant in
    practice, but historical BTC rows or a future re-enable would otherwise
    silently inflate bankroll by sum-of-stakes on the first settlement pass.
    """
    if not settled_trades:
        return

    try:
        state = db.query(BotState).first()
        if not state:
            logger.warning("Bot state not found")
            return

        for trade in settled_trades:
            if trade.pnl is None:
                continue
            # Weather (and Kalshi weather) use the stake-deducted-on-entry
            # share-purchase model; everything else (legacy BTC) does not.
            market_type = getattr(trade, "market_type", None) or "weather"
            if market_type == "weather":
                payout = float(trade.size) + float(trade.pnl)
            else:
                payout = float(trade.pnl)
            state.total_pnl += trade.pnl
            state.bankroll += payout
            if trade.result == "win":
                state.winning_trades += 1

        db.commit()
        logger.info(f"Updated bot state: Bankroll ${state.bankroll:.2f}, P&L ${state.total_pnl:+.2f}")
    except Exception as e:
        logger.error(f"Failed to update bot state: {e}")
        db.rollback()
