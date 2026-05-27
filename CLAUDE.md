# Polymarket project navigation

Before any work that touches files in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/`, read `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/NAVIGATION.md` first. It documents the folder structure, the file-naming convention (`<type>_<YYYY-MM-DD>.md`, no `NN_` prefix on dated files), and where new files belong. Skipping this step is the failure mode that creates duplicate-prefix and orphan-file drift.

---

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

**Note**: `WEATHER_STOP_LOSS_ENABLED` default in `config.py` flipped to `False` on 2026-05-23 (commit `e028f25`). Source default and `.env` override now match. The .env-only setup was the intended live behavior since 2026-05-21 backtest (stops cost $2,160 EV vs saving ~$80); empirical case is now 3 trades over: +$148 overnight 5/21 (#13, #14), +$163 on 5/23 (#29).

## Operational notes (added 2026-05-23)

Three commits shipped, no live-bot restart required (defaults-only change + new tracked file + docs):

- **`e028f25`** — `fix(config): flip WEATHER_STOP_LOSS_ENABLED default to False`. See note above.
- **`201b54b`** — `docs(claude.md): add 2026-05-22 operational notes section`. Yesterday's documentation paperwork.
- **`ab25d35`** — `feat(backtest): historical replay harness for weather signal generator`. ERA5 + climatology stand-in for the GFS ensemble, Brier + 5-bin calibration + std-inflation sweep (1.0 → 2.5). Polymarket-only first cut. **Data-source blocker**: archive-api.open-meteo.com is intermittently TCP-unreachable from consumer ISPs (Cowork sandbox returns 403; operator's home network connection-times-out). LIMITATIONS section in the module docstring documents this and points at the NOAA NOMADS swap as the next step.

**NOMADS swap discovery passed 2026-05-23** (probe took 3 min vs a 30-min cap). Full plan in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-05-23.md` under "NOMADS discovery probe — full result" and "Outstanding for a complete swap". Headline:

- URL: `https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.YYYYMMDD/HH/atmos/pgrb2sp25/gepNN.tHHz.pgrb2s.0p25.fXXX` — no auth.
- 31 members at 0.25°: `gec00` (control) + `gep01..gep30`.
- `.idx` byte-range trick fetches just the TMAX field (~423 KB vs ~15 MB full GRIB).
- `pip install cfgrib xarray numpy` into the existing `.venv` — no separate eccodes install needed.
- Decode via `xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})`, then `.sel(latitude=..., longitude=..., method="nearest")`.
- Cross-validated against the live bot's open-meteo ensemble (Chicago 5/22 18-24h window): NOMADS members 62.15-62.87°F vs live bot 62.0°F ± 1.3°F. Different source, same answer.
- Outstanding: wrap into `fetch_gefs_ensemble_hindcast(date, city, fcst_hour)`, daily-max via `f018 + f024 + f030` element-wise max, add `--source nomads|openmeteo` flag to the harness, run the full backtest, decide whether the queued `WEATHER_ENSEMBLE_STD_INFLATION` knob ships. Est. 2-4 hours focused.

## Operational notes (added 2026-05-27)

Cohort analysis + NOMADS hindcast backtest day. Four commits shipped (`1f61272`, `52f41c3`, `b8c6bb9`, `d4644f1`, `47ae05f`, `f15eaa8`, `7ae7c5a`) plus a config edit. Headline finding: **the model isn't broken on YES/above; the stop-loss policy was the entire problem, and that's already been off since 2026-05-21.**

### New filter knobs (live)

