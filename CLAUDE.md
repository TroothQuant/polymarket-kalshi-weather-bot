# Polymarket project navigation

Before any work that touches files in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/`, read `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/NAVIGATION.md` first. It documents the folder structure, the file-naming convention (`<type>_<YYYY-MM-DD>.md`, no `NN_` prefix on dated files), and where new files belong. Skipping this step is the failure mode that creates duplicate-prefix and orphan-file drift.

---

# Operating Principles (READ FIRST — overrides everything else in this file)

These three rules govern every session. They override conflicting guidance below.

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

### Authority — Claude decides (updated 2026-06-15, supersedes the lists below)

Jonathon has delegated **full decision-making authority** to Claude. Claude makes ALL operational, strategic, risk, tuning, code, deployment-staging, and prioritization decisions for the bots. Claude does **not** park decisions, ask Jonathon to choose between options, or present "optional next steps" for sign-off. Claude determines what is best for the bots' health and profitability, **acts, and reports in past tense.** Recommendations are not floated for approval — they are decided and executed. (Server commands still go to Jonathon as paste-ready Claude Code prompts to *execute*, but the decision behind them is Claude's, already made.)

### Decisions Claude makes autonomously (non-exhaustive)
- Changing strategy/archetype, arming/disarming filters and kill-switches, enabling/disabling a trading category, position sizing and risk caps, retiring or reviving a bot.
- Tuning per-cycle thresholds (Kelly fraction, position/category caps, stop-loss, min edge, conviction floor).
- Code patches and refactors, dashboard/scheduler changes, restarts and deploys, committing + relay-pushing code — backups + tests first.
- Closing/keeping positions, deleting duplicate rows (backup first), DB/schema maintenance, file locations, formats, log verbosity, and choosing what to work on next.

### Reserved for Jonathon (the ONLY exception)
- **The physical act of going live with real money.** Claude decides WHEN the bot is ready and tells Jonathon "we look good to go live." Jonathon then performs that one go-live action. Claude never flips the bot to real-money trading, sends money, initiates a transfer, or emails third parties on Jonathon's behalf without Jonathon performing that specific physical action.

### Guardrails Claude still respects (not "asks," just doesn't do recklessly)
- No irreversible destruction of source code, git history, or backups without a written backup first.
- Real money, transfers, and third-party email remain Jonathon's physical action, on Claude's recommendation.

### How to track progress
- Report what was done, not what is planned. Past tense.
- If intent or scope is ambiguous (rare), one targeted clarifying question at the start of the session is fine. Once scope is clear, execute without re-asking.

## 3. Division of labor — Code writes, Cowork does not

- **Cowork (Claude Desktop) does NOT modify files** — no code, no scripts, no config, and no edits to the canonical record (session logs, NAVIGATION.md, CLAUDE.md). Cowork's job is to plan, research, decide, review, and draft exact content or paste-ready Claude Code prompts.
- **Claude Code is the single writer.** Code makes ALL code/script/config changes AND all writes to the canonical docs, then reports back. If Cowork has produced text for a doc, it hands that text to Code to write.
- **Rationale:** one writer prevents the duplicate-edit / drift failure mode the project has repeatedly hit, and keeps server-deployed code and its git history under a single hand. Cowork's leverage is judgment and drafting, not file edits.
- **Only exception:** if Jonathon explicitly asks Cowork in-session to write or edit a specific file, that one-off overrides this. Default is hands-off.

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

## Operational notes (added 2026-05-28)

Risk-control + documentation day. No trading-side changes — the bot was untouched on entries/exits; only filter/cap/observability code shipped. Two commits (`5681db2`, `1cdae91`). Headline: the lifetime loss is concentrated in **Kalshi** (−$465) and tail longshots, **not** the model or stop-loss. Full diagnosis in `weather_profitability_analysis_2026-05-28.md`.

- **Per-day position cap shipped (commit `5681db2`).** `WEATHER_MAX_NEW_POSITIONS_PER_DAY = 5`. Enforced in `backend/core/scheduler.py` with a pre-loop gate (returns early if today's count is already at/over the cap) and an in-loop break (stops opening once `positions_today + trades_executed` hits the cap). Counts **ALL** Trade rows from today (UTC), not just currently-open — deliberate, so a stop-out cascade can't free slots and refill (the 5/20 failure mode). Both platforms together, no platform filter.
- **Cross-scan vs single-scan correction.** The originally-proposed per-scan cap was rejected after timestamp analysis showed the 5/20 Kalshi pile-in was 12 trades across ~9 scans over ~33 hours, not a single-scan event. The existing hardcoded `MAX_TRADES_PER_SCAN = 3` (commit `e2856a0`) was working the whole time; a per-scan knob would have been a no-op against this failure mode. The full reconciliation between the 5/27 stop-loss narrative and the 5/28 platform/clamp narrative is in `weather_profitability_analysis_2026-05-28.md`.
- **Boot dump observability fix (commit `1cdae91`).** The FastAPI startup handler in `backend/api/main.py` now prints "Weather configuration:" and "Kalshi configuration:" blocks after the existing BTC-oriented "Configuration:" block — 11 weather fields + 2 Kalshi fields. Picks up on next natural restart; the live bot (PID 56099) is already running the correct config, so restarting twice just to surface the dump wasn't worth it.
- **Allocation cap clarification.** `WEATHER_MAX_ALLOCATION_USD = $1,500`, not $500. The 5/28 morning briefing misread the cap because the open book was 5 positions × $100 max-trade-size = $500, which coincidentally matched the pre-5/19 hardcoded value. Per-trade max-size and the total allocation cap are different settings.

## Operational notes (added 2026-06-01)

**As of 2026-06-01 ~20:27 UTC, the live weather bot runs on `trooth-prod-nyc3` (DigitalOcean droplet, NYC3), NOT on the Mac.** Mac repo + state preserved as fallback at `~/Projects/trooth-weather-bot/` (untouched) and `~/Projects/trooth-weather-bot/data/backups/pre_cloud_migration_2026-06-01/` (snapshots). Cloud-migration plan in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/cloud_migration_plan_2026-05-29.md`; session prompts + log at `code_prompt_cloud_migration_session{1,2}_2026-06-01.md` + `session_log_2026-06-01.md` in the same folder.

