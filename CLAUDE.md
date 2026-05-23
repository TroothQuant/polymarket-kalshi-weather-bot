# Operating Principles (READ FIRST — overrides everything else in this file)

These two rules govern every session. They override conflicting guidance below.

## 1. Communicate like Jonathon is a beginner

- Jonathon does not code. He does not understand terminal language, file paths, shell syntax, build tools, or developer jargon by default.
- Every response that asks him to do something must use **numbered steps**.
- Every command must sit inside its own copy-pasteable code block — never run two commands on one line joined by `&&` unless he asks for that.
- Every command must be preceded by one or two sentences in plain English explaining (a) what the command does and (b) which terminal tab he should run it in.
- Avoid jargon. If a technical term is unavoidable, define it in one sentence the first time it appears in the session. Examples of terms to define on first use: `PATH`, `port`, `process`, `commit`, `dry run`, `kill`, `source`, `rc file`, `WAL`, `mount`.
- Never write "just run X" or "as you know" or "obviously" — every instruction needs context.
- Default to showing commands one at a time with a confirmation step between them. Only batch when Jonathon explicitly says so.
- When something goes wrong, lead with a plain-English explanation of what happened before the fix. Don't paste raw error messages without translating them.

## 2. Operate autonomously

- Default to **acting and then reporting**. Do not ask for approval on routine operational decisions.
- Use best judgment informed by: the bot's documented edge strategy, the current portfolio state, today's research and briefing, and what is most likely to keep both bots **healthy and profitable**.

### Decisions to make WITHOUT asking
- Closing redundant or highly correlated positions to free capital
- Tuning per-cycle thresholds (Kelly fraction, position caps, category caps, stop-loss percentage, min edge)
- Applying code patches that don't change trading semantics (bug fixes, dedup logic, dashboard fixes, scheduler improvements)
- Restarting bots after a patch
- Choosing which of two similar positions to keep
- Picking which file location to write outputs to
- Deleting duplicate database rows (always with a backup written first)
- Picking the right format / chart settings / log verbosity

### Decisions that REQUIRE asking first
- Moving from paper trading to live trading (real money)
- Sending money or initiating any transfer
- Sending email on Jonathon's behalf to third parties
- Changing the bot's core strategy archetype (e.g. switching from edge-based to momentum-based)
- Killing an entire trading category permanently
- Irreversible deletion of source code, git history, or backups
- Any action with legal or financial implications beyond paper-trading tuning

### How to track progress
- Report what was done, not what is planned. Past tense.
- If intent or scope is ambiguous (rare), one targeted clarifying question at the start of the session is fine. Once scope is clear, execute without re-asking.

---

# Project Overview

Trooth Prediction Market Trading Bot — multi-strategy paper trading on Polymarket (and optionally Kalshi). See `README.md` for the full description.

Two strategies run in one process:

- **BTC 5-minute Up/Down** — scans Polymarket BTC short-window markets every 60 seconds. Composite signal from RSI / momentum / VWAP / SMA / market skew. Trades when edge > 2%.
- **Weather temperature** — scans daily-temperature markets in nyc / chicago / miami / los_angeles / denver every 5 minutes. Uses 31-member GFS ensemble forecasts from Open-Meteo. Trades when edge > 8%.

## Running

Backend launcher: `scripts/run_backend.sh` (uvicorn + FastAPI on port 8000, all jobs registered via APScheduler).

Frontend (React dashboard): runs out of `frontend/`, normally on port 5173 in dev. The paired Claude bot dashboard lives at `~/Projects/trooth-claude-bot/dashboard_server/dashboard.html` on port 8001 — that's the dashboard most often discussed in sessions.

## Where things live

| Thing                             | Path                                          |
|-----------------------------------|-----------------------------------------------|
| Settings (env-driven Pydantic)    | `backend/config.py`                           |
| SQLite database                   | `tradingbot.db` (project root)                |
| Schema (Trade, Signal, BotState…) | `backend/models/database.py`                  |
| Scheduler (the periodic jobs)     | `backend/core/scheduler.py`                   |
| Settlement logic                  | `backend/core/settlement.py`                  |
| Weather signal generator          | `backend/core/weather_signals.py`             |
| BTC signal generator              | `backend/core/signals.py`                     |
| One-off operational scripts       | `scripts/` (cleanup, diagnostic, force-settle)|
| DB backups                        | `data/backups/`                               |

