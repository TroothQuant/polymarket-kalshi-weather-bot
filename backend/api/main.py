"""FastAPI backend for BTC 5-min trading bot dashboard."""
from fastapi import FastAPI, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from typing import List, Optional
import asyncio
import json
import logging
import os

# Audit 2026-05-19 HIGH #11: every `except Exception` in this module used to
# silently swallow real errors and return [] or None. The dashboard would
# show zeros with no clue why. logger.exception() on these surfaces the
# stack trace to bot.log; the endpoint still returns its safe-default value
# so the dashboard keeps rendering, but the root cause is now visible.
logger = logging.getLogger("backend.api.main")

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog, ScanLog, PnlSnapshot
)
from backend.core.signals import scan_for_signals, TradingSignal
from backend.data.btc_markets import fetch_active_btc_markets, BtcMarket
from backend.data.crypto import fetch_crypto_price, compute_btc_microstructure

from pydantic import BaseModel

app = FastAPI(
    title="BTC 5-Min Trading Bot",
    description="Polymarket BTC Up/Down 5-minute market trading bot",
    version="3.0.0"
)

# Audit 2026-05-19 CRITICAL #2: restrict CORS to known local origins.
# Origins are parsed from settings.API_ALLOWED_ORIGINS (comma-separated).
_allowed_origins = [
    o.strip()
    for o in (settings.API_ALLOWED_ORIGINS or "").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# Audit 2026-05-19 CRITICAL #2: optional bearer-token guard on mutating POSTs.
# When settings.API_AUTH_TOKEN is set, every endpoint that uses
# Depends(require_auth_token) requires `Authorization: Bearer <token>`.
# When the token is unset (paper-only local default), this is a no-op so
# the dashboard keeps working without configuration changes.
async def require_auth_token(authorization: Optional[str] = Header(default=None)):
    token = settings.API_AUTH_TOKEN
    if not token:
        # Auth not configured -- act like a no-op. Safe because the host
        # binding (settings.API_HOST = 127.0.0.1 by default) already blocks
        # off-host access.
        return None
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token. "
                   "Set Authorization: Bearer <API_AUTH_TOKEN> on this request.",
        )
    return None


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Per-client send failure (closed socket etc) — disconnect
                # cleanly rather than spamming the log.
                logger.debug("WS broadcast to one client failed", exc_info=True)


ws_manager = ConnectionManager()


# Pydantic response models
class BtcPriceResponse(BaseModel):
    price: float
    change_24h: float
    change_7d: float
    market_cap: float
    volume_24h: float
    last_updated: datetime


class BtcWindowResponse(BaseModel):
    slug: str
    market_id: str
    up_price: float
    down_price: float
    window_start: datetime
    window_end: datetime
    volume: float
    is_active: bool
    is_upcoming: bool
    time_until_end: float
    spread: float


class MicrostructureResponse(BaseModel):
    rsi: float = 50.0
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    momentum_15m: float = 0.0
    vwap_deviation: float = 0.0
    sma_crossover: float = 0.0
    volatility: float = 0.0
    price: float = 0.0
    source: str = "unknown"


class SignalResponse(BaseModel):
    market_ticker: str
    market_title: str
    platform: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    timestamp: datetime
    category: str = "crypto"
    event_slug: Optional[str] = None
    btc_price: float = 0.0
    btc_change_24h: float = 0.0
    window_end: Optional[datetime] = None
    actionable: bool = False


class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    settlement_time: Optional[datetime] = None
    settlement_value: Optional[float] = None
    result: str
    pnl: Optional[float]
    edge_at_entry: Optional[float] = None


class WeatherPnlByPlatform(BaseModel):
    polymarket: float
    kalshi: float
    total: float
    polymarket_trades: int
    kalshi_trades: int


class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    weather_pnl_by_platform: Optional[WeatherPnlByPlatform] = None
    is_running: bool
    last_run: Optional[datetime]
    # Audit 2026-05-19 HIGH #9: previously win_rate was
    # winning_trades / total_trades, which counted stop_loss trades against
    # the bot. Now win_rate excludes stop_loss settlements (the trade was
    # closed before reaching the model's actual prediction outcome), and we
    # expose stop_loss_count + stop_loss_rate so the operator can see what
    # fraction of trades hit the safety rail.
    stop_loss_count: int = 0
    stop_loss_rate: float = 0.0


class CalibrationBucket(BaseModel):
    bucket: str
    predicted_avg: float
    actual_rate: float
    count: int


class CalibrationSummary(BaseModel):
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


class WeatherForecastResponse(BaseModel):
    city_key: str
    city_name: str
    target_date: str
    mean_high: float
    std_high: float
    mean_low: float
    std_low: float
    num_members: int
    ensemble_agreement: float


class WeatherMarketResponse(BaseModel):
    slug: str
    market_id: str
    platform: str = "polymarket"
    title: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    yes_price: float
    no_price: float
    volume: float


class WeatherSignalResponse(BaseModel):
    market_id: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    ensemble_mean: float
    ensemble_std: float
    ensemble_members: int
    actionable: bool = False