- **SSH into the server:** `ssh trooth-server` (alias, Tailnet-routed). The repo lives at `/home/trooth/Projects/trooth-weather-bot`. Cutover commit: `ae3ced4` (`origin/main` HEAD as of 2026-06-01).
- **Live unit:** `trooth-weather-bot.service` (enabled, active). `sudo systemctl status trooth-weather-bot` from the server, or `sudo journalctl -u trooth-weather-bot -f` to tail logs. ExecStart is `.venv/bin/python run.py`. The unit sets `RAILWAY_ENVIRONMENT=systemd-production` (defeats uvicorn auto-reload) and `PYTHONUNBUFFERED=1` (so `print()` calls in the boot dump flush immediately through systemd's stdout pipe). EnvironmentFile is `/home/trooth/.config/trooth/weather.env` (mode 600).
- **Kalshi daily calibration:** `trooth-kalshi-calibration.timer` fires at 05:15 UTC daily (16 min after Kalshi resolves at 04:59 UTC), invoking the calibration script for yesterday's UTC date. Writes to `/home/trooth/.local/state/trooth/kalshi_calibration_history.csv` (env-controlled by `KALSHI_CALIBRATION_CSV_PATH`). The Mac launchd job `com.trooth.kalshi-calibration` was unloaded but the plist remains at `~/Library/LaunchAgents/` as a break-glass fallback.
- **Rollback procedure (next 7 days):** `sudo systemctl disable --now trooth-weather-bot trooth-kalshi-calibration.timer` on the server, then `~/Projects/trooth-weather-bot/scripts/run_backend.sh` in a fresh Mac Tab 1, then `launchctl load ~/Library/LaunchAgents/com.trooth.kalshi-calibration.plist`. Mac DB is intact at the 2026-06-01 20:25:46 UTC quiescent state.
- **Server-side state path map:**

  | What | Where |
  |---|---|
  | Repo | `/home/trooth/Projects/trooth-weather-bot/` |
  | venv | `/home/trooth/Projects/trooth-weather-bot/.venv/` (Python 3.12.3) |
  | Live `tradingbot.db` | `/home/trooth/Projects/trooth-weather-bot/tradingbot.db` |
  | Env file | `/home/trooth/.config/trooth/weather.env` (mode 600) |
  | Kalshi PEM | `/home/trooth/.config/trooth/kalshi_private_key.pem` (mode 600) |
  | Calibration CSV | `/home/trooth/.local/state/trooth/kalshi_calibration_history.csv` |
  | Backups | `/home/trooth/.local/state/trooth/backups/` (empty; DO Spaces sync pending Session 4) |

**Three commits shipped to support the cutover** (already in `origin/main`):

- `016f408` — `feat(kalshi): self-sufficient CSV writer, replaces Cowork-parsed output` (so the unattended calibration run writes a CSV row without a Cowork agent parsing stdout). Soak-tested for 3 nights on Mac launchd before shipping.
- `ac5ec77` — `chore(deps): bump pandas pin 2.1.4 -> 3.0.3 to match live Mac venv`. The pinned 2.1.4 conflicted with `xarray==2026.4.0` (needs pandas≥2.2). The Mac venv had been running 3.0.3 since the NOMADS work — requirements just hadn't caught up.
- `ae3ced4` — `feat(kalshi): env-overridable CSV path for cross-host portability`. Reads `KALSHI_CALIBRATION_CSV_PATH` env, falls back to the Mac Desktop default. Lets the server write to a server-side path without needing a `/home/trooth/Desktop/` tree.

**Tech debt acknowledged in the unit file:** the `RAILWAY_ENVIRONMENT=systemd-production` repurpose is a side-channel — clean fix is a dedicated `WEATHER_BOT_RELOAD` env var read by `run.py:74`. Not blocking; tracked for a follow-up.

## Operational notes (added 2026-06-05)

Audit remediation (CRITICALs #4/#5 + HIGHs #8/#17/#21 on this bot). Shipped live with per-file backups (`*.bak_*_20260605`). No entry/sizing/exit-strategy change.

- **`scheduler.shutdown(wait=True)`** (was `wait=False`, `scheduler.py:~695`) — in-flight scan/settlement jobs finish before exit on stop (#4).
- **Allocation cap now re-checked in-loop (#5)** — new `weather_alloc_running` accumulator in `weather_scan_and_trade_job`; breaks once `weather_pending + running + trade_size > WEATHER_MAX_ALLOCATION_USD`. Mirrors the 5/28 per-day-cap structure; closes the burst-overshoot-by-$300 gap. Don't remove either the pre-loop early-return or the in-loop break — they're belt-and-suspenders.
- **SQLite WAL + busy_timeout=5000 (#8)** — connect-event listener in `backend/models/database.py`, gated on `sqlite` in the URL so it no-ops against a future Postgres. Fixes dashboard "database is locked" overlap.
- **APScheduler hardening (#17)** — all 5 `add_job` calls now carry `coalesce=True, misfire_grace_time=60` (collapses missed-tick bursts).
- **Kalshi finalized-but-empty (#21)** — `settlement.py` now logs a WARNING ("void-pending-review: <ticker>") instead of silently re-polling forever. Still returns no-settle; does not force-settle.
- **Deferred:** #9 (API auth token — needs coordinated FE `api.ts` + BE + frontend rebuild; endpoints are 127.0.0.1-only), #13 (stop-loss calibration backfill — moot while `WEATHER_STOP_LOSS_ENABLED=false`), #23 (dead 5% Kelly cap — would change low-bankroll sizing, left as-is), #12 (winning_trades KPI denominator — wants a careful pass).

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-05.md`.

## Operational notes (added 2026-06-08)

- **Kalshi calibration probe was broken-by-timing since the 6/1 server migration.** Kalshi now finalizes resolutions LATER than the documented 04:59 UTC, so the 05:15 UTC probe always saw all-pending and wrote `0/0` rows. Fixed 2026-06-07: backfilled 5/29–6/6 by re-running the probe per date (CSV upserts by date; backup `kalshi_calibration_history.csv.bak_20260607`), and added a **second `OnCalendar` at 22:00 UTC** to `trooth-kalshi-calibration.timer` (backup `.bak_20260607`) — the early run's empty row self-corrects. First 22:00 firing verified live.
- **Re-enable gate after rebuild: NOT CLOSE.** Most-recent-5 graded days 11/24 (46%), rolling-10 17/49 (35%) vs the ≥7/10 + ≥4/5 bar (70–80%). Trend is up — late May ~20% (chance ≈17%), June ~46%. `KALSHI_TRADING_ENABLED` stays false; flag untouched.
- **Deployed == version-controlled as of `65f2476`** (audit #4/#5/#8/#17/#21 patches committed retroactively + `.gitignore` rules for the `*.db-shm`/`*.db-wal` WAL sidecars). Server deploy key is read-only — push via Mac relay (`git fetch trooth-server:<repo> main`, then `git push origin FETCH_HEAD:main` from the Mac).

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-08.md`.

## Operational notes (added 2026-06-09)

Weather is now the **primary book** (the Claude bot was put on probation — see that repo's CLAUDE.md). Two backend changes shipped (view/KPI only, no trading change):

- **`/api/stats` now returns `weather_pnl_by_platform`** (`92ae6d3`) — a `GROUP BY platform` realized-P&L sum over settled `market_type='weather'` trades: `{polymarket, kalshi, total, polymarket_trades, kalshi_trades}`. Pure computed view; `total_pnl` unchanged. Live: **Polymarket +$448.95 (37) / Kalshi −$464.79 (12) / net −$15.84** — the blended −$15.84 was hiding a profitable Polymarket book dragged to breakeven by the dead Kalshi book. Kalshi −$464.79 is real model loss (why `KALSHI_TRADING_ENABLED=false`), not an error.
- **#12 win-rate KPI fix (`d1fc667`)** — `get_stats` derives `winning_trades` from the DB (`Trade.result=="win"`) so numerator + denominator are both DB-derived (kills `state.winning_trades` counter drift). KPI-only.
- **#9 (API auth)** — closed on the **already-present** incumbent `require_auth_token` (Bearer / `API_AUTH_TOKEN`, fail-open-when-unset, endpoints bind 127.0.0.1). A redundant `WEATHER_API_TOKEN` layer added by mistake was reverted. Intentionally inert — no public surface (no frontend served on the droplet; React runs Mac-local via SSH tunnel).
- **#7 (`WEATHER_DISABLE_YES_ENTRIES`) — decided KEEP `true`.** Clean YES sample now past n=7 and still loses (4W/6 honest losses, −$257 ex-stops); NO is the engine (+$766). Evidence-backed close.

**Weather-widening direction (analysis only, nothing built/changed in the live bot):**

- **Temperature coord lever is CLOSED.** NYC/Denver settlement-station re-analysis (the 5/29 carryover) → HOLD both at current coords (NYC 82%=82% tie vs KLGA; Denver current 100% > KBKF 80%). All 5 temp cities now validated well-sited. Doc: `nyc_denver_coord_validation_2026-06-09.md`.
- **Precipitation validated as the next book (gate PASSED, NOT yet built).** GEFS probability-of-rain calibration backtest, `scripts/precip_calibration_backtest_2026-06-09.py` (committed `11a3d86`; reuses `nomads_gfs_hindcast.py` + adds APCP — **GEFS APCP is 6h buckets, so a UTC day = f030+f036+f042+f048 of the D-1 00z run**). 78 city-days: **Brier 0.082 vs 0.249 climatology, BSS +0.67.** Tier-1 trade Atlanta/Boston/NYC (BSS +0.79–0.89); Tier-2 size-down Dallas/Denver (use ≥0.10″ — 0.01″ has trace-boundary noise); **exclude San Francisco (no skill, marine regime).** Doc: `precip_calibration_backtest_2026-06-09.md`. **Building the precip category needs operator GO** + a local-day accumulation window (the backtest used UTC day) + a rain settlement parser (NYC rain settles on **Central Park**, not the LGA temp station) + per-city tiering, then paper-confirm. Validated only at 24–48h lead.

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-09.md`.

## Operational notes (added 2026-06-10)

**Next book = temperature-book EXPANSION, now in paper validation (GATE 4).** Precip was shelved (Polymarket sells only thin monthly-cumulative precip buckets). The validated path is adding more of the SAME daily high-temp markets Polymarket already lists (~46 cities, 2–8¢ middle spreads). GEFS-NOMADS calibration "failed" but the REDO with the bot's actual open-meteo GFS source proved the tool was the confound (Dallas MAE 4.88→0.91°F); 5 cities cleared a control anchor (Dallas/Paris/London/Austin/BA), Atlanta/Seattle/Madrid/Sao Paulo are genuine non-edges.

- **Isolated paper sandbox — NOT wired into the live service.** Branch + worktree **`nextbook-paper`** at `/home/trooth/Projects/trooth-weather-nextbook` (live `main` worktree untouched). Harness `scripts/nextbook_paper_harness.py` + `nextbook_cities.json`. Writes ONLY to `~/.local/state/trooth/nextbook_paper.{db,ledger.csv}` — **never `tradingbot.db`**. Runs via `trooth-nextbook-paper.timer` (every 2h). Uses the main bot's `.venv` interpreter (read-only).
- **Active: Austin + Dallas** (settlement stations verified from market text: **Dallas = Love Field KDAL, not DFW**; Austin = Bergstrom). **London/Paris** config-gated `enabled:false` (flip on once the pre-peak filter is validated for their UTC windows). **Buenos Aires excluded** (Southern-Hemisphere winter model overconfidence). Hard per-city pre-peak entry filter; selection ranks by edge and honors the live caps (5/day, $1,500).
- **Pre-committed GO bar (review ~2026-07-01):** ≥15 settled/city + ≥25 total; within-1-bucket ≥73% (control floor) + no overconfidence; mean realized edge ≥ +0.05 AND aggregate paper P&L > 0; 100% pre-peak discipline. Documented in `weather_nextbook_calibration_2026-06-10.md`. **All four must hold → then Jonathon's live-GO to merge a city as a second book.** No live money yet.
- Docs: `weather_nextbook_discovery_2026-06-10.md`, `weather_nextbook_calibration_2026-06-10.md`. Branch `c2293ee` relay-pushed (not merged).

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-10.md`.

## Today's open carryovers

Up-to-date status lives in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/` — look for the latest dated session log and daily briefing files (latest: `session_log_2026-06-01.md`, this morning's briefing: `08_morning_briefing_2026-06-01.md`). As of cutover (2026-06-01 20:27 UTC): bankroll $9,193.72, realized P&L −$406.28, 49 trades, 4 open pending positions (#45 Polymarket 2391290, #46 Polymarket 2400011, #47 Polymarket 2407003, #48 Polymarket 2407026, all NO @ $100). Carryover: re-decide `WEATHER_DISABLE_YES_ENTRIES` once the YES/above sample grows from n=4 to n=7. Next session (3): migrate Claude bot + dashboard to the same droplet.