## Operational notes (added 2026-05-19)

- **Per-ticker dedup is lifetime, not per-day.** `weather_scan_and_trade_job` only opens one trade per `market_ticker` ever. The old "per-UTC-day" filter caused duplicate entries across day boundaries (Denver 5/17 was opened 3×, LA 5/18 was opened in opposite directions). Don't relax this without rethinking the case.
- **Stop-loss job runs every 10 minutes.** Closes open weather positions when mark-to-market loss reaches `WEATHER_STOP_LOSS_FRACTION` (default 0.50) of max possible loss. Implementation: `close_weather_trades_at_stop_loss()` in `settlement.py`. Trade rows are marked `result='stop_loss'`, `settlement_value=None`.
- **Settlement parser uses strict 0.99 / 0.01 thresholds** on `outcomePrices[0]` to declare a NegRisk market resolved. If a market settles with prices like 0.97/0.03 it'll be missed. First check via `scripts/diagnose_settlement_2026-05-19.py` whether a stuck trade is actually unresolved on Polymarket or just below the threshold.
- **Port 8000** is held by the uvicorn server; if a previous run didn't shut down cleanly, the new run fails with `Address already in use`. Fix: `lsof -ti :8000 | xargs kill` then re-run the launcher.

## Payoff model — share-purchase, NOT CFD (migrated 2026-05-19)

`calculate_pnl` and related math (mark_to_market_loss, compute_stop_loss_threshold) all use the Polymarket binary share-purchase model:
- `shares = size / entry_price`
- `pnl = shares * (settlement_value_for_my_side - entry_price)`
- Bankroll deducts `size` on entry, adds back `size + pnl` on settlement.

Was a fictional CFD model before today that under-counted P&L by ~1/entry_price. If you see numbers diverging by 5-50x between the bot and the dashboard or Polymarket UI, check whether someone reverted this. Historical trades were backfilled by `scripts/migrate_to_share_model_2026-05-19.py`.

## Pydantic Settings v2 — `.env` IS loaded now

`backend/config.py` uses `model_config = SettingsConfigDict(env_file=..., extra="ignore")`. Was using v1's `class Config: env_file = ".env"` syntax which v2 silently ignored. If something behaves like a config knob isn't being respected from `.env`, first sanity-check the syntax in `config.py` is still v2.

`.env` lives at the project root and currently sets:
- `KALSHI_ENABLED=true`
- `KALSHI_API_KEY_ID=<uuid>`
- `KALSHI_PRIVATE_KEY_PATH=secrets/kalshi_private_key.pem`
- `BTC_ENABLED=false`

Adding a new env-controlled setting: add the field to `Settings` in `config.py` with a default, then it's automatically picked up from `.env` (or process env vars override).

## Kalshi is live (2026-05-19)

Second platform alongside Polymarket. Scanner returns ~60 Kalshi weather markets per cycle on top of 5-6 Polymarket events. Same GFS ensemble model evaluates both. Bot bets the same direction on whichever side has the larger edge per ticker.

