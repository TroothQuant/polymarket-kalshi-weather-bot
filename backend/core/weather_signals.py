"""Signal generator for weather temperature markets using ensemble forecasts."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from backend.config import settings
from backend.core.signals import calculate_edge, calculate_kelly_size
from backend.data.weather import fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG
from backend.data.weather_markets import WeatherMarket, fetch_polymarket_weather_markets
from backend.models.database import SessionLocal, Signal, BotState

logger = logging.getLogger("trading_bot")


@dataclass
class WeatherTradingSignal:
    """A trading signal for a weather temperature market."""
    market: WeatherMarket

    # Core signal data
    model_probability: float = 0.5   # Ensemble probability of YES outcome
    market_probability: float = 0.5  # Market's implied YES probability
    edge: float = 0.0
    direction: str = "yes"           # "yes" or "no"

    # Confidence and sizing
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    # Metadata
    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Forecast context
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_members: int = 0

    # Model-upgrade v1 SHADOW (2026-07-01): v2 = per-city/per-model bias-corrected +
    # equal-model-weight GFS/ECMWF pool. NULL when v2 couldn't be computed. Logged
    # alongside v1; v1 trades unless WEATHER_MODEL_V2_TRADING.
    model_probability_v2: Optional[float] = None
    ensemble_mean_v2: Optional[float] = None
    ensemble_std_v2: Optional[float] = None
    bias_applied_json: Optional[str] = None

    @property
    def passes_threshold(self) -> bool:
        """Check if signal passes the edge band [MIN, MAX] AND direction gate.

        Below MIN (default 0.25): edge isn't strong enough — losses pile up
        per the 10-25% cohort.
        Above MAX (default 0.50): model is wildly disagreeing with the market
        and the market is winning — see the 50%+ cohort (1 win in 12 trades).
        YES direction when WEATHER_DISABLE_YES_ENTRIES=True: blocked entirely
        pending NOMADS-backtest diagnosis of the YES/above failure mode.
        """
        if abs(self.edge) < settings.WEATHER_MIN_EDGE_THRESHOLD:
            return False
        if abs(self.edge) > settings.WEATHER_MAX_EDGE_THRESHOLD:
            return False
        if settings.WEATHER_DISABLE_YES_ENTRIES and self.direction == "yes":
            return False
        return True


def _model_yes_prob(forecast, market, bias: float = 0.0) -> float:
    """Map the market's question to the right probability_* call on a forecast-like
    object (raw, pre-clip). Shared by v1 (GFS EnsembleForecast) and v2 (PooledForecast)
    so both compute the SAME way on their respective member sets. Extracted verbatim
    from the v1 inline dispatch (2026-07-01).

    `bias` (default 0.0) shifts HIGH-temp thresholds to apply a per-station forecast
    bias on the v1 path: P(actual_high ≥ thr) = P(raw ≥ thr + bias). Only HIGH
    thresholds are shifted (the station bias is a daily-HIGH bias — same high-only
    scope as the v2 shadow's member correction). v2 passes 0 (it corrects members
    directly). bias=0 → byte-identical to the pre-2026-07-20 behavior."""
    strike_type = (getattr(market, "strike_type", None) or "").lower() or None
    is_kalshi = (getattr(market, "platform", "") == "kalshi")
    cap_shift = 1.0 if is_kalshi else 0.0
    floor_shift = 1.0 if is_kalshi else 0.0
    if strike_type and market.metric == "high":
        if strike_type == "between" and market.floor_strike is not None and market.cap_strike is not None:
            return forecast.probability_high_between(market.floor_strike + bias, market.cap_strike + cap_shift + bias)
        elif strike_type == "greater" and market.floor_strike is not None:
            return forecast.probability_high_above(market.floor_strike + floor_shift + bias)
        elif strike_type == "less" and market.cap_strike is not None:
            return forecast.probability_high_below(market.cap_strike + bias)
        else:
            if market.direction == "above":
                return forecast.probability_high_above(market.threshold_f + bias)
            return forecast.probability_high_below(market.threshold_f + bias)
    elif strike_type and market.metric == "low":
        if strike_type == "between" and market.floor_strike is not None and market.cap_strike is not None:
            return forecast.probability_low_between(market.floor_strike, market.cap_strike + cap_shift)
        elif strike_type == "greater" and market.floor_strike is not None:
            return forecast.probability_low_above(market.floor_strike + floor_shift)
        elif strike_type == "less" and market.cap_strike is not None:
            return forecast.probability_low_below(market.cap_strike)
        else:
            if market.direction == "above":
                return forecast.probability_low_above(market.threshold_f)
            return forecast.probability_low_below(market.threshold_f)
    else:
        if market.metric == "high":
            if market.direction == "above":
                return forecast.probability_high_above(market.threshold_f + bias)
            return forecast.probability_high_below(market.threshold_f + bias)
        else:
            if market.direction == "above":
                return forecast.probability_low_above(market.threshold_f)
            return forecast.probability_low_below(market.threshold_f)


async def _compute_v2_shadow(market):
    """Compute the v2 YES probability (bias-corrected, equal-model-weight GFS+ECMWF
    pool) + display stats + bias JSON. Returns (prob_clipped, mean, std, bias_json)
    or (None, None, None, None) if v2 can't be computed. A v2 failure NEVER affects
    v1 — the caller just logs NULLs."""
    try:
        from backend.data.weather import fetch_multimodel_forecast, PooledForecast
        from backend.core.model_bias import get_bias_cached, MODELS
        import json as _json

        raw = await fetch_multimodel_forecast(market.city_key, market.target_date, list(MODELS))
        if not raw:
            return None, None, None, None
        highs, lows = raw["highs"], raw["lows"]
        present = [m for m in MODELS if highs.get(m)]
        if len(present) < 2:   # need a genuine multi-model pool, else it's just v1
            return None, None, None, None
        biases = {m: get_bias_cached(market.city_key, m) for m in present}
        # Bias table is HIGH-temp only → correct highs; leave lows uncorrected.
        corr_highs = {m: [h - biases[m] for h in highs[m]] for m in present}
        corr_lows = {m: list(lows.get(m, [])) for m in present}
        pooled = PooledForecast(market.city_key, getattr(market, "city_name", ""),
                                market.target_date, corr_highs, corr_lows)
        prob = max(0.05, min(0.95, _model_yes_prob(pooled, market)))
        mean_v2 = pooled.mean_high if market.metric == "high" else pooled.mean_low
        std_v2 = pooled.std_high if market.metric == "high" else pooled.std_low
        return prob, mean_v2, std_v2, _json.dumps(biases)
    except Exception as e:
        logger.debug(f"v2 shadow compute failed for {getattr(market, 'city_key', '?')}: {e}")
        return None, None, None, None


async def generate_weather_signal(
    market: WeatherMarket,
    current_bankroll: Optional[float] = None,
) -> Optional[WeatherTradingSignal]:
    """
    Generate a trading signal for a weather temperature market.

    current_bankroll (added 2026-05-21): pass the bot's current bankroll for
    Kelly sizing. Defaults to settings.INITIAL_BANKROLL when None, but the
    scan loop should always pass the live value so sizing adapts as the
    bot's capital changes. Previously hardcoded INITIAL_BANKROLL, meaning
    the bot would oversize when bankroll dropped (treating $5K bankroll as
    if it were $10K) and undersize when bankroll grew above initial.

    Uses ensemble forecast to estimate probability:
    - Count fraction of ensemble members above/below the threshold
    - Compare to market price to find edge
    - Size using Kelly criterion
    """
    forecast = await fetch_ensemble_forecast(market.city_key, market.target_date)
    if not forecast or not forecast.member_highs:
        return None

    # Calculate model probability based on market's actual question.
    #
    # Two semantics in play:
    #
    # 1. Kalshi bucket-aware path (2026-05-20). When the market loader
    #    populated strike_type/floor_strike/cap_strike, the market is asking
    #    one of three questions:
    #      "between"  -> P(high in [floor, cap))           narrow bucket
    #      "greater"  -> P(high > floor)                   upper tail
    #      "less"     -> P(high < cap)                     lower tail
    #    Mixing these up was the bug: the bot was computing P(high > X) on
    #    narrow-bucket markets and reporting fictional 88-94% edges.
    #
    # 2. Polymarket / legacy path (no strike_type). Markets are
    #    cumulative thresholds; use the existing direction + threshold_f
    #    fields exactly as before.
    # v1 probability, clipped. The dispatch is extracted to _model_yes_prob so the
    # v2 shadow reuses the EXACT same logic on its members. Gated stations (2026-
    # 07-20) apply their static METAR backtest bias on the v1 TRADING path via a
    # HIGH-threshold shift (STATION_BIAS_SEED_F); all other cities pass 0.0 →
    # unchanged uncorrected-GFS v1. (US cities' bias stays v2-shadow-only.)
    from backend.core.model_bias import STATION_BIAS_SEED_F
    _v1_bias = STATION_BIAS_SEED_F.get(market.city_key, 0.0)
    prob_v1 = max(0.05, min(0.95, _model_yes_prob(forecast, market, bias=_v1_bias)))

    # ── Model-upgrade v1 SHADOW (2026-07-01) ─────────────────────────────────
    # Compute v2 (bias-corrected, equal-model-weight GFS+ECMWF pool) ALONGSIDE v1.
    # v1 trades by default; v2 feeds edge/sizing only when WEATHER_MODEL_V2_TRADING.
    # A v2 failure logs NULLs and leaves the v1 signal completely intact.
    prob_v2 = mean_v2 = std_v2 = bias_json = None
    if settings.WEATHER_MODEL_V2_SHADOW:
        prob_v2, mean_v2, std_v2, bias_json = await _compute_v2_shadow(market)

    # Active probability driving edge/direction/sizing/reasoning below. With the
    # flag OFF (default) this is v1 → v1 behaviour is byte-identical, v2 is logged.
    if settings.WEATHER_MODEL_V2_TRADING and prob_v2 is not None:
        model_yes_prob = prob_v2
    else:
        model_yes_prob = prob_v1

    # Audit 2026-05-19 HIGH #15: use the implied midpoint probability for
    # edge math (matters on Kalshi where yes_ask + no_ask > 1). Falls back
    # to yes_price for Polymarket where outcomePrices already IS the implied
    # probability.
    market_yes_prob = market.implied_or_yes()

    # Use existing edge calculation (treats yes=up, no=down)
    edge, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
    direction = "yes" if direction_raw == "up" else "no"

    # Clipped-probability edge cap (added 2026-05-22). The model_yes_prob
    # clipping at line ~152 above bounds the raw probability into [0.05,
    # 0.95]. When the clip fires, the underlying ensemble probability is
    # somewhere in (0, 0.05] or [0.95, 1) — we don't know where. Treating
    # the post-clip value as exact inflates our confidence in the edge.
    # Cap |edge| at WEATHER_MAX_CLIPPED_EDGE in that regime.
    # See config.py for the full rationale + trade #12 reference.
    edge_was_clipped = False
    if model_yes_prob in (0.05, 0.95):
        cap = settings.WEATHER_MAX_CLIPPED_EDGE
        if abs(edge) > cap:
            edge_was_clipped = True
            edge = cap if edge > 0 else -cap

    # Entry price filter — applied symmetrically:
    #   too high (> WEATHER_MAX_ENTRY_PRICE): paying too much for asymmetric upside.
    #   too low  (< WEATHER_MIN_ENTRY_PRICE): long-tail bucket where the GFS
    #     ensemble's confident calls have been catastrophically wrong
    #     (lifetime: entry<$0.10 trades 0-W / 10-stop / 1-loss / 1-void,
    #     -$554 P&L). Added 2026-05-22 — see config.py.
    # Edge is zeroed (not the signal dropped) so the row still persists for
    # post-hoc calibration analysis.
    entry_price = market.yes_price if direction == "yes" else market.no_price
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        edge = 0.0
    elif entry_price < settings.WEATHER_MIN_ENTRY_PRICE:
        edge = 0.0

    # Confidence = ensemble agreement (how one-sided the members are)
    if market.metric == "high":
        members = forecast.member_highs
    else:
        members = forecast.member_lows

    above_count = sum(1 for m in members if m > market.threshold_f)
    agreement_frac = max(above_count, len(members) - above_count) / len(members)
    confidence = min(0.9, agreement_frac)

    # Kelly sizing — pass WEATHER_MAX_TRADE_SIZE so calculate_kelly_size
    # caps at the weather cap ($100) not the legacy BTC cap ($75). Without
    # this, every weather signal was being silently shrunk to $75 by the
    # hardcoded MAX_TRADE_SIZE check inside calculate_kelly_size (bug fix
    # 2026-05-21). Also use current_bankroll (caller-supplied) instead of
    # INITIAL_BANKROLL so sizing tracks actual capital.
    bankroll = current_bankroll if current_bankroll is not None else settings.INITIAL_BANKROLL
    suggested_size = calculate_kelly_size(
        edge=abs(edge),
        probability=model_yes_prob,
        market_price=market_yes_prob,
        direction=direction_raw,  # calculate_kelly_size expects "up"/"down"
        bankroll=bankroll,
        max_size=settings.WEATHER_MAX_TRADE_SIZE,
    )
    # Defense-in-depth cap (no-op if the inner cap already fired, but kept
    # so any future code paths around suggested_size respect the weather cap).
    suggested_size = min(suggested_size, settings.WEATHER_MAX_TRADE_SIZE)

    # Ensemble stats for display
    mean_val = forecast.mean_high if market.metric == "high" else forecast.mean_low
    std_val = forecast.std_high if market.metric == "high" else forecast.std_low

    # YES-side kill-switch (added 2026-05-27). Logged loudly so it's visible
    # in bot.log when the gate fires — the dashboard's reasoning text alone
    # is easy to miss.
    yes_blocked = settings.WEATHER_DISABLE_YES_ENTRIES and direction == "yes"
    if yes_blocked:
        logger.info(
            f"YES entry blocked by kill-switch: {market.city_name} "
            f"{market.metric} {market.direction} {market.threshold_f:.0f}F "
            f"on {market.target_date} (edge {edge:+.1%})"
        )

    # Build reasoning. ACTIONABLE only if the edge sits inside [MIN, MAX]
    # AND direction passes the kill-switch.
    conviction_z = abs(mean_val - market.threshold_f) / std_val if std_val > 0 else 0.0
    actionable = (
        abs(edge) >= settings.WEATHER_MIN_EDGE_THRESHOLD
        and abs(edge) <= settings.WEATHER_MAX_EDGE_THRESHOLD
        and not yes_blocked
        and conviction_z >= settings.WEATHER_MIN_CONVICTION_Z
    )
    filter_status = "ACTIONABLE" if actionable else "FILTERED"
    filter_notes = []
    if abs(edge) > settings.WEATHER_MAX_EDGE_THRESHOLD:
        filter_notes.append(
            f"edge {edge:+.1%} > {settings.WEATHER_MAX_EDGE_THRESHOLD:.0%} ceiling"
        )
    if yes_blocked:
        filter_notes.append("YES entry blocked by kill-switch")
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    if entry_price < settings.WEATHER_MIN_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} < {settings.WEATHER_MIN_ENTRY_PRICE:.0%} (long-tail)")
    if edge_was_clipped:
        filter_notes.append(f"edge capped @ {settings.WEATHER_MAX_CLIPPED_EDGE:.0%} (model clipped {model_yes_prob:.2f})")
    if conviction_z < settings.WEATHER_MIN_CONVICTION_Z:
        filter_notes.append(f"conviction z={conviction_z:.1f} < {settings.WEATHER_MIN_CONVICTION_Z:.1f} floor")
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    reasoning = (
        f"[{filter_status}]{filter_note} "
        f"{market.city_name} {market.metric} {market.direction} {market.threshold_f:.0f}F on {market.target_date} | "
        f"Ensemble: {mean_val:.1f}F +/- {std_val:.1f}F ({forecast.num_members} members) | "
        f"Model YES: {model_yes_prob:.0%} vs Market: {market_yes_prob:.0%} | "
        f"Edge: {edge:+.1%} -> {direction.upper()} @ {entry_price:.0%} | "
        f"Agreement: {agreement_frac:.0%}"
        f" | Conviction z: {conviction_z:.1f}"
    )

    return WeatherTradingSignal(
        market=market,
        model_probability=model_yes_prob,
        market_probability=market_yes_prob,
        edge=edge,
        direction=direction,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"open_meteo_ensemble_{forecast.num_members}m"],
        reasoning=reasoning,
        ensemble_mean=mean_val,
        ensemble_std=std_val,
        ensemble_members=forecast.num_members,
        model_probability_v2=prob_v2,
        ensemble_mean_v2=mean_v2,
        ensemble_std_v2=std_v2,
        bias_applied_json=bias_json,
    )


async def scan_for_weather_signals() -> List[WeatherTradingSignal]:
    """
    Scan weather markets and generate ensemble-based signals.
    """
    signals = []

    city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]

    logger.info("=" * 50)
    logger.info("WEATHER SCAN: Fetching temperature markets...")

    markets = []

    # Polymarket
    try:
        poly_markets = await fetch_polymarket_weather_markets(city_keys)
        markets.extend(poly_markets)
        logger.info(f"Polymarket: {len(poly_markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch Polymarket weather markets: {e}")

    # Kalshi
    if settings.KALSHI_ENABLED:
        try:
            from backend.data.kalshi_client import kalshi_credentials_present
            from backend.data.kalshi_markets import fetch_kalshi_weather_markets
            if kalshi_credentials_present():
                kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                markets.extend(kalshi_markets)
                logger.info(f"Kalshi: {len(kalshi_markets)} weather markets")
        except Exception as e:
            logger.error(f"Failed to fetch Kalshi weather markets: {e}")

    logger.info(f"Found {len(markets)} total weather temperature markets")

    # Fetch current bankroll once per scan so Kelly sizing tracks actual
    # capital, not the hardcoded INITIAL_BANKROLL constant (bug fix
    # 2026-05-21). Falls back to INITIAL_BANKROLL if the DB query fails.
    current_bankroll = settings.INITIAL_BANKROLL
    try:
        _db = SessionLocal()
        try:
            _state = _db.query(BotState).first()
            if _state and _state.bankroll is not None:
                current_bankroll = float(_state.bankroll)
        finally:
            _db.close()
    except Exception as e:
        logger.warning(f"Could not fetch live bankroll for sizing, using initial: {e}")

    for market in markets:
        try:
            signal = await generate_weather_signal(market, current_bankroll=current_bankroll)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.warning(f"Weather signal generation failed for {market.title}: {e}")

    # Sort by absolute edge
    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"WEATHER SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")
    # Always-visible funnel (2026-07-01): markets found → signals → actionable →
    # persisted. `persisted` counts non-zero-edge signals only (zero-edge signals,
    # e.g. entry-price-band longshots, are found-but-NOT-persisted by design) — so
    # "N signals but 0 rows in the DB" reads as expected, not a silent failure.
    logger.info(
        f"WEATHER FUNNEL: markets={len(markets)} signals={len(signals)} "
        f"actionable={len(actionable)} "
        f"persisted={sum(1 for s in signals if abs(s.edge) > 0)}"
    )

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.city_name}: {signal.market.metric} {signal.market.direction} "
                     f"{signal.market.threshold_f:.0f}F | Edge: {signal.edge:+.1%}")

    # Persist signals to DB
    _persist_weather_signals(signals)

    return signals


def _persist_weather_signals(signals: list):
    """Save weather signals to DB for calibration tracking."""
    to_save = [s for s in signals if abs(s.edge) > 0]
    if not to_save:
        return

    db = SessionLocal()
    try:
        for signal in to_save:
            # Audit 2026-05-19 HIGH #13: see signals.py:_persist_signals
            # for rationale. Same composite-dedup pattern.
            existing = db.query(Signal).filter(
                Signal.market_ticker == signal.market.market_id,
                Signal.direction == signal.direction,
                Signal.market_price == round(signal.market_probability, 2),
                Signal.timestamp >= signal.timestamp.replace(second=0, microsecond=0),
            ).first()
            if existing:
                continue

            db_signal = Signal(
                market_ticker=signal.market.market_id,
                platform=signal.market.platform,
                market_type="weather",
                timestamp=signal.timestamp,
                direction=signal.direction,
                model_probability=signal.model_probability,
                market_price=signal.market_probability,
                edge=signal.edge,
                confidence=signal.confidence,
                kelly_fraction=signal.kelly_fraction,
                suggested_size=signal.suggested_size,
                sources=signal.sources,
                reasoning=signal.reasoning,
                executed=False,
                # Model-upgrade v1 SHADOW columns (NULL when v2 unavailable).
                model_probability_v2=signal.model_probability_v2,
                ensemble_mean_v2=signal.ensemble_mean_v2,
                ensemble_std_v2=signal.ensemble_std_v2,
                bias_applied_json=signal.bias_applied_json,
            )
            db.add(db_signal)

        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist weather signals: {e}")
        db.rollback()
    finally:
        db.close()
