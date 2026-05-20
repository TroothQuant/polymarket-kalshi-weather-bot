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

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    # KALSHI_ENABLED gates scanning + market discovery (we want to see
    # Kalshi signals even if we're not trading them yet).
    KALSHI_ENABLED: bool = True
    # KALSHI_TRADING_ENABLED gates *trade execution* on Kalshi. Default
    # False as of 2026-05-20 because the Kalshi platform integration is
    # not yet at full parity with Polymarket — bucket semantics need
    # re-verification before we let the bot place more entries. Scans
    # and signal logging continue regardless.
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
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8% — weather has more signal than 5-min BTC
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    WEATHER_MAX_TRADE_SIZE: float = 100.0
    WEATHER_MAX_ALLOCATION_USD: float = 1500.0  # Max combined open weather exposure (was hardcoded $500 in scheduler; bumped 2026-05-19 after Kalshi expanded the universe 13x)
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
    WEATHER_STOP_LOSS_ENABLED: bool = True
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