- Private key: `secrets/kalshi_private_key.pem` (gitignored).
- Trade rows now correctly carry `platform="kalshi"` or `"polymarket"` (was hardcoded to polymarket before today's fix).
- `WEATHER_MAX_ALLOCATION_USD = 1500.0` (was hardcoded $500 — too tight after Kalshi tripled the opportunity set).

## Watch out for SIGTERM-ignoring zombie processes

The weather bot's uvicorn process and the Claude bot's main.py have BOTH been observed ignoring SIGTERM today. The "stop" command appeared to succeed, but the process kept running and overwrote state files on its heartbeat (clobbering close-out scripts within ~9 minutes).

Before declaring a bot "stopped", confirm with:
```
ps aux | grep -i "python.*main.py" | grep -v grep
```
If anything comes back, force-kill with `kill -9 <PID>`.

## Operational notes (added 2026-05-22)

Two defensive filters shipped today against actual lifetime loss patterns. Both live in `backend/config.py` + `backend/core/weather_signals.py`. Both apply to BOTH platforms (the same GFS model feeds both).

- **`WEATHER_MIN_ENTRY_PRICE = 0.10`** (commit `327541d`). Refuses to enter on either side when the asked-side price is below the floor. Lifetime DB scan at introduction: entries < $0.10 were n=12, **zero wins**, 10 stops, 1 loss, 1 void, −$554 P&L. Symmetric with the existing `WEATHER_MAX_ENTRY_PRICE = 0.70` cap. Zeros edge while preserving the signal row for post-hoc calibration. Catches the 0.05-clipped long-tail case.
- **`WEATHER_MAX_CLIPPED_EDGE = 0.25`** (commit `9074eec`). Caps `|edge|` when `model_yes_prob` clips at the 0.05 floor or 0.95 ceiling. Catches the at-the-money case the entry-price filter cannot see — trade #12 pattern: Polymarket entry 0.500, model=0.950, raw edge +0.450, stopped −$42. With stop-loss now disabled, that same trade would have eaten the full stake. Capped signals still log as ACTIONABLE with a `[edge capped @ 25% (model clipped 0.XX)]` filter note for visibility; Kelly sizing then uses the capped edge.

**Kalshi calibration finding 2026-05-22**: `scripts/kalshi_eod_calibration_2026-05-20.py` returned **0/10 cities** across May 20 + May 21 — model picked the winning bucket on NO city across EITHER day. Three failure modes: wrong tail direction, right region wrong bucket, right bucket clipped at 0.05. `KALSHI_TRADING_ENABLED` stays `false`. Re-enable gate: ≥ 7/10 cities in a rolling 10-day window AND ≥ 4/5 in the most recent 5 days.

**Daily Kalshi calibration scheduled** via Cowork's scheduled-tasks MCP. Cron `15 0 * * *` (local CDT = 05:15 UTC, 16 min after Kalshi finalizes resolutions at 04:59 UTC). Appends to `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/kalshi_calibration_history.csv`. Observation-only — does not flip flags or commit. Note: scheduled tasks only fire while the Claude desktop app is open; if closed at 00:15, runs on next launch.

**Backtest harness scaffolded** at `scripts/backtest_weather_harness_2026-05-22.py` (untracked, awaits operator review). Pulls settled Polymarket weather trades, re-fetches ERA5 reanalyzed daily max + 5-yr climatological std as GFS-ensemble stand-in (ERA5 is the only no-key historical source; GFS hindcast via NOAA NOMADS is the documented next step). Replays the model probability calc with configurable std inflation, outputs Brier + 5-bin calibration curve + 1.0 → 2.5 inflation sweep. 3-trade smoke succeeded end-to-end. First interesting datum: trade #7 ERA5 replay disagreed with the bot's GFS ensemble, and the bot's tighter ensemble called it right — hint that GFS may NOT be under-dispersive (opposite of yesterday's Trade #1 deep-dive hypothesis). n=1, not actionable; full-book replay is queued.

**Note**: `WEATHER_STOP_LOSS_ENABLED` default in `config.py` is `True` but `.env` overrides to `False`. The override is the intended live behavior (per the 2026-05-21 backtest finding: stops cost $2,160 EV vs saving ~$80). Validated again 2026-05-22 by +$148 overnight realized from trades #13 and #14, both of which would have stopped under the old policy. Consider flipping the `config.py` default to match.

## Today's open carryovers

Up-to-date status lives in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/` — look for the latest dated session log and daily briefing files (latest: `16_session_log_2026-05-22.md`). As of EOD 2026-05-22: 5 weather positions open ($450/$1500 cap used), bankroll $9,249.32, total_pnl −$300.68, 34 trades / 8 wins. Trade #29 (Chicago today NO above 66°F) resolves tonight; expected win +$163.