class DashboardData(BaseModel):
    stats: BotStats
    btc_price: Optional[BtcPriceResponse]
    microstructure: Optional[MicrostructureResponse] = None
    windows: List[BtcWindowResponse]
    active_signals: List[SignalResponse]
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    calibration: Optional[CalibrationSummary] = None
    weather_signals: List[WeatherSignalResponse] = []
    weather_forecasts: List[WeatherForecastResponse] = []


class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}


# Startup / Shutdown
@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("BTC 5-MIN TRADING BOT v3.0")
    print("=" * 60)
    print("Initializing database...")

    init_db()

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=True
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades")
    finally:
        db.close()

    print("")
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Min edge threshold: {settings.MIN_EDGE_THRESHOLD:.0%}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Scan interval: {settings.SCAN_INTERVAL_SECONDS}s")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("")

    print("Weather configuration:")
    print(f"  - Min edge threshold: {settings.WEATHER_MIN_EDGE_THRESHOLD:.2f}")
    print(f"  - Max edge threshold: {settings.WEATHER_MAX_EDGE_THRESHOLD:.2f}")
    print(f"  - Min entry price: ${settings.WEATHER_MIN_ENTRY_PRICE:.2f}")
    print(f"  - Max entry price: ${settings.WEATHER_MAX_ENTRY_PRICE:.2f}")
    print(f"  - Max clipped edge: {settings.WEATHER_MAX_CLIPPED_EDGE:.2f}")
    print(f"  - Max trade size: ${settings.WEATHER_MAX_TRADE_SIZE:.1f}")
    print(f"  - Max allocation: ${settings.WEATHER_MAX_ALLOCATION_USD:.1f}")
    print(f"  - Max new positions/day: {settings.WEATHER_MAX_NEW_POSITIONS_PER_DAY}")
    print(f"  - Stop-loss enabled: {settings.WEATHER_STOP_LOSS_ENABLED}")
    if settings.WEATHER_STOP_LOSS_ENABLED:
        print(f"  - Stop-loss fraction: {settings.WEATHER_STOP_LOSS_FRACTION:.2f}")
    print(f"  - Disable YES entries: {settings.WEATHER_DISABLE_YES_ENTRIES}")
    print("")

    print("Kalshi configuration:")
    print(f"  - Kalshi enabled: {settings.KALSHI_ENABLED}")
    print(f"  - Kalshi trading enabled: {settings.KALSHI_TRADING_ENABLED}")
    print("")

    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    log_event("success", "BTC 5-min trading bot initialized")

    # CRYPTO5050 paper book (2026-07-22): its OWN asyncio task, entirely outside
    # the weather scheduler. Double-wrapped: the runner catches its own loop
    # errors, and this hook catches even a failed task CREATION — a crypto5050
    # problem can never take down the weather path.
    if getattr(settings, "CRYPTO_5050_ENABLED", False):
        try:
            from backend.core.crypto5050 import start_crypto5050
            from backend.models.database import SessionLocal as _c5050_session
            app.state.crypto5050_task = start_crypto5050(settings, _c5050_session, log_event)
            print("  - CRYPTO5050 paper book: ENABLED (own task, paper-only)")
        except Exception as _e:
            log_event("error", f"[c5050] failed to start (weather unaffected): {_e}")
    else:
        print("  - CRYPTO5050 paper book: disabled")

    print("Bot is now running!")
    print(f"  - BTC scan: every {settings.SCAN_INTERVAL_SECONDS}s (edge >= {settings.MIN_EDGE_THRESHOLD:.0%})")
    print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    if settings.WEATHER_ENABLED:
        print(f"  - Weather scan: every {settings.WEATHER_SCAN_INTERVAL_SECONDS}s (edge >= {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%})")
        print(f"  - Weather cities: {settings.WEATHER_CITIES}")
    else:
        print("  - Weather trading: DISABLED")
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()


# Core endpoints
@app.get("/")
async def root():
    return {"status": "ok", "message": "BTC 5-Min Trading Bot API v3.0", "simulation_mode": settings.SIMULATION_MODE}


@app.get("/api/health")
async def health():
    return {"status": "healthy"}


def _compute_weather_pnl_by_platform(db: Session) -> WeatherPnlByPlatform:
    rows = (db.query(Trade.platform,
                     func.coalesce(func.sum(Trade.pnl), 0.0),
                     func.count())
              .filter(Trade.market_type == "weather",
                      Trade.settled == True,
                      Trade.pnl.isnot(None))
              .group_by(Trade.platform).all())
    by = {p: (float(pnl), int(n)) for p, pnl, n in rows}
    poly_pnl, poly_n = by.get("polymarket", (0.0, 0))
    kal_pnl, kal_n = by.get("kalshi", (0.0, 0))
    return WeatherPnlByPlatform(
        polymarket=round(poly_pnl, 2),
        kalshi=round(kal_pnl, 2),
        total=round(poly_pnl + kal_pnl, 2),
        polymarket_trades=poly_n,
        kalshi_trades=kal_n,
    )


