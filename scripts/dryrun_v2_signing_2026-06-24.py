"""V2 SIGNING dry-run (v2) — uses a REAL active market's token_id so tick_size
resolves (read-only), signs locally with a THROWAWAY never-funded key, inspects
the signed V2 order. NO post_order, no funds."""
import sys, urllib.request, json
sys.path.insert(0, "/home/trooth/Projects/trooth-weather-live")
from backend.core.live_trader import WeatherLiveTrader

# 1. find a real active market token (any market; signing is market-agnostic)
req = urllib.request.Request("https://clob.polymarket.com/markets", headers={"User-Agent": "Mozilla/5.0"})
ms = json.load(urllib.request.urlopen(req, timeout=25))
ms = ms.get("data") if isinstance(ms, dict) else ms
tok = None
for m in ms:
    if m.get("closed"): continue
    for t in (m.get("tokens") or []):
        if t.get("token_id") and t["token_id"] != "0":
            tok = (t["token_id"], 0.55, m.get("question", "")[:40]); break
    if tok: break
token_id, price, q = tok
print(f"real market token: ...{token_id[-12:]}  price={price}  ({q})")

from eth_account import Account
acct = Account.create()
print("throwaway signer (never funded):", acct.address)

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
from py_clob_client_v2.order_builder.constants import BUY
client = ClobClient("https://clob.polymarket.com", chain_id=137,
                    key=acct.key.hex(), signature_type=0, funder=acct.address)

spec = WeatherLiveTrader.build_order_args(token_id, size_usd=11.0, market_price=price)
print(f"order spec: price={spec['price']} size={spec['size']} shares (=${spec['amount_usd']})")
oa = OrderArgs(token_id=token_id, price=spec["price"], size=spec["size"], side=BUY)
signed = client.create_order(oa, PartialCreateOrderOptions(neg_risk=False))

print("\n=== create_order SIGNED locally (no post) ===")
order = getattr(signed, "order", None) or (signed.get("order") if isinstance(signed, dict) else signed)
def g(k):
    return order.get(k) if isinstance(order, dict) else getattr(order, k, None)
mk = g("makerAmount"); tk = g("takerAmount"); sig = g("signature") or getattr(signed, "signature", None)
print("  side:", g("side"), "| signature present:", bool(sig))
print("  makerAmount (pUSD paid, 6dp):", mk, " takerAmount (shares, 6dp):", tk)
if mk and tk:
    md, td = float(mk)/1e6, float(tk)/1e6
    print(f"  => pays ~${md:.2f} for {td:.2f} shares (implied {md/td:.4f}; order price {spec['price']})")
    print("  UNITS OK (maker≈size*price, taker≈size):",
          abs(md - spec["size"]*spec["price"]) < 0.06 and abs(td - spec["size"]) < 0.06)
# confirm it signed against the V2 exchange (builder default version=2 → exchange_v2)
from py_clob_client_v2.config import get_contract_config
cc = get_contract_config(137)
print("  V2 exchange (signing target):", getattr(cc, "exchange_v2", None))
print("  pUSD collateral:", getattr(cc, "collateral", None))
print("\nNO post_order. No funds. Throwaway key.")
