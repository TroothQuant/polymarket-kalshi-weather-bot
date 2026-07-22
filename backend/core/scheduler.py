"""Background scheduler for BTC 5-min autonomous trading."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal, PnlSnapshot
from backend.core.notify import notify_push  # JOB 1 (2026-07-09): ntfy push alerts
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


# ── G2-2 (weather-live-v1): flag-gated LIVE execution for the weather job ──────
# All of this is inert while settings.WEATHER_LIVE_TRADING is False (the default
# and the only state through G2): resolve_weather_live() returns a "paper"
# decision and the live_trader is never imported or constructed.
from collections import namedtuple

# action: "paper" (flag off / not polymarket — write the normal paper row)
#         | "fill"  (confirmed live fill — write a row with the ACTUAL economics)
#         | "skip"  (missing token id, or order unfilled — write NO row, continue)
#         | "halt"  (daily live-loss kill-switch tripped — stop new live opens)
LiveDecision = namedtuple("LiveDecision", ["action", "entry_price", "size", "order_id"])


def _live_daily_realized_loss(db) -> float:
    """Magnitude (>=0) of today's realized loss on LIVE polymarket weather trades.
    Live trades are exactly those with a non-NULL order_id."""
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
        Trade.order_id.isnot(None),
        Trade.market_type == "weather",
        Trade.platform == "polymarket",
        Trade.settled == True,  # noqa: E712
        Trade.pnl < 0,
        # Key the daily halt on SETTLEMENT day, not entry day — a position opened
        # yesterday that settles at a loss today must count toward today's halt
        # (mirrors the BTC breaker at ~line 110). Using entry `timestamp` would let
        # yesterday's losses escape today's kill-switch (audit 2, 2026-07-01).
        Trade.settlement_time >= day_start,
    ).scalar() or 0.0
    return abs(float(total))


def _live_total_open_exposure(db) -> float:
    """Summed stake (USD) on OPEN live polymarket weather positions (order_id NOT
    NULL, still pending). The OI1 total-exposure cap is checked against this."""
    total = db.query(func.coalesce(func.sum(Trade.size), 0.0)).filter(
        Trade.order_id.isnot(None),
        Trade.market_type == "weather",
        Trade.platform == "polymarket",
        Trade.result == "pending",
    ).scalar() or 0.0
    return float(total)


# F3: in-memory consecutive-divergence counter per token (resets on restart). A
# divergence must persist >= RECON_GRACE_CYCLES cycles before it's flagged, since
# data-api lags fresh fills.
_recon_divergence_counts: dict = {}
RECON_GRACE_CYCLES = 2


def _sync_live_bankroll() -> bool:
    """D4: when live, set BotState.bankroll to the REAL on-chain wallet balance
    (sig_type=3) so Kelly sizing + the dashboard 'current cash' reflect real
    money, not INITIAL_BANKROLL. Returns True if it's safe to open new positions
    this cycle; False if live AND the balance read failed — caller skips new
    entries, and bankroll is left UNCHANGED (never zeroed). No-op (True) on paper.

    Realized P&L is computed elsewhere from settled Trade rows, NOT from this
    bankroll/wallet value — so operator deposits / profit-sweeps never read as P&L."""
    if not settings.WEATHER_LIVE_TRADING:
        return True
    try:
        from backend.core.live_trader import WeatherLiveTrader
        bal = WeatherLiveTrader().get_balance()
    except Exception as e:
        log_event("warning", f"[live] bankroll sync failed ({e}); bankroll unchanged, skipping new entries this cycle")
        return False
    if bal is None:
        log_event("warning", "[live] balance read None; bankroll unchanged, skipping new entries this cycle")
        return False
    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if state is not None:
            state.bankroll = float(bal)
            db.commit()
            log_event("data", f"[live] bankroll synced to on-chain wallet: ${bal:.2f}")
    finally:
        db.close()
    return True


def _fetch_onchain_positions(funder: str) -> dict:
    """F3: data-api/positions for the funder → {token_id: shares} for size>0.1.
    Read-only, unauthenticated. Raises on network error (caller catches)."""
    import json
    import urllib.request
    url = f"https://data-api.polymarket.com/positions?user={funder}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    out = {}
    for p in (data or []):
        tok = str(p.get("asset") or "")
        try:
            sz = float(p.get("size") or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if tok and sz > 0.1:
            out[tok] = sz
    return out


def _reconcile_live_positions() -> None:
    """F3: per-cycle reconcile recorded OPEN live positions vs data-api/positions,
    BOTH directions — ghost (recorded, not on-chain) + phantom (on-chain, not
    recorded; the E3 backstop). Surface-only: logs + sets BotState.reconcile_status
    for the dashboard; NEVER auto-closes. A divergence must persist
    RECON_GRACE_CYCLES cycles before flagging (data-api lags fresh fills). No-op
    on paper."""
    if not settings.WEATHER_LIVE_TRADING:
        return
    funder = getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)
    if not funder:
        return
    try:
        onchain = _fetch_onchain_positions(funder)
    except Exception as e:
        log_event("warning", f"[live] reconcile: data-api fetch failed ({e}); skipping this cycle")
        return
    db = SessionLocal()
    try:
        rows = db.query(Trade).filter(
            Trade.order_id.isnot(None),
            Trade.market_type == "weather",
            Trade.result == "pending",
            Trade.token_id.isnot(None),
        ).all()
        recorded = {t.token_id for t in rows}
        onchain_toks = set(onchain.keys())
        # Unredeemed WON live positions still sit on-chain until the manual F1
        # claim (redeem_won is not wired yet). Their rows are no longer 'pending',
        # so they'd read as phantoms even though they're expected.
        # P0-2 (2026-07-19): exclude EVERY settled-win token still on-chain from the
        # phantom set, regardless of age — an unredeemed win is benign at any age
        # (alert-only, never a CONFIRMED divergence). The old 48h cutoff let a win
        # age into a "phantom" and the tripwire PAUSED the live book for ~6 days
        # (the away-week failure). Any on-chain token that matches a settled-win row
        # is a known unclaimed win, not a divergence.
        won_rows = db.query(Trade).filter(
            Trade.order_id.isnot(None),
            Trade.market_type == "weather",
            Trade.result == "win",
            Trade.token_id.isnot(None),
        ).all()
        unclaimed_win_toks = {t.token_id for t in won_rows}
        ghosts = recorded - onchain_toks                        # recorded but NOT on-chain
        phantoms = (onchain_toks - recorded) - unclaimed_win_toks  # on-chain, unrecorded, not a known unclaimed win
        diverged = ghosts | phantoms
        for tok in list(_recon_divergence_counts):
            if tok not in diverged:
                _recon_divergence_counts.pop(tok, None)
        for tok in diverged:
            _recon_divergence_counts[tok] = _recon_divergence_counts.get(tok, 0) + 1
        conf_ghost = [t for t in ghosts if _recon_divergence_counts[t] >= RECON_GRACE_CYCLES]
        conf_phantom = [t for t in phantoms if _recon_divergence_counts[t] >= RECON_GRACE_CYCLES]
        state = db.query(BotState).first()
        if conf_ghost or conf_phantom:
            msg = f"⚠ {len(conf_phantom)} phantom / {len(conf_ghost)} ghost (>= {RECON_GRACE_CYCLES} cycles)"
            log_event("warning", f"[live] RECONCILE DIVERGENCE: {msg}; phantom={conf_phantom} ghost={conf_ghost}")
            if state is not None:
                state.reconcile_status = msg
                db.commit()
        elif state is not None and state.reconcile_status not in (None, "ok"):
            state.reconcile_status = "ok"
            db.commit()
    finally:
        db.close()


def resolve_weather_live(signal, trade_size, entry_price, db, settings, live_trader_factory,
                         resting_notional: float = 0.0, resting_ok: bool = True) -> LiveDecision:
    """Decide paper-vs-live for one weather candidate. Pure except for the
    injected db / live_trader_factory, so it unit-tests with mocks.

    Live fires ONLY for polymarket weather with the master flag on — Kalshi
    (platform != 'polymarket') and any non-weather can never reach the live path
    (defense-in-depth on top of the existing Kalshi kill-switch)."""
    platform = getattr(signal.market, "platform", "polymarket")
    # Belt-and-suspenders: this job only builds weather rows, but guard the live
    # path on market_type too so a future caller can never reach it with non-weather.
    market_type = getattr(signal.market, "market_type", "weather")
    # P0-1 pause honesty (2026-07-19): on the LIVE deployment, a false live-trading
    # flag means PAUSED → HALT (never write unmarked sim rows into the live DB /
    # :8003 dashboard — the away-week phantom-#3–#9 failure). The PAPER deployment
    # (WEATHER_LIVE_DEPLOYMENT unset) keeps its normal paper-simulate behavior.
    if (getattr(settings, "WEATHER_LIVE_DEPLOYMENT", False)
            and platform == "polymarket" and market_type == "weather"
            and not settings.WEATHER_LIVE_TRADING):
        return LiveDecision("halt_paused", entry_price, trade_size, None)
    if not (settings.WEATHER_LIVE_TRADING and platform == "polymarket"
            and market_type == "weather"):
        return LiveDecision("paper", entry_price, trade_size, None)

    # OI1 NYC-only live restriction (defense-in-depth; the scan is also limited
    # to WEATHER_CITIES). Non-NYC weather still trades on PAPER, never live.
    live_cities = {c.strip().lower() for c in settings.WEATHER_LIVE_CITIES.split(",") if c.strip()}
    city = (getattr(signal.market, "city_key", "") or "").lower()
    if live_cities and city not in live_cities:
        return LiveDecision("paper", entry_price, trade_size, None)

    # F3 guard: must know the CLOB token of the exact side we're buying. Never guess.
    token_id = (signal.market.token_id_yes if signal.direction == "yes"
                else signal.market.token_id_no)
    if not token_id:
        return LiveDecision("skip", entry_price, trade_size, None)

    # Daily realized-loss kill-switch (live only).
    if _live_daily_realized_loss(db) >= settings.WEATHER_LIVE_DAILY_LOSS_STOP_USD:
        return LiveDecision("halt", entry_price, trade_size, None)

    # AGGRESSIVE-HYBRID pricing (v2, 2026-07-09). limit_price = the model's
    # probability for OUR side minus the minimum-edge floor — the MOST we will
    # ever pay; within it we sweep + rest maximally aggressively so we never miss
    # a fillable trade over pennies. `model_probability` is the ensemble YES prob.
    min_edge_floor = getattr(settings, "WEATHER_LIVE_MIN_EDGE_FLOOR", 0.05)
    model_p_side = (signal.model_probability if signal.direction == "yes"
                    else 1.0 - signal.model_probability)
    limit_price = model_p_side - min_edge_floor

    # 15-share guard + min-size bump ("never miss a fillable trade over pennies"):
    # size the stake UP to >=15 shares' worth at limit_price so a below-cap Kelly
    # size can never produce a sub-15-share order. The guard fires (clean skip with
    # a filter note) ONLY when even the FULL per-trade cap can't clear 15 shares.
    min_for_15 = 15.0 * limit_price + 0.05
    if limit_price <= 0 or min_for_15 > settings.WEATHER_LIVE_MAX_TRADE_USD:
        return LiveDecision("guard_skip", limit_price, trade_size, None)

    # Hard per-trade dollar cap, floored to clear the 15-share minimum.
    live_size = min(max(trade_size, min_for_15), settings.WEATHER_LIVE_MAX_TRADE_USD)

    # BUG 1 fail-closed: if resting exposure is UNKNOWN (open-orders list failed),
    # do NOT open — we can't prove we're under the cap.
    if not resting_ok:
        return LiveDecision("exposure_skip", entry_price, trade_size, None)

    # OI1 hard TOTAL open-exposure cap. Filled open stake (DB pending) + UNFILLED
    # resting-order notional (BUG 1, 2026-07-10) + this trade must not exceed the
    # ceiling. Resting orders were previously uncounted → could breach the cap.
    if (_live_total_open_exposure(db) + resting_notional + live_size
            > settings.WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD):
        return LiveDecision("halt", entry_price, trade_size, None)

    # LADDER (2026-07-22, census params): up to 3 simultaneous maker rungs at
    # model_p_side − offsets, budget split of the cap; take-first sweep at
    # −offsets[0] unchanged (execute_ladder delegates to the hybrid when the
    # book has fillable asks). Flag off → the proven single-order hybrid.
    if getattr(settings, "WEATHER_LIVE_LADDER", False):
        offsets = tuple(float(x) for x in
                        str(getattr(settings, "WEATHER_LIVE_LADDER_OFFSETS", "0.05,0.09,0.13")).split(","))
        split = tuple(float(x) for x in
                      str(getattr(settings, "WEATHER_LIVE_LADDER_SPLIT", "0.40,0.30,0.30")).split(","))
        fill = live_trader_factory().execute_ladder(
            token_id, live_size, model_p_side, offsets=offsets, split=split)
    else:
        fill = live_trader_factory().execute_aggressive_hybrid(token_id, live_size, limit_price)
    if fill is None:
        return LiveDecision("skip", limit_price, trade_size, None)  # hard failure → NO row

    if fill["filled_shares"] > 0:
        # Immediate TAKE → write the row for the filled portion NOW. Any resting
        # remainder is managed cross-cycle by manage_live_resting_orders.
        return LiveDecision("fill", fill["fill_price"], fill["filled_cost"], fill["order_id"])

    # Pure REST (0 immediate take): the GTC order sits top-of-book for the next
    # seller. No row yet; manage_live_resting_orders records it when it fills.
    return LiveDecision("rested", limit_price, trade_size, fill["order_id"])


def _resting_at_settlement_approach(target_date) -> bool:
    """True if a resting order's market is at/after its weather day (resolution
    imminent) → cancel the resting bid (never open fresh exposure into a resolving
    market). Unknown/unparseable date → False (leave it; reconcile is the backstop)."""
    if not target_date:
        return False
    try:
        from datetime import date as _date
        if isinstance(target_date, str):
            td = _date.fromisoformat(target_date[:10])
        elif isinstance(target_date, datetime):
            td = target_date.date()
        else:
            td = target_date  # already a date
        return datetime.utcnow().date() >= td
    except Exception:
        return False


def manage_live_resting_orders(trader, db, market_by_token, settings):
    """AGGRESSIVE-HYBRID cross-cycle lifecycle (v2, 2026-07-09; BUG-1 fix 2026-07-10).
    For every resting live BUY order: (1) record late fills as Trade rows (mapping
    token→market from this cycle's markets, or a prior row for the same order —
    pure-rest fills with no prior row are still caught LOUDLY by the F3 phantom
    reconcile); (2) cancel orders whose market is at settlement approach.

    Returns (resting_tokens, resting_notional, ok):
      - resting_tokens: tokens that STILL have a live resting order (entry-loop dedupe).
      - resting_notional: sum of price*(size − size_matched) over live resting BUYs —
        the UNCOUNTED exposure the $33 cap must now include (BUG 1).
      - ok: False if the open-orders LIST failed → caller FAILS CLOSED (skips new
        entries this cycle) because resting exposure is unknown.
    Never raises — resting-order management must never take down the scan."""
    resting_tokens: set = set()
    resting_notional = 0.0
    if not settings.WEATHER_LIVE_TRADING:
        return resting_tokens, 0.0, True
    try:
        orders = trader.list_open_weather_orders()
    except Exception as e:
        log_event("warning", f"[live] resting-order list failed ({e}) — resting exposure UNKNOWN, failing closed (no new entries this cycle)")
        return resting_tokens, 0.0, False
    for o in orders:
        try:
            order_id = o.get("id") or o.get("orderID") or o.get("order_id")
            token = str(o.get("asset_id") or o.get("token_id") or o.get("tokenID") or "")
            if not order_id or not token:
                continue
            try:
                price = float(o.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            matched = trader._matched_shares(o)

            prior = db.query(Trade).filter(Trade.order_id == order_id).all()
            recorded = 0.0
            for t in prior:
                if t.entry_price:
                    recorded += (t.size / t.entry_price)
            meta = market_by_token.get(token)
            if meta is None and prior:
                p0 = prior[0]
                meta = {"market_id": p0.market_ticker, "slug": p0.event_slug,
                        "direction": p0.direction, "model_probability": p0.model_probability,
                        "market_probability": p0.market_price_at_entry,
                        "edge": p0.edge_at_entry, "target_date": None}

            delta = matched - recorded
            if delta >= 1.0 and price > 0 and meta is not None:
                db.add(Trade(
                    market_ticker=meta["market_id"], platform="polymarket",
                    event_slug=meta.get("slug"), market_type="weather",
                    direction=meta["direction"], entry_price=price,
                    size=round(delta * price, 6),
                    model_probability=meta.get("model_probability"),
                    market_price_at_entry=meta.get("market_probability"),
                    edge_at_entry=meta.get("edge"),
                    order_id=order_id, token_id=token,
                ))
                db.commit()
                log_event("success",
                          f"[live] resting fill recorded: {delta:.1f} sh @ {price:.3f} on "
                          f"{meta['market_id']} (order {str(order_id)[:10]}…)")
                notify_push(  # watchdog C: a resting order caught a seller
                    "Weather LIVE fill (rested)",
                    f"{delta:.1f} sh @ {price:.3f} on {meta['market_id']} "
                    f"({meta.get('direction','?').upper()}) — resting order filled.",
                    priority="high", tags="moneybag")

            # Settlement-approach cancel (only when we can date the market).
            if meta is not None and _resting_at_settlement_approach(meta.get("target_date")):
                if trader.cancel(order_id):
                    log_event("info",
                              f"[live] cancelled resting {str(order_id)[:10]}… on "
                              f"{meta['market_id']} @ settlement approach")
                continue  # cancelled → not a live resting token anymore

            # Edge-flip / adverse-selection cancel (2026-07-22, ladder-arming
            # requirement). Acts ONLY on FRESH meta (this scan's refreshed
            # signal — the DB-fallback meta above carries stale entry-time
            # numbers and must never trigger a cancel). Cancels when:
            #   (a) the refreshed signal no longer wants this side (not
            #       actionable, or the direction flipped — the both-token
            #       keying above makes a flip land here as actionable=False); or
            #   (b) protective_action says the resting price no longer clears
            #       the min-edge floor against the refreshed model (a stale
            #       high bid is adverse-selection bait). We cancel rather than
            #       reprice: strictly safer, and the next scan reposts at the
            #       fresh price if the signal is still actionable.
            if meta is not None and meta.get("fresh") and price > 0:
                floor = getattr(settings, "WEATHER_LIVE_MIN_EDGE_FLOOR", 0.05)
                pa = trader.protective_action(price, meta.get("model_p_side", 0.0), floor)
                if (not meta.get("actionable", True)) or pa["action"] != "hold":
                    why = ("signal no longer actionable for this side"
                           if not meta.get("actionable", True)
                           else f"price {price:.3f} no longer clears the edge floor "
                                f"(refreshed p_side {meta.get('model_p_side', 0):.3f}, "
                                f"protective={pa['action']})")
                    if trader.cancel(order_id):
                        log_event("info",
                                  f"[live] cancelled resting {str(order_id)[:10]}… on "
                                  f"{meta['market_id']} — edge-flip guard: {why}")
                        continue  # cancelled → not a live resting token anymore
                    # cancel failed → fall through: keep counting its exposure

            resting_tokens.add(token)
            # BUG 1: this order's remaining notional counts toward the exposure cap.
            try:
                osz = float(o.get("original_size") or o.get("size") or 0)
            except (TypeError, ValueError):
                osz = 0.0
            remaining = max(0.0, osz - matched)
            resting_notional += price * remaining
        except Exception as e:
            log_event("warning", f"[live] resting-order manage error ({e})")
    return resting_tokens, resting_notional, True


async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every 5 minutes when WEATHER_ENABLED.
    """
    log_event("info", "Scanning weather temperature markets...")

    # D4/F3 (2026-06-26): when live, sync bankroll to the REAL wallet BEFORE
    # sizing, and reconcile recorded vs on-chain positions. Both are no-ops on
    # paper. If the balance read fails, skip NEW entries this cycle (review +
    # settlement, which run in other jobs, are unaffected).
    live_entries_ok = _sync_live_bankroll()
    _reconcile_live_positions()

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
            # G2-2 (weather-live-v1): lazy live-trader holder. Constructed at most
            # once per scan and ONLY when WEATHER_LIVE_TRADING is on, so
            # py-clob-client is never imported on the paper path. Inert while the
            # flag is False (resolve_weather_live returns a "paper" decision and
            # never calls the factory).
            _live_trader_box = []

            def _live_trader_factory():
                if not _live_trader_box:
                    from backend.core.live_trader import WeatherLiveTrader
                    _live_trader_box.append(WeatherLiveTrader())
                return _live_trader_box[0]

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
            if not live_entries_ok:
                log_event("info", "[live] balance unavailable this cycle — skipping new entries (position review/settlement run separately)")

            # AGGRESSIVE-HYBRID (v2, 2026-07-09): manage resting orders BEFORE
            # opening new ones — record late fills, cancel at settlement approach,
            # and collect the set of tokens already resting so we never double-open
            # (the Trade-row dedup below can't see a pure-rest order that has no row
            # yet). Live-only; never raises into the scan.
            resting_tokens: set = set()
            resting_notional = 0.0
            resting_ok = True
            if settings.WEATHER_LIVE_TRADING and live_entries_ok:
                market_by_token = {}
                for s in signals:
                    m = s.market
                    # Key BOTH side tokens (2026-07-22): a refreshed signal that
                    # flips direction changes which token the signal points at —
                    # keying only the signal side let a stale OTHER-side rest
                    # escape the edge-flip guard below. "actionable"/"fresh"
                    # feed manage_live_resting_orders' adverse-selection cancel.
                    p_yes = s.model_probability
                    for side, tok in (("yes", m.token_id_yes), ("no", m.token_id_no)):
                        if not tok:
                            continue
                        market_by_token[str(tok)] = {
                            "market_id": m.market_id, "slug": getattr(m, "slug", None),
                            "direction": side,
                            "model_probability": p_yes,
                            "market_probability": getattr(s, "market_probability", None),
                            "edge": s.edge,
                            "target_date": getattr(m, "target_date", None),
                            "model_p_side": (p_yes if side == "yes" else 1.0 - p_yes),
                            # this side is only "still wanted" if the refreshed
                            # signal is actionable AND still points at this side
                            "actionable": bool(getattr(s, "passes_threshold", False)
                                               and s.direction == side),
                            "fresh": True,
                        }
                try:
                    resting_tokens, resting_notional, resting_ok = manage_live_resting_orders(
                        _live_trader_factory(), db, market_by_token, settings)
                except Exception as e:
                    # Unexpected manage failure → fail CLOSED (resting exposure unknown).
                    log_event("warning", f"[live] resting-order management failed ({e}) — failing closed")
                    resting_ok = False

            for signal in (actionable[:MAX_TRADES_PER_SCAN] if live_entries_ok else []):
                try:
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

                    # Resting-order dedup (v2): a pure-rest GTC order has no Trade
                    # row yet, so the row-based dedup above can't see it. Skip
                    # re-opening a market that already has a live resting order.
                    our_tok = (signal.market.token_id_yes if signal.direction == "yes"
                               else signal.market.token_id_no)
                    if our_tok and str(our_tok) in resting_tokens:
                        log_event(
                            "info",
                            f"[live] {signal.market.market_id} already resting (live GTC) "
                            f"— not double-opening.")
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

                    # ── G2-2 flag-gated LIVE execution ────────────────────────────
                    # Flag OFF (default, all of G2) → action == "paper": entry_price
                    # and trade_size pass through unchanged, order_id is None, and the
                    # row write below is byte-for-byte the original paper behaviour.
                    decision = resolve_weather_live(
                        signal, trade_size, entry_price, db, settings, _live_trader_factory,
                        resting_notional=resting_notional, resting_ok=resting_ok)
                    if decision.action == "halt_paused":
                        # P0-1: live deployment is PAUSED (flag false) → write NO row,
                        # simulate NOTHING. The dashboard must never show sim as live.
                        log_event(
                            "info",
                            "[live] PAUSED (WEATHER_LIVE_TRADING=false on the live deployment) "
                            "— entries halted, no rows written.")
                        break
                    if decision.action == "halt":
                        log_event(
                            "warning",
                            f"[live] daily realized-loss (${settings.WEATHER_LIVE_DAILY_LOSS_STOP_USD:.0f}) "
                            f"or total-exposure (${settings.WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD:.0f}) cap "
                            f"reached — halting new live opens for the day.")
                        break
                    if decision.action == "guard_skip":
                        # Watchdog B: actionable but structurally un-submittable at
                        # the cap (15-share min). Clean skip WITH the reason — never
                        # a silent miss (this replaces today's sub-15-share refusals).
                        lp = decision.entry_price
                        _msg = (f"{signal.market.market_id} actionable but skipped: "
                                f"15 x limit ${15.0 * lp:.2f} > cap "
                                f"${settings.WEATHER_LIVE_MAX_TRADE_USD:.0f} (sub-15-share guard).")
                        log_event("alert", f"[UNFILLED] {_msg}")
                        notify_push("Weather live: UNFILLED (guard)", _msg,
                                    priority="high", tags="warning")  # watchdog B
                        continue
                    if decision.action == "exposure_skip":
                        # BUG 1 fail-closed: resting exposure unknown this cycle.
                        log_event(
                            "warning",
                            f"[live] {signal.market.market_id} skipped — resting-order "
                            f"exposure unknown (open-orders list failed); failing closed.")
                        continue
                    if decision.action == "rested":
                        # Aggressive-hybrid took 0 immediately; the GTC limit is now
                        # resting top-of-book for the next seller. Expected, not a
                        # failure — no row until it fills (manage records it).
                        log_event(
                            "info",
                            f"[live] {signal.market.market_id} resting GTC @ {decision.entry_price:.3f} "
                            f"(order {str(decision.order_id)[:10]}…) — first in queue, 0 immediate take.")
                        continue
                    if decision.action == "skip":
                        # Watchdog B: hard construction/post failure or missing token.
                        _msg = (f"{signal.market.market_id} actionable but NOT opened "
                                f"(order build/post failed or missing token id).")
                        log_event("alert", f"[UNFILLED] {_msg}")
                        notify_push("Weather live: UNFILLED (error)", _msg,
                                    priority="high", tags="rotating_light")  # watchdog B
                        continue
                    # "paper" → unchanged; "fill" → ACTUAL fill price/cost + order_id.
                    entry_price = decision.entry_price
                    trade_size = decision.size
                    order_id = decision.order_id

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
                        order_id=order_id,  # NULL on paper; CLOB id on a live fill
                        # F3: store the bought outcome's CLOB token id on live fills
                        # (NULL on paper) so reconciliation can match data-api/positions.
                        token_id=((signal.market.token_id_yes if signal.direction == "yes"
                                   else signal.market.token_id_no) if order_id else None),
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
                    # Watchdog C: push on a LIVE (real-money) take fill — the
                    # first natural aggressive-hybrid fill is the moment we've been
                    # waiting for. order_id present == live (NULL on paper).
                    if order_id:
                        notify_push(
                            "Weather LIVE fill (take)",
                            f"{signal.market.city_name} {signal.direction.upper()} "
                            f"${trade_size:.2f} @ {entry_price:.3f} on {signal.market.market_id}.",
                            priority="high", tags="moneybag")

                    # Persist THIS candidate now — a booked row (especially a
                    # real-money live fill) must survive a LATER candidate raising
                    # (audit 1 CRITICAL, 2026-07-01). Paper rows commit here too,
                    # which is benign and keeps the rollback below from discarding
                    # earlier candidates' committed work.
                    db.commit()
                except Exception as _cand_err:
                    db.rollback()
                    log_event("error", f"[scan] candidate {getattr(signal.market, 'market_id', '?')} errored — skipping: {_cand_err}")
                    continue
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
                # Watchdog C: push only when a LIVE (real-money) position settled.
                live_settled = [t for t in settled if getattr(t, "order_id", None)]
                if live_settled:
                    lw = sum(1 for t in live_settled if t.result == "win")
                    ll = sum(1 for t in live_settled if t.result == "loss")
                    lpnl = sum(t.pnl for t in live_settled if t.pnl is not None)
                    notify_push(
                        "Weather LIVE settled",
                        f"{len(live_settled)} live: {lw}W/{ll}L, P&L ${lpnl:+.2f}. "
                        f"(WON needs manual F1 redeem within 48h.)",
                        priority="high", tags="moneybag")

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
