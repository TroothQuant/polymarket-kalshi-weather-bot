# Weather Live-Execution Branch (`weather-live-v1`)

**Status: parked at end of G2. NOT merged to `main`. `WEATHER_LIVE_TRADING` ships
`False` and stays `False`.** The live weather service runs `main` and is paper-only.
First real order is **G3**, and only after the gates below + Jonathon's explicit GO.

This branch ports the Claude bot's soak-tested `LiveTrader` into the weather bot
behind a default-off flag (`backend/core/live_trader.py`), captures CLOB token IDs
on the gamma parse (P0/F3), and wires a flag-gated live branch into
`weather_scan_and_trade_job` (`resolve_weather_live`). All of it is inert while the
flag is off: `resolve_weather_live` returns a `"paper"` decision and the live
trader is never imported or constructed.

## The master switch
`WEATHER_LIVE_TRADING: bool = False` (`backend/config.py`). While false, the paper
path is byte-for-byte unchanged — the live trader (and `py-clob-client`) is never
imported. Live fires ONLY for `platform == "polymarket"` AND `market_type ==
"weather"` AND the flag on; Kalshi and non-weather can never reach it.

## Pre-merge / pre-arm gates

### MG1 — `order_id` column migration (BLOCKS MERGE)
The `Trade` model gained a nullable `order_id` column (the live-vs-paper marker;
the daily-loss kill-switch keys off `order_id IS NOT NULL`). The production
`trades` table does NOT have this column yet. **Before this branch merges to
`main` OR the flag is armed on the live DB**, run:
```sql
ALTER TABLE trades ADD COLUMN order_id VARCHAR;
```
Without it, paper inserts on the prod DB would reference a missing column and fail.

### OI1 — kill-switch is REALIZED-only (known limitation)
`_live_daily_realized_loss` sums `pnl` on **settled** live trades for the current
UTC day. Open-but-unsettled live losses are not counted until they settle. Fine at
the G3 $2 smoke scale; revisit the semantics (e.g. mark-to-market intraday) before
any real sizing-up (G5).

### F1 — settlement claim is manual at G3 (auto-claim is G4)
`WeatherLiveTrader.redeem_won()` is a skeleton that raises `NotImplementedError`.
The Python Claude bot never implemented redeem (auto-claim was .NET-only), so there
is no proven path to port. **At G3, winnings are claimed MANUALLY.** Wiring the
on-chain redeem + bankroll reconciliation is **G4**.

### F2 — execution path is UNPROVEN (verify before trusting size)
No order has ever been posted from this code. **G3 is the first real order and is
itself the execution test.** Before trusting size, verify the `py-clob-client`
`OrderArgs` amount semantics on the real API — i.e. whether `amount` is USD
notional or share count for a BUY — with a single minimal ($1 / 1-share) order and
reconcile the actual fill against `_parse_fill`'s `cost`/`shares`. `build_order_args`
currently passes `amount = size_usd` assuming USD notional; confirm that holds.

## Fill economics (settlement identity)
`execute_buy` returns `{order_id, fill_price, shares, cost}` where
`fill_price = cost / shares` (realized average, `shares > 0` guaranteed or the fill
is treated as a non-fill). The Trade row stores `size = cost`, `entry_price =
fill_price`, so settlement's `shares = size / entry_price` recovers the exact share
count. A row is written ONLY on a confirmed fill; a non-fill writes no row and
deducts no bankroll.

## Exact G3 arming steps (do in order; each is a deliberate checkpoint)
1. **G0**: paper sample clears **n ≥ 20** with the book still in profit.
2. **G1 (Jonathon)**: fund the live Polymarket wallet; place the private key in
   server secrets, `weather.env` mode 600.
3. **MG1**: run the `ALTER TABLE trades ADD COLUMN order_id VARCHAR;` migration on
   the live DB (and confirm a paper insert still succeeds).
4. **Secrets** in `weather.env` (mode 600): `POLYMARKET_PRIVATE_KEY`,
   `POLYMARKET_FUNDER_ADDRESS`, `POLYMARKET_SIGNATURE_TYPE` (match the funded
   wallet type), and optionally `POLYMARKET_API_KEY/_SECRET/_PASSPHRASE` (else
   derived at init).
5. **F2 check**: post ONE minimal real order out-of-band, confirm `OrderArgs`
   USD-vs-shares semantics and that `_parse_fill` reconciles the actual fill.
6. Set caps: `WEATHER_LIVE_MAX_TRADE_USD=2`, `WEATHER_LIVE_DAILY_LOSS_STOP_USD=10`.
7. Flip `WEATHER_LIVE_TRADING=true`; restart `trooth-weather-bot`.
8. Watch the first live fill land a Trade row with `order_id`; **manually claim**
   any win (F1) until G4 ships auto-claim.

## Tests (branch, mocked — no network, no real order)
`tests/test_weather_live_*.py`, run with the worktree-local pytest:
`PYTHONPATH=.testdeps <main-venv>/python -m pytest tests/`. Covers F3 label-mapping,
flag-off paper-unchanged, every `resolve_weather_live` branch (paper / skip / halt /
fill), the cap + kill-switch, the no-py-clob-on-paper-path guard, and the
`_parse_fill` settlement identity. `execute_buy` is never run live in tests.