- **`WEATHER_MIN_EDGE_THRESHOLD = 0.25`** (was 0.08, commit `1f61272`). Drops the failing 10-25% edge band entirely (6 historical trades, 1 win, −$402). The 0.08 floor was letting marginal-edge entries through that didn't have enough conviction to overcome variance.
- **`WEATHER_MAX_EDGE_THRESHOLD = 0.50`** (new, commit `b8c6bb9`). Caps entries when the raw model-vs-market edge exceeds 50%. Distinct from `WEATHER_MAX_CLIPPED_EDGE = 0.25` which only fires when the model probability has been clipped at the 0.05/0.95 boundary. The new ceiling catches the at-the-money case (model 0.95, market 0.30, edge 0.65, no clipping). Historical 50%+ edge band was 12 trades, 1 win, −$390 — when the model thinks the market is wildly mispricing something, the model is usually wrong, not the market.
- **`WEATHER_DISABLE_YES_ENTRIES = true`** (new, commit `d4644f1`, set in `.env`). **TEMPORARY** — pending settlement of the 3 currently-open YES positions (Denver 5/27, Denver 5/28, Chicago 5/28). Re-decide whether to flip back to `false` once we have n=7 clean YES/above outcomes instead of n=4. The data we have today doesn't justify keeping YES off permanently; it justifies waiting for the next round of evidence before re-enabling.

### NOMADS hindcast adapter shipped (commits `47ae05f` + `f15eaa8`)

NOAA NOMADS prod (`nomads.ncep.noaa.gov`) keeps only ~2 days of GEFS data. The 5/23 discovery probe established that the URL pattern works for current dates but didn't test retention. Pivoted to AWS S3 `noaa-gefs-pds` bucket — same GRIB files, same naming convention (`gefs.YYYYMMDD/00/atmos/pgrb2sp25/gepNN.t00z.pgrb2s.0p25.fXXX`), multi-year retention, no auth.

Adapter at `scripts/nomads_gfs_hindcast.py`. Reusable hindcast tool. Backtest harness at `scripts/backtest_weather_harness_2026-05-22.py` now takes `--source nomads|openmeteo`, defaults to nomads.

### Backtest findings (`nomads_backtest_2026-05-27.md`)

Three hypotheses tested, three resolved as NO:

- **(a/b) GFS warm bias / model bias on YES/above?** NO. Per-cohort Brier: YES/above 0.0162 (n=4), NO/above 0.1150 (n=8), YES/below 0.0157 (n=3), NO/below 0.0025 (n=3). The high-confidence YES bin [0.8, 1.0] is 5/5 wins. Model is calibrated; the lifetime −$411 on YES/above came from stop-loss exits, not model error.
- **(c) GFS ensemble too narrow?** NO. Std-inflation sweep: Brier monotonically worsens from 1.0 (0.0577) → 2.5 (0.0673). **The queued `WEATHER_ENSEMBLE_STD_INFLATION` knob should NOT ship.** Drop from queue.
- **(d) Data source problem?** NO. Live bot (open-meteo) Brier 0.0750 vs NOMADS replay Brier 0.0577. Mean |bot_p − replay_p| = 0.076 across 18 trades. Sources broadly agree.

### Lifetime narrative correction

The weather bot's −$247 realized P&L is **fossil damage** from the pre-2026-05-21 stop-loss-enabled era. With stops disabled + the new edge band filters, the forward-looking expected value is positive, not negative. Every retrospective analysis of "the bot is broken" should be checked against the date of the loss — if it's pre-5/21, it's likely a stop-loss artifact, not a strategy or model failure.

### Cosmetic parser fixes (same session)

`diagnostic_pull.py` saved at the top of the Polymarket folder (reusable). Fixed (i) NYC city split between "New York" and "New York City" — normalized to canonical "New York City" via `CITY_ALIASES`; (ii) regex now handles bracketed filter-note prefixes like `[FILTERED] [YES entry blocked by kill-switch]` before parsing the city name.

## Today's open carryovers

Up-to-date status lives in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/` — look for the latest dated session log and daily briefing files (latest: `session_log_2026-05-23.md`, next-session prompt: `morning_prompt_2026-05-24.md`). As of EOD 2026-05-23: 6 weather positions open ($575/$1500 cap used), bankroll $9,187.42, total_pnl −$212.58 (+$88.10 on the day from trade #29 win), 37 trades / 9 wins. Three positions resolve 5/24 (#31, #32, #33); three resolve 5/25 (#34, #35, #36).
