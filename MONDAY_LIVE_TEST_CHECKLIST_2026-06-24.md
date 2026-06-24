# weather-live-v1 — Monday NYC Live-Test GO/NO-GO Checklist (2026-06-24)

Engineering-only prep done this session. **No flag flip, no real order, isolated worktree.**
Live paper bot untouched (read-only). All file edits backed up `*.bak_livetest_20260624`.

## ✅ VERIFIED (done + tested this session)
- **MG1** — isolated DB `/home/trooth/.local/state/trooth/weather_live.db` (copy of paper, paper UNTOUCHED), `order_id` column added + verified; idempotent migration script `scripts/migrate_add_order_id_2026-06-24.py`; pre-migrate backup kept.
- **OI1 caps** — daily realized-loss kill-switch CONFIRMED wired (`scheduler.py:_live_daily_realized_loss`); hard per-trade `$` cap wired; **NEW hard total-open-exposure cap** added (`_live_total_open_exposure`, set ≤ funded wallet). **6 cap unit-tests pass.**
- **F2** — order-path UNITS BUG FIXED (see GATING #1) + documented; dry-run builds a correctly-sized (shares) / correctly-priced (market+2-tick) order; cost=size×price matches intended `$`.
- **NYC restriction** — `WEATHER_CITIES=nyc` (scan) + live-path NYC guard `WEATHER_LIVE_CITIES=nyc` (non-NYC → paper, never live); tested.
- **Isolation** — `weather-live.env` (mode 600) with ABSOLUTE `DATABASE_URL` (no CWD hazard), caps, NYC, conviction var, `KALSHI_ENABLED=false`.
- **Full suite: 29/29 pass** (incl. `paper_unchanged` — paper path byte-for-byte unchanged).

## ⚠ GATING — resolve before a real order
1. **The order path was just fixed and has NEVER executed.** py-clob `OrderArgs` has no `amount` field (takes `size`=shares); both weather-live AND the "soak-tested" claude-bot port passed `amount=` → would `TypeError` on order 1. Fixed today, but the fix is **unexercised against real py-clob** → the first live order IS the test of the fix. Watch it like a hawk.
2. **$2 cap < CLOB ~5-share minimum** at NYC NO prices (~0.55 → 3.5 shares → REJECTED). → raise `WEATHER_LIVE_MAX_TRADE_USD` to ~**$3** (still tiny) OR confirm the market's real `min_order_size` from the live orderbook.
3. **weather-live-v1 is 8 commits behind `main`; the conviction-gate LOGIC is not in this branch.** Config var added but INERT (no `conviction_z` filter here). A live test now runs WITHOUT the conviction filter armed on the live paper bot = **strategy mismatch.** → for a strategy-faithful test, `git merge main` into weather-live-v1 (conflict-reviewed) to bring the gate + 7 refinements; for a pure plumbing smoke test, accept it's execution-mechanics-only.
4. **py-clob-client NOT installed in weather-live's runtime venv** (only the claude-bot venv has it). → install in the weather-live runtime env before any live run.
5. **`redeem_won` is `NotImplementedError` (G2b)** — a WON live position can't be auto-claimed yet. Fine for a 1-order smoke test (claim manually) — just know it.
6. **No systemd unit / runtime venv defined for weather-live** → decide how Monday runs: env-sourced manual run (`set -a; source weather-live.env; set +a; <venv>/bin/python run.py`) or a dedicated unit.

## Jonathon's physical actions (RESERVED — Claude never does these)
- [ ] Fund a small capped Polygon wallet with USDC.
- [ ] Put `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER_ADDRESS` in `weather-live.env` (stays mode 600).
- [ ] Set `WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD` = funded balance (or ≤).
- [ ] **Flip `WEATHER_LIVE_TRADING=true`** — the GO. (Claude decides readiness; Jonathon flips.)

## First-order verification (the ONE tiny order)
1. Flag on + cap ~$3 + wallet funded → wait for ONE NYC NO signal.
2. Confirm the single order in `weather_live.db`: `order_id` set; `size`(=cost) and `entry_price`(=fill price) imply shares = size/entry_price; values match intent.
3. Cross-check Polymarket UI: position exists, size + fill price match.
4. Match → continue. ANY mismatch → flip flag OFF immediately, diagnose. Do not place a 2nd order until order 1 reconciles.

## RECOMMENDATION
**Conditional GO for a 1-order PLUMBING smoke test** once GATING #2 (cap→~$3), #4 (install py-clob), and Jonathon's funding are done. This validates the just-fixed order path end-to-end at trivial size — which is exactly the point.
**NOT strategy-faithful** until GATING #3 (merge `main` for the conviction gate). Do the merge before ANY scaled/repeated live use.
The first order is the real test of the F2 fix — treat order 1 as the experiment, not a trade.
