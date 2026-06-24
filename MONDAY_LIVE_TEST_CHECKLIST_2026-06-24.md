# weather-live-v1 — Monday NYC Live-Test GO/NO-GO Checklist (rev 2026-06-24, round 2)

Engineering-only. **No flag flip, no real order.** Isolated worktree `trooth-weather-live` (branch `weather-live-v1`),
isolated venv + DB. Live paper bot (`main`) verified untouched. Edits backed up `*.bak_livetest_20260624` / `*.bak_round2_20260624`.

## ✅ VERIFIED / DONE
- **Strategy-faithful merge** — `main`→`weather-live-v1` (commit `23aeb87`), every hunk reviewed. Only semantic issue (duplicate `WEATHER_MIN_CONVICTION_Z` from a clean textual auto-merge) caught + fixed. Brought the **conviction gate** + decay sensor + governance.
- **Conviction gate ACTIVE on the live path** — `weather_signals.py` filters `conviction_z < WEATHER_MIN_CONVICTION_Z`; env sets **z=1.0** (verified loaded). Conviction-failing signals never reach the live hook.
- **Master flag still default-OFF** — `WEATHER_LIVE_TRADING=False` (config default + env). Jonathon's physical GO only.
- **MG1** — isolated `weather_live.db` (`/home/trooth/.local/state/trooth/`), `order_id` added + verified; paper DB untouched.
- **OI1 caps** — daily realized-loss kill-switch wired; per-trade cap; **total-open-exposure cap** added. 6 cap unit-tests pass.
- **F2 order-path units FIX** — `OrderArgs(size=shares)` not `amount` (py-clob has no `amount` field). Confirmed against py-clob **0.34.6** signature.
- **py-clob-client INSTALLED** in the isolated weather-live venv (`0.34.6`, httpx `0.28.1` — resolved the `httpx==0.26.0` requirements conflict). Import + `OrderArgs.size` confirmed.
- **Real CLOB minimum confirmed = 15 shares** (96% of markets; the claude-bot's "5" was stale). Corrects round-1.
- **Cap set for the real min** — `WEATHER_LIVE_MAX_TRADE_USD = $11` (15 shares × ~0.72 NO-band top = $10.8; **$3–5 would be REJECTED**). `WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD = $25`.
- **Dry-run** — $11 → 19.3 shares @ price 0.57, clears the 15-share min; cost=size×price; no post, no py-clob in the dry-run.
- **Full suite: 31/31 pass** in the isolated venv (incl. `paper_unchanged` + merged `test_conviction_gate.py`).

## ⚠ REMAINING NOTES (not blockers, but know them)
1. **The order path was just fixed and has still never executed** (the bug existed in the "soak-tested" port too). The first live order IS the test of the fix — watch order 1 like an experiment, not a trade.
2. **One full smoke loss (~$10.8) trips the $10 daily-loss stop** — intended (halts further live opens). Fine for a 1-order test; bump `WEATHER_LIVE_DAILY_LOSS_STOP_USD` only if you want a 2nd attempt same day.
3. **`redeem_won` is `NotImplementedError` (G2b)** — a WON live position needs MANUAL claim. Fine for one order.
4. **httpx bumped 0.26→0.28** in the live venv (required by py-clob; matches the proven claude-bot). Tests pass; runtime httpx calls exercised first on Monday.
5. **No systemd unit yet** — Monday runs via env-sourced manual launch (below).

## Jonathon's Monday physical actions — FINAL ORDERED LIST
1. **Fund** a small Polygon wallet with USDC (≈ **$15–25**: covers one ~$8–11 min order + buffer).
2. **Secrets** → edit `/home/trooth/.config/trooth/weather-live.env` (stays mode 600): set `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER_ADDRESS`.
3. **Set** `WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD` = funded balance (or ≤).
4. **Launch** weather-live (env-sourced, isolated venv):
   `set -a; source /home/trooth/.config/trooth/weather-live.env; set +a; cd /home/trooth/Projects/trooth-weather-live && .venv/bin/python run.py`
   (confirm boot dump shows `WEATHER_LIVE_TRADING False`, conviction z=1.0, DB=weather_live.db — still paper at this point).
5. **GO**: set `WEATHER_LIVE_TRADING=true` in the env, relaunch. This is the live flip — **Jonathon only.**
6. **First-order verification (the experiment):** wait for ONE NYC conviction-passing NO signal →
   - confirm in `weather_live.db`: `order_id` set; `size`(cost) & `entry_price`(fill) imply shares = size/entry_price; ~15+ shares.
   - cross-check Polymarket UI: position, size, fill price match intent.
   - **Match → continue. ANY mismatch → set `WEATHER_LIVE_TRADING=false` immediately, diagnose. No 2nd order until order 1 reconciles.**

## RECOMMENDATION
**GO for a 1-order NYC smoke test** once Jonathon completes actions 1–3. The build is now strategy-faithful (conviction gate live), isolated, dependency-complete, cap-correct for the real CLOB minimum, and fully tested. Treat order 1 as the validation of the never-before-run order path.
