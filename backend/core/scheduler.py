"""Background scheduler for BTC 5-min autonomous trading."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal, PnlSnapshot
from backend.core.signals import scan_for_signals

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200

# Audit 2026-05-19 HIGH #10: monotonic sequence number on every event so the
# WS endpoint can slice on `event["seq"] > last_seen_seq` instead of the
# old len-delta heuristic, which re-pushed the entire 200-event buffer on
# every poll once the deque saturated.
_event_seq = 0


def log_event(event_type: str, message: str, data: dict = None):
    """Log an event for terminal display.

    Timestamps are tz-aware UTC ("...+00:00") so the dashboard's
    `new Date(...).toLocaleTimeString()` converts correctly to the
    viewer's local time. Naive `utcnow().isoformat()` was being parsed
    as LOCAL time by the browser, displaying UTC hours.
    """
    global _event_seq
    _event_seq += 1
    event = {
        "seq": _event_seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "message": message,
        "data": data or {}
    }
    event_log.append(event)

    while len(event_log) > MAX_LOG_SIZE:
        event_log.pop(0)

    log_func = {
        "error": logger.error,
        "warning": logger.warning,
        "success": logger.info,
        "info": logger.info,
        "data": logger.debug,
        "trade": logger.info
    }.get(event_type, logger.info)

    log_func(f"[{event_type.upper()}] {message}")


def get_recent_events(limit: int = 50) -> List[dict]:
    """Get recent events for terminal display."""
    return event_log[-limit:]


async def scan_and_trade_job():
    """
    Background job: Scan BTC 5-min markets, generate signals, execute trades.
    Runs every minute.
    """
    log_event("info", "Scanning BTC 5-min markets...")

    try:
        signals = await scan_for_signals()
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Found {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        if not actionable:
            log_event("info", "No actionable BTC signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping trades")
                return

            MAX_TRADES_PER_SCAN = 2
            MIN_TRADE_SIZE = 10
            MAX_TRADE_FRACTION = 0.03  # 3% max per trade
            MAX_TOTAL_PENDING = settings.MAX_TOTAL_PENDING_TRADES

            # --- Daily loss circuit breaker ---
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.settlement_time >= today_start
            ).scalar()

            if daily_pnl <= -settings.DAILY_LOSS_LIMIT:
                log_event("warning", f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${settings.DAILY_LOSS_LIMIT:.0f}). Stopping trades.")
                return

            total_pending = db.query(Trade).filter(Trade.settled == False).count()
            if total_pending >= MAX_TOTAL_PENDING:
                log_event("info", f"Max pending trades reached ({total_pending}/{MAX_TOTAL_PENDING})")
                return

            trades_executed = 0
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                # Check if we already have a trade for this market window
                existing = db.query(Trade).filter(
                    Trade.event_slug == signal.market.slug,
                    Trade.settled == False
                ).first()

                if existing:
                    continue

                trade_size = min(signal.suggested_size, state.bankroll * MAX_TRADE_FRACTION)
                trade_size = max(trade_size, MIN_TRADE_SIZE)

                if state.bankroll < MIN_TRADE_SIZE:
                    log_event("warning", f"Bankroll too low: ${state.bankroll:.2f}")
                    break

                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break

                # Map up/down to yes/no for storage
                entry_price = signal.market.up_price if signal.direction == "up" else signal.market.down_price

                trade = Trade(
                    market_ticker=signal.market.market_id,
                    platform="polymarket",
                    event_slug=signal.market.slug,
                    direction=signal.direction,
                    entry_price=entry_price,
                    size=trade_size,
                    model_probability=signal.model_probability,
                    market_price_at_entry=signal.market_probability,
                    edge_at_entry=signal.edge
                )

                db.add(trade)
                db.flush()  # get trade.id

                # Link trade to the most recent matching Signal and mark it executed
                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                state.total_trades += 1
                trades_executed += 1

                log_event("trade",
                    f"BTC {signal.direction.upper()} ${trade_size:.0f} @ {entry_price:.0%} | {signal.market.slug}",
                    {
                        "slug": signal.market.slug,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": entry_price,
                        "btc_price": signal.btc_price,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} BTC trade(s)")
            else:
                log_event("info", "No new trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Scan error: {str(e)}")
        logger.exception("Error in scan_and_trade_job")


async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every 5 minutes when WEATHER_ENABLED.
    """
    log_event("info", "Scanning weather temperature markets...")

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Weather: {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        if not actionable:
            log_event("info", "No actionable weather signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping weather trades")
                return

            MAX_TRADES_PER_SCAN = 3
            MIN_TRADE_SIZE = 10
            # Max total exposure to weather markets (configurable via .env or
            # WEATHER_MAX_ALLOCATION_USD in config.py). Bumped from $500 →
            # $1500 on 2026-05-19 after Kalshi rollout expanded the universe
            # from 5 → 65 markets/cycle and the bot started seeing 60+
            # actionable signals per scan.
            MAX_WEATHER_ALLOCATION = settings.WEATHER_MAX_ALLOCATION_USD

            # Check weather allocation limit
            weather_pending = db.query(func.coalesce(func.sum(Trade.size), 0.0)).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).scalar()

            if weather_pending >= MAX_WEATHER_ALLOCATION:
                log_event("info", f"Weather allocation limit reached: ${weather_pending:.0f}/${MAX_WEATHER_ALLOCATION:.0f}")
                return

            # Per-day new-position cap (added 2026-05-28). Counts weather
            # positions OPENED today (UTC midnight → now) regardless of whether
            # they've since settled — a position opened AND stopped the same day
            # still consumed daily exposure, so settled-today rows must still
            # count or the cap wouldn't stop a 2026-05-20-style run. This schema
            # has no separate "exit" rows (settlement mutates the open row), so
            # counting Trade rows by entry timestamp inherently counts opens only.
            # The existing per-scan cap (MAX_TRADES_PER_SCAN) was never breached
            # on 2026-05-20 — the damage was 10 trades across ~9 scans, which only
            # a per-day cap can catch. Counts BOTH platforms together.
            max_per_day = settings.WEATHER_MAX_NEW_POSITIONS_PER_DAY
            utc_day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            positions_today = db.query(func.count(Trade.id)).filter(
                Trade.market_type == "weather",
                Trade.timestamp >= utc_day_start,
            ).scalar() or 0

            if positions_today >= max_per_day:
                log_event(
                    "info",
                    f"[blocked] WEATHER_MAX_NEW_POSITIONS_PER_DAY={max_per_day} reached "
                    f"({positions_today} opened today); skipping all new weather opens this scan."
                )
                return

            trades_executed = 0
            weather_alloc_running = 0.0
            # Kalshi kill-switch budget-starvation fix (2026-05-21): when
            # KALSHI_TRADING_ENABLED=False, filter Kalshi signals OUT of the
            # actionable list BEFORE applying MAX_TRADES_PER_SCAN. Otherwise
            # the top-of-list Kalshi signals (which dominate by edge due to
            # long-tail entry inflation — edges of 0.85-0.93 vs Polymarket's
            # typical 0.30-0.65) burn through all 3 slots via `continue` and
            # the bot never falls through to actually-executable Polymarket
            # signals lower in the sort. Bug observed 2026-05-19→2026-05-21:
            # bot generated 60+ actionable signals/cycle for 48 hours but
            # opened zero new Polymarket positions (last new trade: #14 on
            # 2026-05-19 17:55) while Chicago 2309247 at +65.5% edge sat
            # un-traded.
            kalshi_trading_enabled = getattr(settings, "KALSHI_TRADING_ENABLED", False)
            if not kalshi_trading_enabled:
                pre_filter_count = len(actionable)
                actionable = [
                    s for s in actionable
                    if getattr(s.market, "platform", "polymarket") != "kalshi"
                ]
                skipped = pre_filter_count - len(actionable)
                if skipped > 0:
                    log_event(
                        "info",
                        f"Kalshi trading disabled — filtered {skipped} Kalshi "
                        f"signal(s) from actionable list; {len(actionable)} "
                        f"Polymarket signals remain for trade consideration."
                    )

            # Per-market dedup (2026-05-19 fix): one trade per market_ticker for
            # its lifetime, regardless of UTC-day boundaries. Each weather ticker
            # is unique per (city, resolution date), so a trade for ticker X is by
            # definition a trade for that single resolution. The old `today_start`
            # filter let a market be re-entered across day boundaries, producing
            # the 2265993/2274465/2274497 dupes observed on 2026-05-15..17 and the
            # 2274497 opposite-direction re-entry. Also blocks re-buy of an already-
            # settled market on later days (which makes no sense for daily resolution).
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                # Kalshi trade kill-switch (2026-05-20): defense-in-depth check.
                # The pre-loop filter above should have removed all Kalshi
                # signals when KALSHI_TRADING_ENABLED=False, but we leave this
                # guard in case the filter path is ever bypassed.
                platform = getattr(signal.market, "platform", "polymarket")
                if platform == "kalshi" and not kalshi_trading_enabled:
                    log_event(
                        "info",
                        f"Kalshi trading disabled — skipping {signal.market.market_id} "
                        f"(edge {signal.edge:.0%}); set KALSHI_TRADING_ENABLED=true "
                        f"in .env to re-enable after parity verification."
                    )
                    continue

                existing = db.query(Trade).filter(
                    Trade.market_ticker == signal.market.market_id,
                ).first()

                if existing:
                    log_event(
                        "info",
                        f"Already traded {signal.market.market_id} "
                        f"(trade #{existing.id}, settled={existing.settled}); skipping",
                    )
                    continue

                trade_size = min(signal.suggested_size, settings.WEATHER_MAX_TRADE_SIZE)
                trade_size = max(trade_size, MIN_TRADE_SIZE)

                if state.bankroll < MIN_TRADE_SIZE:
                    log_event("warning", f"Bankroll too low: ${state.bankroll:.2f}")
                    break

                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break

                # Per-day cap (added 2026-05-28): positions_today was counted at
                # scan start; trades_executed is what THIS scan has opened so far.
                # Breaks mid-scan once the daily budget is exhausted. Belt-and-
                # suspenders with the pre-loop early return above (same pattern as
                # the per-scan cap, which uses both a slice and this break).
                if positions_today + trades_executed >= max_per_day:
                    log_event(
                        "info",
                        f"[blocked] WEATHER_MAX_NEW_POSITIONS_PER_DAY={max_per_day} reached "
                        f"({positions_today} earlier today + {trades_executed} this scan); "
                        f"deferring remaining candidate(s) to tomorrow."
                    )
                    break

                # Allocation cap break (CRITICAL #5, 2026-06-05): mirror of the
                # per-day-cap pattern above. weather_pending is the scan-start
                # snapshot of size-on-the-book; weather_alloc_running is what
                # THIS scan has opened so far. Breaks mid-scan once the dollar
                # budget would be exceeded by adding this candidate's stake.
                # Belt-and-suspenders with the pre-loop early return at ~line 249.
                if weather_pending + weather_alloc_running + trade_size > MAX_WEATHER_ALLOCATION:
                    log_event(
                        "info",
                        f"[blocked] WEATHER_MAX_ALLOCATION_USD=${MAX_WEATHER_ALLOCATION:.0f} would be exceeded "
                        f"(pending ${weather_pending:.0f} + ${weather_alloc_running:.0f} this scan + ${trade_size:.0f}); "
                        f"deferring remaining candidate(s)."
                    )
                    break

                entry_price = signal.market.yes_price if signal.direction == "yes" else signal.market.no_price

                # Realistic fills (2026-07-20, paper server only): fetch the REAL
                # CLOB book and fill ONLY against actual asks at/below this cap
                # (partial at real ask sizes, price = swept VWAP, 5% taker fee
                # folded into the cost basis). No fillable ask -> NO trade row;
                # the signal is tagged unfilled_no_liquidity. Flag OFF = the
                # historical fantasy-fill at the gamma outcomePrice (unchanged).
                if (settings.WEATHER_PAPER_REALISTIC_FILLS
                        and getattr(signal.market, "platform", "polymarket") == "polymarket"):
                    from backend.core.execution_realism import (
                        resolve_token_id, fetch_book, realistic_fill)
                    token = resolve_token_id(
                        getattr(signal.market, "condition_id", ""), signal.direction)
                    book = fetch_book(token) if token else {"asks": []}
                    fill = realistic_fill(book, cap_price=entry_price, size_usd=trade_size)
                    if fill is None:
                        log_event("info",
                            f"[unfilled_no_liquidity] WX {signal.market.city_name}: "
                            f"{signal.direction.upper()} no ask <= {entry_price:.3f} "
                            f"(req ${trade_size:.0f})",
                            {"slug": signal.market.slug, "direction": signal.direction,
                             "reason": "unfilled_no_liquidity", "cap": entry_price,
                             "city": signal.market.city_name})
                        unf = db.query(Signal).filter(
                            Signal.market_ticker == signal.market.market_id,
                            Signal.market_type == "weather",
                            Signal.executed == False,
                        ).order_by(Signal.timestamp.desc()).first()
                        if unf and "unfilled_no_liquidity" not in (unf.reasoning or ""):
                            unf.reasoning = (unf.reasoning or "") + " [unfilled_no_liquidity]"
                        continue
                    entry_price = fill["effective_entry_price"]  # fee-inclusive cost basis
                    trade_size = fill["cost"]                    # actual $ deployed (partial)

                # Use the signal's platform so Kalshi trades save as "kalshi"
                # (was hardcoded to "polymarket" before the Kalshi rollout 2026-05-19).
                trade = Trade(
                    market_ticker=signal.market.market_id,
                    platform=getattr(signal.market, "platform", "polymarket"),
                    event_slug=signal.market.slug,
                    market_type="weather",
                    direction=signal.direction,
                    entry_price=entry_price,
                    size=trade_size,
                    model_probability=signal.model_probability,
                    market_price_at_entry=signal.market_probability,
                    edge_at_entry=signal.edge,
                )

                db.add(trade)
                db.flush()

                # Link to signal record
                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.market_type == "weather",
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                # Deduct the stake from bankroll at entry (migrated 2026-05-19
                # to share-purchase cash flow). The full stake `size` is locked
                # up in shares now; bankroll holds only cash. Settlement adds
                # back `size + pnl` (= payout).
                state.bankroll -= trade_size

                state.total_trades += 1
                trades_executed += 1
                weather_alloc_running += trade_size

                log_event("trade",
                    f"WX {signal.market.city_name}: {signal.direction.upper()} "
                    f"${trade_size:.0f} @ {entry_price:.0%} | "
                    f"{signal.market.metric} {signal.market.direction} {signal.market.threshold_f:.0f}F",
                    {
                        "slug": signal.market.slug,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": entry_price,
                        "city": signal.market.city_name,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} weather trade(s)")
            else:
                log_event("info", "No new weather trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Weather scan error: {str(e)}")
        logger.exception("Error in weather_scan_and_trade_job")


async def settlement_job():
    """
    Background job: Check and settle pending trades.
    Runs every 2 minutes (BTC 5-min markets resolve fast).
    """
    log_event("info", "Checking BTC trade settlements...")

    try:
        from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements

        db = SessionLocal()
        try:
            pending_count = db.query(Trade).filter(Trade.settled == False).count()

            if pending_count == 0:
                log_event("data", "No pending trades to settle")
                return

            log_event("data", f"Processing {pending_count} pending trades")

            settled = await settle_pending_trades(db)

            if settled:
                await update_bot_state_with_settlements(db, settled)

                wins = sum(1 for t in settled if t.result == "win")
                losses = sum(1 for t in settled if t.result == "loss")
                total_pnl = sum(t.pnl for t in settled if t.pnl is not None)

                log_event("success", f"Settled {len(settled)} trades: {wins}W/{losses}L, P&L: ${total_pnl:.2f}", {
                    "settled_count": len(settled),
                    "wins": wins,
                    "losses": losses,
                    "pnl": total_pnl
                })

                for trade in settled:
                    result_prefix = "+" if trade.pnl and trade.pnl > 0 else ""
                    log_event("data", f"  {trade.event_slug}: {trade.result.upper()} {result_prefix}${trade.pnl:.2f}")
            else:
                log_event("info", "No trades ready for settlement")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Settlement error: {str(e)}")
        logger.exception("Error in settlement_job")


async def weather_stop_loss_job():
    """
    Background job (added 2026-05-19): check open weather positions for
    mark-to-market loss exceeding settings.WEATHER_STOP_LOSS_FRACTION of the
    position's max-possible-loss. Closes positions early at the current mark
    rather than riding to settlement.
    """
    if not settings.WEATHER_STOP_LOSS_ENABLED:
        return

    try:
        from backend.core.settlement import (
            close_weather_trades_at_stop_loss,
            update_bot_state_with_settlements,
        )

        db = SessionLocal()
        try:
            stopped = await close_weather_trades_at_stop_loss(
                db, fraction=settings.WEATHER_STOP_LOSS_FRACTION
            )
            if stopped:
                await update_bot_state_with_settlements(db, stopped)
                total_pnl = sum(t.pnl or 0.0 for t in stopped)
                log_event(
                    "trade",
                    f"Stop-loss closed {len(stopped)} weather position(s), realized ${total_pnl:+.2f}",
                    {"count": len(stopped), "pnl": total_pnl},
                )
        finally:
            db.close()
    except Exception as e:
        log_event("error", f"Stop-loss error: {e}")
        logger.exception("Error in weather_stop_loss_job")


_last_snapshot_prune_ts = 0.0


async def heartbeat_job():
    """Periodic heartbeat. Runs every minute. Also writes a PnL snapshot."""
    global _last_snapshot_prune_ts
    db = None
    try:
        db = SessionLocal()
        state = db.query(BotState).first()
        pending = db.query(Trade).filter(
            Trade.settled == False,
            Trade.market_type == "weather",
        ).count()

        if state is None:
            log_event("warning", "Heartbeat: Bot state not initialized")
            return

        # Audit 2026-05-19 HIGH #28: write state.last_run on every heartbeat
        # so the dashboard's "Last scan" label reflects the bot's actual
        # liveness. Previously `state.last_run` was only set when a scan
        # found actionable signals -- the dashboard would show "5h ago" while
        # the bot was scanning normally every cycle.
        state.last_run = datetime.utcnow()

        # Audit 2026-05-19 HIGH #12: prune PnlSnapshot rows older than 30
        # days at most once per hour. Heartbeat writes ~1440 rows/day; left
        # unbounded the dashboard's `since=24h` queries table-scan more and
        # more over time. Cheap modulo gate keeps overhead negligible.
        import time as _time
        now = _time.time()
        if now - _last_snapshot_prune_ts > 3600:
            try:
                cutoff = datetime.utcnow() - timedelta(days=30)
                deleted = db.query(PnlSnapshot).filter(
                    PnlSnapshot.timestamp < cutoff
                ).delete(synchronize_session=False)
                db.commit()
                _last_snapshot_prune_ts = now
                if deleted:
                    log_event("info", f"Pruned {deleted} PnlSnapshot rows older than 30d")
            except Exception:
                logger.exception("PnlSnapshot retention prune failed")

        log_event("data", f"Heartbeat: {pending} pending trades, bankroll: ${state.bankroll:.2f}", {
            "pending_trades": pending,
            "bankroll": state.bankroll,
            "is_running": state.is_running
        })

        # Write a snapshot row for dashboard charting. Cheap; swallow errors.
        try:
            from sqlalchemy import func as _f
            exposure = db.query(_f.coalesce(_f.sum(Trade.size), 0.0)).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).scalar() or 0.0
            realized = db.query(_f.coalesce(_f.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.market_type == "weather",
            ).scalar() or 0.0
            settled = db.query(Trade).filter(
                Trade.settled == True,
                Trade.market_type == "weather",
            ).count()

            db.add(PnlSnapshot(
                bankroll=float(state.bankroll),
                exposure=float(exposure),
                realized_pnl=float(realized),
                pending_count=int(pending),
                settled_count=int(settled),
                is_running=bool(state.is_running),
            ))
            db.commit()
        except Exception as snap_err:
            log_event("warning", f"PnL snapshot insert failed: {snap_err}")
    except Exception as e:
        log_event("warning", f"Heartbeat failed: {str(e)}")
    finally:
        if db:
            db.close()


def _model_bias_refresh_job():
    """Sync wrapper for the nightly model-bias refresh (runs in the executor)."""
    try:
        from backend.core.model_bias import refresh_all
        refresh_all()
    except Exception as e:
        logger.warning(f"[model_bias] nightly refresh job failed: {e}")


def start_scheduler():
    """Start the background scheduler for BTC 5-min trading."""
    global scheduler

    if scheduler is not None and scheduler.running:
        log_event("warning", "Scheduler already running")
        return

    scheduler = AsyncIOScheduler()

    scan_seconds = settings.SCAN_INTERVAL_SECONDS
    settle_seconds = settings.SETTLEMENT_INTERVAL_SECONDS

    # Scan BTC markets every minute (gated by BTC_ENABLED)
    if settings.BTC_ENABLED:
        scheduler.add_job(
            scan_and_trade_job,
            IntervalTrigger(seconds=scan_seconds),
            id="market_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    # Check settlements every 2 minutes
    scheduler.add_job(
        settlement_job,
        IntervalTrigger(seconds=settle_seconds),
        id="settlement_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Heartbeat every minute
    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(minutes=1),
        id="heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Weather trading jobs (gated by WEATHER_ENABLED)
    if settings.WEATHER_ENABLED:
        weather_scan_seconds = settings.WEATHER_SCAN_INTERVAL_SECONDS
        weather_settle_seconds = settings.WEATHER_SETTLEMENT_INTERVAL_SECONDS

        scheduler.add_job(
            weather_scan_and_trade_job,
            IntervalTrigger(seconds=weather_scan_seconds),
            id="weather_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

        # Stop-loss check (added 2026-05-19): mark-to-market open weather
        # positions and close any down ≥ WEATHER_STOP_LOSS_FRACTION of max loss.
        if settings.WEATHER_STOP_LOSS_ENABLED:
            scheduler.add_job(
                weather_stop_loss_job,
                IntervalTrigger(seconds=settings.WEATHER_STOP_LOSS_INTERVAL_SECONDS),
                id="weather_stop_loss",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )

        # Model-upgrade v1 (2026-07-01): nightly per-(city,model) bias refresh +
        # signal-grading backfill. Sync job → runs in the executor thread, off the
        # scan hot path. Also fired ONCE at startup so the table + grading populate
        # immediately. Fully self-isolating (swallows its own errors).
        scheduler.add_job(
            _model_bias_refresh_job,
            CronTrigger(hour=8, minute=0),   # 08:00 UTC — ERA5 has the prior days by then
            id="model_bias_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            _model_bias_refresh_job,
            "date", run_date=datetime.utcnow(),
            id="model_bias_initial",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    scheduler.start()
    log_event("success", "Trading scheduler started", {
        "scan_interval": f"{scan_seconds}s",
        "settlement_interval": f"{settle_seconds}s",
        "min_edge": f"{settings.MIN_EDGE_THRESHOLD:.0%}",
        "btc_enabled": settings.BTC_ENABLED,
        "weather_enabled": settings.WEATHER_ENABLED,
    })

    if settings.BTC_ENABLED:
        asyncio.create_task(scan_and_trade_job())

    if settings.WEATHER_ENABLED:
        asyncio.create_task(weather_scan_and_trade_job())


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler

    if scheduler is None or not scheduler.running:
        log_event("info", "Scheduler not running")
        return

    scheduler.shutdown(wait=True)
    scheduler = None
    log_event("info", "Scheduler stopped")


def is_scheduler_running() -> bool:
    """Check if scheduler is currently running."""
    return scheduler is not None and scheduler.running


async def run_manual_scan():
    """Trigger a manual market scan."""
    log_event("info", "Manual scan triggered")
    await scan_and_trade_job()


async def run_manual_settlement():
    """Trigger a manual settlement check."""
    log_event("info", "Manual settlement triggered")
    await settlement_job()