@app.get("/api/stats", response_model=BotStats)
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    # Audit 2026-05-19 HIGH #9: compute win_rate over trades that actually
    # resolved to a market outcome (win/loss), excluding stop_loss settlements
    # which closed the position before the prediction was tested. Expose
    # stop_loss_count + stop_loss_rate as their own metric.
    stop_loss_count = db.query(Trade).filter(Trade.result == "stop_loss").count()
    resolved_count = db.query(Trade).filter(Trade.result.in_(["win", "loss"])).count()
    win_count = db.query(Trade).filter(Trade.result == "win").count()
    settled_count = stop_loss_count + resolved_count

    win_rate = win_count / resolved_count if resolved_count > 0 else 0.0
    stop_loss_rate = stop_loss_count / settled_count if settled_count > 0 else 0.0

    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=win_count,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        weather_pnl_by_platform=_compute_weather_pnl_by_platform(db),
        is_running=state.is_running,
        last_run=state.last_run,
        stop_loss_count=stop_loss_count,
        stop_loss_rate=stop_loss_rate,
    )


# ── CRYPTO5050 paper book (2026-07-22) — read-only views for the dashboard ───
@app.get("/api/crypto5050/summary")
async def crypto5050_summary(db: Session = Depends(get_db)):
    """Module totals for the dashboard panel. Read-only; separate book —
    nothing here touches the weather stats."""
    from backend.models.database import CryptoWindow
    settled = db.query(CryptoWindow).filter(CryptoWindow.status == "settled")
    n = settled.count()
    sub_dollar = settled.filter(CryptoWindow.pair_vwap < 1.0).count()
    sums = db.query(
        func.coalesce(func.sum(CryptoWindow.net_pnl), 0.0),
        func.coalesce(func.sum(CryptoWindow.locked_pnl), 0.0),
        func.coalesce(func.sum(CryptoWindow.lean_pnl), 0.0),
        func.coalesce(func.sum(CryptoWindow.maker_fills), 0),
        func.coalesce(func.sum(CryptoWindow.taker_fills), 0),
    ).filter(CryptoWindow.status == "settled").first()
    net, locked, lean, mk, tk = sums

    def _hit(col):
        hits = settled.filter(col == 1).count()
        graded = settled.filter(col.isnot(None)).count()
        return {"hits": hits, "n": graded,
                "rate": round(hits / graded, 3) if graded else None}

    return {
        "enabled": bool(getattr(settings, "CRYPTO_5050_ENABLED", False)),
        "windows_settled": n,
        "pct_pair_vwap_sub_dollar": round(sub_dollar / n, 3) if n else None,
        "maker_fill_pct": round(mk / (mk + tk), 3) if (mk + tk) else None,
        "cumulative_net": round(float(net), 2),
        "locked_pnl_total": round(float(locked or 0.0), 2),
        "lean_pnl_total": round(float(lean or 0.0), 2),
        "hit_rates": {"spot_drift": _hit(CryptoWindow.hit_spot_drift),
                      "momentum": _hit(CryptoWindow.hit_momentum),
                      "depth": _hit(CryptoWindow.hit_depth),
                      "late_recency": _hit(CryptoWindow.hit_late_recency),
                      "brownian": _hit(CryptoWindow.hit_brownian)},
        "brownian_abstain_rate": (lambda a, g: round(a / g, 3) if g else None)(
            settled.filter(CryptoWindow.pick_brownian == "abstain").count(),
            settled.filter(CryptoWindow.pick_brownian.isnot(None)).count()),
        "arb": {
            "windows_with_hit": settled.filter(CryptoWindow.arb_hits > 0).count(),
            "total_hits": int(db.query(func.coalesce(func.sum(CryptoWindow.arb_hits), 0))
                              .filter(CryptoWindow.status == "settled").scalar() or 0),
            "total_polls": int(db.query(func.coalesce(func.sum(CryptoWindow.arb_polls), 0))
                               .filter(CryptoWindow.status == "settled").scalar() or 0),
            "best_sum": db.query(func.min(CryptoWindow.arb_best_sum))
                          .filter(CryptoWindow.status == "settled").scalar()},
        "allocation_usd": settings.CRYPTO5050_ALLOCATION_USD,
        "allocation_available": round(settings.CRYPTO5050_ALLOCATION_USD + float(net), 2),
        "window_cap_usd": settings.CRYPTO5050_MAX_WINDOW_NOTIONAL_USD,
    }


