"""Configuration settings for the BTC 5-min trading bot."""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # ─── Weather LIVE execution (G2, weather-live-v1 branch) ──────────────────
    # MASTER kill-switch — ships False and STAYS False through G2. First live
    # order is G3 only (Jonathon's GO), after G0 clears n>=20 and the wallet is
    # funded. Nothing below this line is reachable while WEATHER_LIVE_TRADING is
    # False; the live trader is never even imported on the paper path.
    WEATHER_LIVE_TRADING: bool = False
    WEATHER_LIVE_MAX_TRADE_USD: float = 2.0        # hard per-trade $ cap at G3 smoke
    WEATHER_LIVE_DAILY_LOSS_STOP_USD: float = 10.0  # daily realized-loss kill-switch (live only)
    WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD: float = 10.0  # OI1 (2026-06-24): hard cap on summed OPEN live exposure; set <= funded wallet balance
    WEATHER_LIVE_CITIES: str = "nyc"                    # OI1: live path restricted to these cities (defense-in-depth on WEATHER_CITIES)
    CLOB_HOST: str = "https://clob.polymarket.com"
    POLYMARKET_CHAIN_ID: int = 137                  # Polygon
    POLYMARKET_SIGNATURE_TYPE: int = 0             # match the wallet type set at G1
    POLYMARKET_PRIVATE_KEY: Optional[str] = None    # from server secrets, mode 600
    POLYMARKET_FUNDER_ADDRESS: Optional[str] = None
    # optional pre-generated CLOB creds (else derived at trader init):
    POLYMARKET_API_SECRET: Optional[str] = None
    POLYMARKET_API_PASSPHRASE: Optional[str] = None

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    # KALSHI_ENABLED gates scanning + market discovery (we want to see
    # Kalshi signals even if we're not trading them yet).
    KALSHI_ENABLED: bool = True
    # KALSHI_TRADING_ENABLED gates *trade execution* on Kalshi. Default
    # False. The original kill-switch went in 2026-05-20 for bucket-semantics
    # (since fixed in 073216e) and truncation rounding (fixed in 419734a).
    #
    # Reconfirmed False 2026-05-22 after running
    # scripts/kalshi_eod_calibration_2026-05-20.py against the May 20 + 21
    # resolutions: model picked the winning bucket 0/10 cities across both
    # days. Three failure modes:
    #   1. Wrong tail direction — model placed 95% confidence on the opposite
    #      half of the distribution from where the actual high landed.
    #   2. Right region, wrong bucket — winning bucket appears in the model's
    #      top-3 but isn't the singular peak; bot bets NO against the peak,
    #      which is also the winner.
    #   3. Right bucket, clipped floor — winning bucket sits at the 0.05
    #      probability clip (line ~152 in weather_signals.py) while the
    #      model bets heavily against it.
    #
    # Gating fixes (from yesterday's Trade #1 deep dive, now data-supported):
    # ensemble std calibration factor, max-edge-under-uncertainty cap,
    # historical backtest harness. Re-run kalshi_eod_calibration after each
    # ships; flip this to True only when the model picks the winning bucket
    # across ≥ 7 of the 10-day rolling window. Scans and signal logging
    # continue regardless.
    KALSHI_TRADING_ENABLED: bool = False

    # AI API Keys
    GROQ_API_KEY: Optional[str] = None

    # AI Model Configuration
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # AI Feature Flags
    AI_LOG_ALL_CALLS: bool = True
    AI_DAILY_BUDGET_USD: float = 1.0

    # Bot settings - BTC 5-MIN TRADING
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    KELLY_FRACTION: float = 0.15  # Fractional Kelly

    # BTC 5-min specific settings
    # Disabled 2026-05-19 — Pydantic Settings v2 doesn't load .env reliably,
    # so the BTC scheduler ran regardless of env-var intent. The strategy was
    # never being used for real trading and the scans were polluting the
    # signals table (~7,000+ dead rows). Flip to True only after fixing the
    # env-loading issue.
    BTC_ENABLED: bool = False  # Master switch for the BTC 5-min strategy
    SCAN_INTERVAL_SECONDS: int = 60  # Scan every minute
    SETTLEMENT_INTERVAL_SECONDS: int = 120  # Check settlements every 2 min
    BTC_PRICE_SOURCE: str = "coinbase"
    MIN_EDGE_THRESHOLD: float = 0.02  # 2% edge required — these are 50/50 markets
    MAX_ENTRY_PRICE: float = 0.55  # Enter up to 55c
    MAX_TRADES_PER_WINDOW: int = 1
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Risk management
    DAILY_LOSS_LIMIT: float = 300.0
    MAX_TRADE_SIZE: float = 75.0
    MIN_TIME_REMAINING: int = 60  # Don't trade windows closing in < 60s
    MAX_TIME_REMAINING: int = 1800  # Trade windows up to 30min out

    # Indicator weights for composite signal (must sum to ~1.0)
    WEIGHT_RSI: float = 0.20
    WEIGHT_MOMENTUM: float = 0.35
    WEIGHT_VWAP: float = 0.20
    WEIGHT_SMA: float = 0.15
    WEIGHT_MARKET_SKEW: float = 0.10

    # Volume filter
    MIN_MARKET_VOLUME: float = 100.0  # Low volume for 5-min markets

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min
    # Cohort analysis 2026-05-27 (n=34 settled weather trades) showed the
    # 10-25% edge band is 6 trades, 1 win, -$402 P&L. Drops the entire
    # failing band. Working strategy is NO bets with 25-50% edge — see
    # diagnostic_pull_2026-05-27.csv in the Polymarket folder. Was 0.08.
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.25
    # Cohort analysis 2026-05-27 also showed the 50%+ edge band is 12 trades,
    # 1 win, -$390 P&L (8.3% win rate). Distinct from WEATHER_MAX_CLIPPED_EDGE
    # (which only fires when model_yes_prob clips at 0.05/0.95); this ceiling
    # fires regardless of clipping. The catastrophic regime — when the model
    # thinks the market is wildly mispricing, the model is usually wrong.
    WEATHER_MAX_EDGE_THRESHOLD: float = 0.50
    # YES-side kill-switch (added 2026-05-27). Per the same cohort analysis,
    # YES/above is 13 trades, 2 wins, 16.7% WR, -$411 P&L — 80% of total
    # lifetime drawdown lives in one cohort. NO bets work either way (60%
    # win rate). YES/below is mediocre. Temporarily refuse all YES entries
    # while we figure out the root cause (GFS warm bias vs bucket-direction
    # inversion vs calibration). Override in .env to True to enable; the
    # default keeps a fresh checkout matching tomorrow's intent.
    WEATHER_DISABLE_YES_ENTRIES: bool = False
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    # WEATHER_MIN_ENTRY_PRICE (added 2026-05-22): refuse to enter on either
    # side when the asked-side price is below this floor. Lifetime DB scan
    # at the time of introduction (n=25 settled weather trades):
    #   entry < 0.10: n=12, wins=0, losses=1, stops=10, void=1, P&L=-$554.09
    #   0.10-0.25:    n=2,  wins=0, losses=1, stops=1,  void=0, P&L=-$144.28
    #   0.25-0.50:    n=1,  wins=1, losses=0, stops=0,  void=0, P&L=+$86.29
    #   0.50-0.75:    n=10, wins=7, losses=0, stops=3,  void=0, P&L=+$311.40
    # Long-tail entries (< $0.10) have NEVER won — the GFS ensemble's
    # 95% probability concentration on long-tail buckets is calibration-
    # broken (see KALSHI_TRADING_ENABLED comment above). This filter applies
    # to BOTH platforms because the same model feeds both; 3 of the 14
    # lifetime stop-loss trades are Polymarket long-tails (#2, #6, #12),
    # not just Kalshi. The cap is per-direction: a $0.05 NO buy and a
    # $0.05 YES buy are equally suspect.
    WEATHER_MIN_ENTRY_PRICE: float = 0.10
    # WEATHER_MAX_CLIPPED_EDGE (added 2026-05-22): edge magnitude cap that
    # applies ONLY when the model probability has been clipped to the 0.05
    # floor or 0.95 ceiling in weather_signals.py (~line 152). When the
    # raw ensemble probability rounds to those bounds, the model has run
    # out of representational range — the 'true' probability could be
    # 0.001 or 0.04 (both clip to 0.05), and we don't know which. The raw
    # edge calc (|model_p - market_p|) attributes high confidence to that
    # delta when it shouldn't.
    #
    # The companion WEATHER_MIN_ENTRY_PRICE filter catches one expression
    # of this problem (we buy a clipped-floor long-tail at < $0.10), but
    # the at-the-money case is NOT covered by an entry-price floor:
    # e.g., model says 95% YES, market at 0.50, edge +0.45 — we'd buy
    # YES @ 0.50 with raw +45% edge, sized aggressively. Trade #12 in our
    # DB is exactly this pattern (Polymarket entry 0.500, model=0.950,
    # edge=+0.450, stopped −$42). With stop-loss now disabled, that loss
    # would have been the full stake.
    #
    # Cap at 0.25 by default. Above-cap signals still log as ACTIONABLE
    # for visibility but trade at the capped size.
    WEATHER_MAX_CLIPPED_EDGE: float = 0.25
    # Minimum forecast conviction to enter (added 2026-06-15). z = |ensemble_mean
    # - bucket_threshold| / ensemble_std. Validated on 500 independently-settled
    # offered markets: z<1.0 wins 41.6% (losing), z>=1.0 wins ~58-67%. Default 0.0
    # = no-op. Arm via .env (recommend 1.0).
    WEATHER_MIN_CONVICTION_Z: float = 0.0
    WEATHER_MAX_TRADE_SIZE: float = 100.0
    WEATHER_MAX_ALLOCATION_USD: float = 1500.0  # Max combined open weather exposure (was hardcoded $500 in scheduler; bumped 2026-05-19 after Kalshi expanded the universe 13x)
    # Cap on new weather positions opened per UTC day. Catches cross-scan accumulation patterns like the 2026-05-20 Kalshi pile-in (10 trades over 33 hours across ~9 scans). Polymarket normal-day rate is 1-3 so this is headroom, not a bite.
    WEATHER_MAX_NEW_POSITIONS_PER_DAY: int = 5
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver"

    # Weather stop-loss (added 2026-05-19)
    # Close a weather position early when its mark-to-market loss reaches
    # WEATHER_STOP_LOSS_FRACTION of the position's max-possible-loss.
    #
    # Max-possible-loss = entry_price * size  (this is the pnl if direction loses,
    # per backend.core.settlement.calculate_pnl). So fraction=0.50 means: close
    # when unrealized loss = 50% of that max, i.e. when the current mark has
    # moved halfway from entry toward the losing-side value.
    #
    # Concrete example: NO @ 0.17 size $75 → max loss $12.75. Stop triggers when
    # the NO mark drops to 0.085 (current unrealized = −$6.375).
    # Default flipped 2026-05-23 to match .env production override. The .env
    # has had WEATHER_STOP_LOSS_ENABLED=false since 2026-05-21 when the
    # backtest showed stops cost ~$2,160 EV vs saving ~$80. Empirically
    # validated three settled trades over: #13 +$74.55, #14 +$73.51
    # (2026-05-21 overnight), #29 +$163.10 (2026-05-23). All three would
    # have stopped under True. A fresh checkout no longer contradicts
    # the documented live behavior.
    WEATHER_STOP_LOSS_ENABLED: bool = False
    WEATHER_STOP_LOSS_FRACTION: float = 0.50
    WEATHER_STOP_LOSS_INTERVAL_SECONDS: int = 600  # check every 10 min

    # API / dashboard hardening (added 2026-05-20 per audit CRITICAL #2).
    # The FastAPI app used to bind 0.0.0.0 with CORS=* and no auth on the
    # mutating endpoints (/api/bot/reset, /api/bot/start|stop, /api/simulate-
    # trade, /api/run-scan, /api/settle-trades). A drive-by visit to any
    # malicious page while the dashboard tab was open could wipe the ledger.
    #
    # Defaults below are local-only safe:
    #   - API_HOST=127.0.0.1 blocks LAN reachability.
    #   - API_ALLOWED_ORIGINS restricts CORS to the local dashboard origins.
    #   - API_AUTH_TOKEN is optional; if set, mutating POSTs require
    #     `Authorization: Bearer <token>`. Leave unset for paper-only local
    #     work; set before any live or network-exposed run.
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000
    API_ALLOWED_ORIGINS: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:8001,"
        "http://127.0.0.1:8001,"
        "http://localhost:8000,"
        "http://127.0.0.1:8000"
    )
    API_AUTH_TOKEN: Optional[str] = None

    # Pydantic Settings v2 config (fixed 2026-05-19 — was using v1's `class
    # Config` syntax which v2 silently ignores; that's why `.env` had been
    # not loading and BTC scanned regardless of intent).
    # `env_file` is resolved relative to the project root (one level above
    # this file), so the launcher's CWD doesn't matter.
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        extra="ignore",
    )


settings = Settings()
