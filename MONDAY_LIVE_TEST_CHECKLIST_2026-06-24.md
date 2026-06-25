# weather-live-v1 — Monday NYC Live-Test GO/NO-GO Checklist (rev 3, 2026-06-24 — CLOB V2 + pUSD)

Engineering-only. **No flag flip, no real order.** Isolated worktree + venv + DB. Paper bot (`main`) verified untouched.

## 🔴 ROUND-3 CONTEXT — why this was mandatory
Polymarket cut over to **CLOB V2 + pUSD collateral on 2026-04-28**. The V1 SDK has **no backward compatibility** (`order_version_mismatch`). The bot's live path is now migrated to V2.

## ✅ V2 MIGRATION — DONE + VERIFIED
- **SDK swapped:** removed `py-clob-client` 0.34.6 (V1); installed **`py-clob-client-v2` 1.0.1** in the isolated venv. `requirements.txt` updated.
- **`WeatherLiveTrader` rebuilt for V2** — V2 client constructor; order signing via the V2 builder (**default `version=2` → V2 CTF Exchange + EIP-712 domain "2"**); collateral/balance → **pUSD** (`AssetType.COLLATERAL` now returns pUSD); creds `create_or_derive_api_key`; cancel `cancel_order`. L1/L2 auth domain stays "1" (SDK-internal).
- **F2-on-V2:** V2 `OrderArgs.size` = *"Size in terms of the ConditionalToken"* = **SHARES** (same as the round-1 fix). `MarketOrderArgs.amount` = USD. **15-share CLOB min unchanged under V2.** Cap stays $11.
- **V2 signing dry-run (throwaway key, NO post, no funds):** real EIP-712 signature produced; `makerAmount=11001000` (**$11.00 pUSD**, 6dp) for `takerAmount=19300000` (**19.3 shares**), implied price 0.5700 = order price ✓; signed against **V2 exchange `0xE111180000d2663C0091e4f400237545B87B996B`**, **pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`**.
- **Full suite: 31/31 pass** (V2 in venv; `paper_unchanged` + `test_conviction_gate` incl).
- **Conviction gate active** (z=1.0), **flag default-OFF**, **isolated DB** — all carried from round 2.

## 🔑 VERIFIED V2 CONTRACT ADDRESSES (Polygon, chain 137)
Confirmed by THREE independent sources (docs.polymarket.com/resources/contracts + the V2 SDK's baked config + the actual signed order):
| Contract | Address | Confidence |
|---|---|---|
| pUSD collateral | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | triple-confirmed |
| V2 CTF Exchange | `0xE111180000d2663C0091e4f400237545B87B996B` | triple-confirmed |
| V2 Neg-Risk Exchange | `0xe2222d279d744050d28e00520010520000310F59` | docs + SDK |
| Conditional Tokens | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | docs + SDK |
| **CollateralOnramp** | `0x93070a847efEf7F70739046A929D47a521F5B8ee` | **docs only — RE-VERIFY by eye before funding** |
| USDC.e (source) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | docs + SDK(V1) |

**⚠ Before ANY transaction, re-read each address directly on docs.polymarket.com. The CollateralOnramp (funding-critical) is from a single doc source — confirm it by eye.**

## 💵 VERIFIED V2 FUNDING FLOW (API/private-key route → pUSD collateral)
1. Get **USDC.e** (`0x2791…`) onto Polygon in the trading wallet (or use the bridge `POST /deposit` flow from any chain, which auto-wraps to pUSD — simplest).
2. **Approve** the CollateralOnramp on the USDC.e ERC-20: `approve(spender=0x93070a847efEf7F70739046A929D47a521F5B8ee, amount)`.
3. **Wrap**: `CollateralOnramp.wrap(_asset=0x2791…USDC.e, _to=<wallet>, _amount=<6-decimals>)` (e.g. 25 USDC.e = `25000000`). pUSD is minted to the wallet — that pUSD is the CLOB collateral.

## ⚠ REMAINING NOTES
- **The V2 order path has still never POSTED** (signing verified; the network post + fill-response parsing exercise first on Monday). `_parse_fill` keeps a robust fallback; confirm the real V2 fill-response keys (`makingAmount`/`takingAmount` vs V2 naming) on order 1.
- `redeem_won` still `NotImplementedError` (G2b) — won live position needs MANUAL claim.
- One full smoke loss (~$10.8) trips the $10 daily-loss stop (intended).

## Jonathon's Monday physical actions — FINAL ORDERED LIST
1. **Fund** wallet with **~$15–25 of USDC.e** on Polygon (covers one ~$8–11 min order + buffer).
2. **Wrap to pUSD** (approve CollateralOnramp → `wrap()`), per the funding flow above — or use the bridge deposit which auto-wraps.
3. **Secrets** → set `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER_ADDRESS` in `weather-live.env` (mode 600); set `WEATHER_LIVE_MAX_TOTAL_EXPOSURE_USD` = pUSD balance.
4. **Launch** (still paper): `set -a; source /home/trooth/.config/trooth/weather-live.env; set +a; cd /home/trooth/Projects/trooth-weather-live && .venv/bin/python run.py` — confirm boot shows flag False, z=1.0, isolated DB.
5. **GO**: set `WEATHER_LIVE_TRADING=true`, relaunch (Jonathon only).
6. **First-order verification (experiment):** one NYC NO signal → confirm fill in `weather_live.db` (order_id, ~15+ shares, size/price) AND on Polymarket UI → match continue, mismatch flip OFF + diagnose.

## RECOMMENDATION
The execution layer is **GO-ready** for an automated smoke test (CLOB-V2 + pUSD verified, strategy-faithful, isolated, tested). Not yet deployed (flag-OFF). Live testing is currently **operator-driven / manual**; the automated path stays parked + ready.