@app.get("/api/crypto5050/windows")
async def crypto5050_windows(limit: int = 5, db: Session = Depends(get_db)):
    """Most recent windows, newest first, with both sides broken out per row
    (the Polymarket-positions-style view the dashboard renders)."""
    from backend.models.database import CryptoWindow
    rows = (db.query(CryptoWindow).order_by(CryptoWindow.window_start.desc())
            .limit(max(1, min(int(limit), 50))).all())
    out = []
    for r in rows:
        res_price_up = (1.0 if r.resolution == "up" else 0.0) if r.resolution else None
        # L1 residue = hedge-leg imbalance. Broken out so accidental-imbalance
        # P&L is never read as lean performance (the lean is its own fields).
        u_sh, d_sh = r.up_shares or 0.0, r.down_shares or 0.0
        residue = ({"side": "up", "shares": u_sh - d_sh} if u_sh > d_sh
                   else {"side": "down", "shares": d_sh - u_sh} if d_sh > u_sh
                   else None)
        out.append({
            "l1_residue": residue,
            "slug": r.slug, "question": r.question, "status": r.status,
            "window_start": r.window_start.isoformat() + "Z" if r.window_start else None,
            "fills": r.fills_count or 0, "maker_fills": r.maker_fills or 0,
            "taker_fills": r.taker_fills or 0,
            "pair_vwap": r.pair_vwap, "locked_pairs": r.locked_pairs,
            "locked_pnl": r.locked_pnl,
            "lean": {"side": r.lean_side, "shares": r.lean_shares or 0.0,
                     "price": r.lean_price, "pnl": r.lean_pnl,
                     "mark": (r.up_mark if r.lean_side == "up" else r.down_mark)
                             if r.lean_side else None},
            "picks": {"spot_drift": r.pick_spot_drift, "momentum": r.pick_momentum,
                      "depth": r.pick_depth, "late_recency": r.pick_late_recency,
                      "brownian": r.pick_brownian, "p_up_brownian": r.p_up_brownian},
            "arb": {"polls": r.arb_polls or 0, "hits": r.arb_hits or 0,
                    "best_sum": r.arb_best_sum},
            "resolution": r.resolution, "resolution_source": r.resolution_source,
            "fees_paid": r.fees_paid or 0.0, "net_pnl": r.net_pnl,
            "sides": [
                {"side": "Up", "shares": r.up_shares or 0.0,
                 "avg_price": round((r.up_cost or 0.0) / r.up_shares, 4) if r.up_shares else None,
                 "resolution_price": res_price_up, "mark": r.up_mark},
                {"side": "Down", "shares": r.down_shares or 0.0,
                 "avg_price": round((r.down_cost or 0.0) / r.down_shares, 4) if r.down_shares else None,
                 "resolution_price": (1.0 - res_price_up) if res_price_up is not None else None,
                 "mark": r.down_mark},
            ],
        })
    return out


# BTC-specific endpoints
@app.get("/api/btc/price", response_model=Optional[BtcPriceResponse])
async def get_btc_price():
    """Get current BTC price and momentum data."""
    try:
        btc = await fetch_crypto_price("BTC")
        if not btc:
            return None

        return BtcPriceResponse(
            price=btc.current_price,
            change_24h=btc.change_24h,
            change_7d=btc.change_7d,
            market_cap=btc.market_cap,
            volume_24h=btc.volume_24h,
            last_updated=btc.last_updated
        )
    except Exception:
        logger.exception("GET /api/btc/price failed")
        return None


@app.get("/api/btc/windows", response_model=List[BtcWindowResponse])
async def get_btc_windows():
    """Get upcoming BTC 5-min windows with prices."""
    try:
        markets = await fetch_active_btc_markets()
        return [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        logger.exception("GET /api/btc/windows failed")
        return []


@app.get("/api/signals", response_model=List[SignalResponse])
async def get_signals():
    """Get current BTC trading signals."""
    try:
        signals = await scan_for_signals()
        return [_signal_to_response(s) for s in signals]
    except Exception:
        logger.exception("GET /api/signals failed")
        return []


@app.get("/api/signals/actionable", response_model=List[SignalResponse])
async def get_actionable_signals():
    """Get only signals that pass the edge threshold."""
    try:
        signals = await scan_for_signals()
        actionable = [s for s in signals if s.passes_threshold]
        return [_signal_to_response(s) for s in actionable]
    except Exception:
        logger.exception("GET /api/signals/actionable failed")
        return []


def _signal_to_response(s: TradingSignal, actionable: bool = False) -> SignalResponse:
    return SignalResponse(
        market_ticker=s.market.market_id,
        market_title=f"BTC 5m - {s.market.slug}",
        platform="polymarket",
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        timestamp=s.timestamp,
        category="crypto",
        event_slug=s.market.slug,
        btc_price=s.btc_price,
        btc_change_24h=s.btc_change_24h,
        window_end=s.market.window_end,
        actionable=actionable,
    )


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.result == status)
    trades = query.order_by(Trade.timestamp.desc()).limit(limit).all()

    def _compute_edge(t):
        if t.model_probability is None or t.market_price_at_entry is None:
            return None
        return abs(t.model_probability - t.market_price_at_entry)

    return [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            settlement_time=t.settlement_time,
            settlement_value=t.settlement_value,
            result=t.result,
            pnl=t.pnl,
            edge_at_entry=_compute_edge(t),
        )
        for t in trades
    ]


