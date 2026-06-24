"""Dry-run of the weather-live order path — NO order posted, no py-clob, no network.
Calls the real WeatherLiveTrader.build_order_args (the pure construction unit) for a
representative NYC NO entry, applies the live caps, and reports the exact order spec.
"""
import sys
sys.path.insert(0, "/home/trooth/Projects/trooth-weather-live")
from backend.core.live_trader import WeatherLiveTrader

MAX_TRADE_USD = 11.0          # WEATHER_LIVE_MAX_TRADE_USD (corrected for 15-share CLOB min)
MIN_SHARES = 15.0            # REAL Polymarket CLOB minimum_order_size (96%% of markets; was assumed 5)

# Representative NYC NO entry (0 live NYC weather markets in summer; this is a
# conviction-passing crossover-band NO fade like the live paper book takes).
token_no = "NYC_NO_TOKEN_EXAMPLE"
no_price = 0.55
requested_size = 50.0        # what Kelly might suggest

print("=== weather-live ORDER-PATH DRY-RUN (no post, no network, no py-clob) ===")
live_size = min(requested_size, MAX_TRADE_USD)
print(f"requested size ${requested_size:.2f}  →  per-trade cap clamps to ${live_size:.2f}")

spec = WeatherLiveTrader.build_order_args(token_no, size_usd=live_size, market_price=no_price)
cost = spec["size"] * spec["price"]
print(f"\nORDER SPEC (build_order_args):")
print(f"  token_id   : {spec['token_id']}")
print(f"  side       : {spec['side']}")
print(f"  price      : {spec['price']}   (market {no_price} + 2-tick taker aggression, cap 0.99)")
print(f"  size       : {spec['size']} SHARES   (= ${live_size:.2f} / {spec['price']})")
print(f"  amount_usd : ${spec['amount_usd']:.2f}   (intended spend)")
print(f"  implied cost = size*price = ${cost:.4f}  ✓ matches intended ${live_size:.2f}")
assert abs(cost - live_size) < 0.01, "cost must match intended USD"

print(f"\nMIN-ORDER CHECK (CLOB min ~{MIN_SHARES:.0f} shares):")
print(f"  {spec['size']} shares vs {MIN_SHARES:.0f}-share min → "
      f"{'OK' if spec['size'] >= MIN_SHARES else 'BELOW MIN — would be REJECTED by CLOB'}")
if spec['size'] < MIN_SHARES:
    need = round(MIN_SHARES * spec['price'], 2)
    print(f"  → at price {spec['price']}, clearing the {MIN_SHARES:.0f}-share min needs ~${need:.2f} "
          f"(raise WEATHER_LIVE_MAX_TRADE_USD to ~${need:.2f}+ for the smoke test, or confirm the "
          f"market's real min_order_size from the live orderbook).")

print(f"\nSAFETY: py_clob_client imported? {'py_clob_client' in sys.modules}  (must be False)")
print("No order posted. execute_buy (which signs+posts) was never called.")