@app.get("/api/equity-curve")
async def get_equity_curve(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0
    bankroll = settings.INITIAL_BANKROLL

    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": bankroll + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post("/api/simulate-trade")
async def simulate_trade(
    signal_ticker: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.scheduler import log_event

    signals = await scan_for_signals()
    signal = next((s for s in signals if s.market.market_id == signal_ticker), None)

    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=500, detail="Bot state not initialized")

    entry_price = signal.market.up_price if signal.direction == "up" else signal.market.down_price

    trade = Trade(
        market_ticker=signal.market.market_id,
        platform="polymarket",
        event_slug=signal.market.slug,
        direction=signal.direction,
        entry_price=entry_price,
        size=min(signal.suggested_size, state.bankroll * 0.05),
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual BTC trade: {signal.direction.upper()} {signal.market.slug}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@app.post("/api/run-scan")
async def run_scan(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.scheduler import run_manual_scan, log_event

    state = db.query(BotState).first()
    if state:
        state.last_run = datetime.utcnow()
        db.commit()

    log_event("info", "Manual scan triggered (BTC + Weather)")
    await run_manual_scan()

    signals = await scan_for_signals()
    actionable = [s for s in signals if s.passes_threshold]

    result = {
        "status": "ok",
        "total_signals": len(signals),
        "actionable_signals": len(actionable),
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Also run weather scan if enabled
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import scan_for_weather_signals
            wx_signals = await scan_for_weather_signals()
            wx_actionable = [s for s in wx_signals if s.passes_threshold]
            result["weather_signals"] = len(wx_signals)
            result["weather_actionable"] = len(wx_actionable)
        except Exception:
            logger.exception("Manual scan: weather sub-scan failed")
            result["weather_signals"] = 0
            result["weather_actionable"] = 0

    return result


@app.post("/api/settle-trades")
async def settle_trades_endpoint(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
    from backend.core.scheduler import log_event

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled]
    }


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals."""
    total_signals = db.query(Signal).count()
    settled_signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    # Actual edge: for correct predictions, edge was real; for incorrect, edge was negative
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    # Brier score: mean squared error of probability forecasts
    # For each signal: (predicted_prob - actual_outcome)^2
    brier_sum = 0.0
    for s in settled_signals:
        # Model probability is for UP; actual is 1.0 if UP won, 0.0 if DOWN won
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


@app.get("/api/calibration")
async def get_calibration(db: Session = Depends(get_db)):
    """Return calibration data: predicted probability vs actual win rate."""
    signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not signals:
        return {"buckets": [], "summary": None}

    # Bucket signals by model_probability into 5% bins
    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        # Bin by 5% increments
        bin_start = int(s.model_probability * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += s.model_probability
        buckets_data[bucket_key]["total"] += 1
        if s.outcome_correct:
            buckets_data[bucket_key]["correct"] += 1

    buckets = []
    for bucket_key in sorted(buckets_data.keys()):
        d = buckets_data[bucket_key]
        buckets.append(CalibrationBucket(
            bucket=bucket_key,
            predicted_avg=d["predicted_sum"] / d["total"],
            actual_rate=d["correct"] / d["total"],
            count=d["total"],
        ))

    summary = _compute_calibration_summary(db)

    return {"buckets": buckets, "summary": summary}


# Kalshi endpoints
@app.get("/api/kalshi/status")
async def get_kalshi_status():
    """Test Kalshi API authentication and return connection status."""
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

    if not kalshi_credentials_present():
        return {
            "connected": False,
            "error": "Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)",
        }

    try:
        client = KalshiClient()
        balance_data = await client.get_balance()
        return {
            "connected": True,
            "balance": balance_data,
        }
    except Exception as e:
        logger.exception("GET /api/kalshi/status: balance probe failed")
        return {
            "connected": False,
            "error": str(e),
        }


# Weather endpoints
@app.get("/api/weather/forecasts", response_model=List[WeatherForecastResponse])
async def get_weather_forecasts():
    """Get ensemble forecasts for configured cities."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG
        from datetime import date

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        forecasts = []

        for city_key in city_keys:
            if city_key not in CITY_CONFIG:
                continue
            forecast = await fetch_ensemble_forecast(city_key)
            if forecast:
                forecasts.append(WeatherForecastResponse(
                    city_key=forecast.city_key,
                    city_name=forecast.city_name,
                    target_date=forecast.target_date.isoformat(),
                    mean_high=forecast.mean_high,
                    std_high=forecast.std_high,
                    mean_low=forecast.mean_low,
                    std_low=forecast.std_low,
                    num_members=forecast.num_members,
                    ensemble_agreement=forecast.ensemble_agreement,
                ))

        return forecasts
    except Exception:
        logger.exception("GET /api/weather/forecasts failed")
        return []


@app.get("/api/weather/markets", response_model=List[WeatherMarketResponse])
async def get_weather_markets():
    """Get active weather temperature markets."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather_markets import fetch_polymarket_weather_markets

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        markets = await fetch_polymarket_weather_markets(city_keys)

        # Also fetch Kalshi markets if enabled
        if settings.KALSHI_ENABLED:
            try:
                from backend.data.kalshi_client import kalshi_credentials_present
                from backend.data.kalshi_markets import fetch_kalshi_weather_markets
                if kalshi_credentials_present():
                    kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                    markets.extend(kalshi_markets)
            except Exception:
                logger.exception("Kalshi market fetch failed in /api/weather/markets")

        return [
            WeatherMarketResponse(
                slug=m.slug,
                market_id=m.market_id,
                platform=m.platform,
                title=m.title,
                city_key=m.city_key,
                city_name=m.city_name,
                target_date=m.target_date.isoformat(),
                threshold_f=m.threshold_f,
                metric=m.metric,
                direction=m.direction,
                yes_price=m.yes_price,
                no_price=m.no_price,
                volume=m.volume,
            )
            for m in markets
        ]
    except Exception:
        logger.exception("GET /api/weather/markets failed")
        return []


@app.get("/api/weather/signals", response_model=List[WeatherSignalResponse])
async def get_weather_signals():
    """Get current weather trading signals."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        return [_weather_signal_to_response(s) for s in signals]
    except Exception:
        logger.exception("GET /api/weather/signals failed")
        return []


def _weather_signal_to_response(s) -> WeatherSignalResponse:
    return WeatherSignalResponse(
        market_id=s.market.market_id,
        city_key=s.market.city_key,
        city_name=s.market.city_name,
        target_date=s.market.target_date.isoformat(),
        threshold_f=s.market.threshold_f,
        metric=s.market.metric,
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        ensemble_mean=s.ensemble_mean,
        ensemble_std=s.ensemble_std,
        ensemble_members=s.ensemble_members,
        actionable=s.passes_threshold,
    )


@app.get("/api/events", response_model=List[EventResponse])
async def get_events(limit: int = 50):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(limit)
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


# ── Live-pricing cache for open positions ──────────────────────────────
# Caches Gamma /markets/<id> lookups for 60s. Keyed by market_ticker.
# Each entry: (cached_at_ts, yes_price, no_price)
_PRICE_CACHE: dict = {}
_PRICE_CACHE_TTL = 60.0  # seconds


async def _get_market_prices(market_ticker: str) -> Optional[tuple]:
    """Return (yes_price, no_price) for an open position, cached 60s.

    Dispatches by ticker shape:
      - KX-prefixed -> Kalshi /markets/{ticker} (reads *_dollars decimal
        strings; HIGH-priority parity fix 2026-05-20 — until today this
        function only knew Polymarket, so every Kalshi position showed
        blank current_price / unrealized on the dashboard and the
        stop-loss job had no live mark to compare against).
      - otherwise   -> Polymarket Gamma /markets/<id> (numeric market_id),
        reads outcomePrices.

    Returns None on fetch error or unknown ticker shape.
    """
    import time as _t
    import httpx as _httpx

    now = _t.time()
    cached = _PRICE_CACHE.get(market_ticker)
    if cached and (now - cached[0]) < _PRICE_CACHE_TTL:
        return (cached[1], cached[2])

    # Kalshi branch
    if isinstance(market_ticker, str) and market_ticker.startswith("KX"):
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
        except Exception:
            logger.debug(f"Kalshi mark fetch failed for {market_ticker}", exc_info=True)
            return None

        def _pd(v):
            if v in (None, ""):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        # Ask side first (what we'd pay to enter today), with bid + last_price
        # fallbacks. Matches the entry-time parser in kalshi_markets.py so
        # mark and entry use the same notion of "fair current price".
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
        _PRICE_CACHE[market_ticker] = (now, yes_p, no_p)
        return (yes_p, no_p)

    # Polymarket branch (original behavior)
    url = f"https://gamma-api.polymarket.com/markets/{market_ticker}"
    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
        outcome_prices_raw = data.get("outcomePrices")
        if isinstance(outcome_prices_raw, str):
            # comes back as JSON-encoded string sometimes: '["0.625","0.375"]'
            import json as _json
            try:
                outcome_prices_raw = _json.loads(outcome_prices_raw)
            except (ValueError, TypeError):
                return None
        if not isinstance(outcome_prices_raw, list) or len(outcome_prices_raw) < 2:
            return None
        yes_p = float(outcome_prices_raw[0])
        no_p = float(outcome_prices_raw[1])
        _PRICE_CACHE[market_ticker] = (now, yes_p, no_p)
        return (yes_p, no_p)
    except (_httpx.RequestError, _httpx.HTTPError, ValueError, TypeError):
        return None


def _resolution_overdue_hours(ticker: str, platform: str) -> Optional[float]:
    """Hours past expected resolution for a weather ticker.

    Returns None if we can't parse the resolution date out of the ticker
    (e.g. legacy non-conforming ticker). Returns a positive float for
    markets past the expected end of their resolution day, negative for
    markets still in the future. Used by the dashboard to surface
    genuinely stuck pending positions (Polymarket / UMA reporting lag)
    without false-flagging same-day open positions.

    Added 2026-05-20 to make trades #7 and #9 (4 days stuck behind UMA)
    visible at a glance.
    """
    if not ticker:
        return None
    try:
        # Polymarket weather tickers: numeric market IDs -- can't infer date
        # from the ticker directly. Caller will fall back to None.
        if not ticker.startswith("KX"):
            return None
        # Kalshi tickers: KXHIGHNY-26MAY20-T96
        import re
        m = re.match(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-", ticker)
        if not m:
            return None
        MONTH = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                 "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        yy, mon, dd = int(m.group(1)), MONTH.get(m.group(2)), int(m.group(3))
        if mon is None:
            return None
        # Resolution typically reported a few hours after end-of-day UTC.
        # Anchor 23:59 UTC; consumers can decide what "overdue" means.
        from datetime import datetime as _dt
        resolution_anchor = _dt(2000 + yy, mon, dd, 23, 59)
        delta = _dt.utcnow() - resolution_anchor
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


@app.get("/api/positions-detail")
async def get_positions_detail(db: Session = Depends(get_db)):
    """Pending weather positions with current prices and unrealized P&L."""
    pending = db.query(Trade).filter(
        Trade.settled == False,
        Trade.market_type == "weather",
    ).order_by(Trade.timestamp.desc()).all()

    out = []
    for t in pending:
        # Share-purchase math (Polymarket binary): a `size` of $X buys X/p
        # shares at $1 face value, costing $X to enter. This matches what the
        # bot will actually realize at settlement once `calculate_pnl()` was
        # migrated on 2026-05-19 to mirror real Polymarket payouts (was a
        # fictional CFD model that under-counted P&L by a factor of 1/entry).
        shares = (t.size / t.entry_price) if (t.entry_price and t.entry_price > 0) else 0.0

        current_price = None
        unrealized_pnl = None
        unrealized_pct = None

        prices = await _get_market_prices(str(t.market_ticker)) if t.market_ticker else None
        if prices is not None:
            yes_p, no_p = prices
            # direction is "yes" or "no" — pick the matching outcome's price
            direction = (t.direction or "").lower()
            if direction == "yes":
                current_price = yes_p
            elif direction == "no":
                current_price = no_p
            if current_price is not None and t.entry_price and t.size:
                unrealized_pnl = shares * (current_price - t.entry_price)
                # % return on stake (capital at risk). Matches how a trader thinks
                # about ROI: +100% = doubled the stake, -100% = lost it all.
                unrealized_pct = unrealized_pnl / float(t.size)

        # Stuck-position surface (HIGH-priority follow-up 2026-05-20):
        # compute hours-past-resolution for Kalshi-style tickers so the
        # dashboard can flag positions waiting on a platform settlement.
        # severity buckets: "fresh" <0, "stale" 0-24h, "stuck" 24-72h,
        # "very_stuck" >72h. Polymarket tickers don't carry a parseable
        # date so they default to None and the UI leaves the badge off.
        overdue_h = _resolution_overdue_hours(str(t.market_ticker or ""), t.platform)
        if overdue_h is None:
            stuck_severity = None
        elif overdue_h < 0:
            stuck_severity = "fresh"
        elif overdue_h < 24:
            stuck_severity = "stale"
        elif overdue_h < 72:
            stuck_severity = "stuck"
        else:
            stuck_severity = "very_stuck"

        out.append({
            "id": t.id,
            "market_ticker": t.market_ticker,
            "platform": t.platform,
            "event_slug": t.event_slug,
            "direction": t.direction,
            "size_usd": t.size,
            "shares": shares,
            "entry_price": t.entry_price,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pct": unrealized_pct,
            "edge_at_entry": t.edge_at_entry,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            # Operational visibility — populated for tickers we can parse
            # a resolution date out of (Kalshi today; Polymarket can be
            # added later if we capture end_date at trade-entry time).
            "hours_overdue": overdue_h,
            "stuck_severity": stuck_severity,
        })
    return {"positions": out, "cache_ttl_s": _PRICE_CACHE_TTL}


@app.get("/api/snapshots")
async def get_pnl_snapshots(since: str = "24h", db: Session = Depends(get_db)):
    """Time series of weather-bot P&L snapshots for the dashboard chart.

    `since`: 24h, 7d, all
    Returns rows with: ts (unix), bankroll, exposure, realized_pnl, pending_count.
    """
    s = (since or "24h").lower().strip()
    if s == "all":
        cutoff = datetime(1970, 1, 1)
    elif s.endswith("h"):
        cutoff = datetime.utcnow() - timedelta(hours=int(s[:-1]))
    elif s.endswith("d"):
        cutoff = datetime.utcnow() - timedelta(days=int(s[:-1]))
    else:
        cutoff = datetime.utcnow() - timedelta(hours=24)

    rows = db.query(PnlSnapshot).filter(PnlSnapshot.timestamp >= cutoff).order_by(PnlSnapshot.timestamp).all()
    return {
        "snapshots": [
            {
                "ts": int(r.timestamp.replace(tzinfo=None).timestamp())
                      if hasattr(r.timestamp, "replace") else 0,
                "bankroll": r.bankroll,
                "exposure": r.exposure,
                "realized_pnl": r.realized_pnl,
                "pending_count": r.pending_count,
                "settled_count": r.settled_count,
                "is_running": bool(r.is_running),
            }
            for r in rows
        ],
        "since": since,
    }


# Bot control
@app.post("/api/bot/start")
async def start_bot(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Trading bot started")
    return {"status": "started", "is_running": True}


@app.post("/api/bot/stop")
async def stop_bot(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post("/api/bot/reset")
async def reset_bot(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth_token),
):
    from backend.core.scheduler import log_event

    try:
        trades_deleted = db.query(Trade).delete()
        state = db.query(BotState).first()
        if state:
            state.bankroll = settings.INITIAL_BANKROLL
            state.total_trades = 0
            state.winning_trades = 0
            state.total_pnl = 0.0
            state.is_running = True

        ai_logs_deleted = db.query(AILog).delete()
        db.commit()

        log_event("success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}")

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "ai_logs_deleted": ai_logs_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


@app.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    # Fetch BTC price from microstructure first, fallback to CoinGecko
    btc_price_data = None
    micro_data = None
    try:
        micro = await compute_btc_microstructure()
        if micro:
            micro_data = MicrostructureResponse(
                rsi=micro.rsi,
                momentum_1m=micro.momentum_1m,
                momentum_5m=micro.momentum_5m,
                momentum_15m=micro.momentum_15m,
                vwap_deviation=micro.vwap_deviation,
                sma_crossover=micro.sma_crossover,
                volatility=micro.volatility,
                price=micro.price,
                source=micro.source,
            )
            btc_price_data = BtcPriceResponse(
                price=micro.price,
                change_24h=micro.momentum_15m * 96,  # rough extrapolation
                change_7d=0,
                market_cap=0,
                volume_24h=0,
                last_updated=datetime.utcnow(),
            )
    except Exception:
        logger.exception("Dashboard: BTC microstructure fetch failed")
    if not btc_price_data:
        try:
            btc = await fetch_crypto_price("BTC")
            if btc:
                btc_price_data = BtcPriceResponse(
                    price=btc.current_price,
                    change_24h=btc.change_24h,
                    change_7d=btc.change_7d,
                    market_cap=btc.market_cap,
                    volume_24h=btc.volume_24h,
                    last_updated=btc.last_updated
                )
        except Exception:
            logger.exception("Dashboard: BTC fallback price fetch failed")

    # Fetch windows
    windows = []
    try:
        markets = await fetch_active_btc_markets()
        windows = [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        logger.exception("Dashboard: BTC windows fetch failed")

    # Signals — return ALL signals, mark which are actionable
    signals = []
    try:
        raw_signals = await scan_for_signals()
        signals = [_signal_to_response(s, actionable=s.passes_threshold) for s in raw_signals]
    except Exception:
        logger.exception("Dashboard: signals scan failed")

    # Recent trades
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
    recent_trades = [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl
        )
        for t in trades
    ]

    # Equity curve
    equity_trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    # Calibration summary
    calibration = _compute_calibration_summary(db)

    # Weather data (if enabled)
    weather_signals_data = []
    weather_forecasts_data = []
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import scan_for_weather_signals
            from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG

            wx_signals = await scan_for_weather_signals()
            weather_signals_data = [_weather_signal_to_response(s) for s in wx_signals]

            city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
            for city_key in city_keys:
                if city_key not in CITY_CONFIG:
                    continue
                forecast = await fetch_ensemble_forecast(city_key)
                if forecast:
                    weather_forecasts_data.append(WeatherForecastResponse(
                        city_key=forecast.city_key,
                        city_name=forecast.city_name,
                        target_date=forecast.target_date.isoformat(),
                        mean_high=forecast.mean_high,
                        std_high=forecast.std_high,
                        mean_low=forecast.mean_low,
                        std_low=forecast.std_low,
                        num_members=forecast.num_members,
                        ensemble_agreement=forecast.ensemble_agreement,
                    ))
        except Exception:
            logger.exception("Dashboard: weather signals/forecasts fetch failed")

    return DashboardData(
        stats=stats,
        btc_price=btc_price_data,
        microstructure=micro_data,
        windows=windows,
        active_signals=signals,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
        weather_signals=weather_signals_data,
        weather_forecasts=weather_forecasts_data,
    )


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to BTC trading bot"
        })

        from backend.core.scheduler import get_recent_events
        for event in get_recent_events(20):
            await websocket.send_json(event)

        # Audit 2026-05-19 HIGH #10: track by monotonic seq instead of
        # len-delta. The old approach re-pushed the entire 200-event buffer
        # every poll once the deque saturated (~50KB per client per 2s).
        recent = get_recent_events(200)
        last_seen_seq = recent[-1]["seq"] if recent else 0
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            new_events = [e for e in current_events if e.get("seq", 0) > last_seen_seq]
            for event in new_events:
                await websocket.send_json(event)
            if new_events:
                last_seen_seq = new_events[-1]["seq"]

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    # Audit 2026-05-19 CRITICAL #2: bind to settings.API_HOST (default
    # 127.0.0.1) so the mutating endpoints are not reachable from the LAN.
    # PORT env var still wins if set for Railway/Heroku-style deploys.
    bind_host = os.getenv("HOST", settings.API_HOST)
    bind_port = int(os.getenv("PORT", str(settings.API_PORT)))
    uvicorn.run(app, host=bind_host, port=bind_port)
