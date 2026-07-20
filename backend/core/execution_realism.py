"""Realistic-fill engine for the weather PAPER bot (2026-07-20, runbook #9).

Ported SHAPE from the sports bot's execution_realism.py (VWAP sweep over the
ask side), adapted for weather:
  * adds an ENTRY-PRICE CAP the sports version lacked — we fill ONLY against
    asks priced at/below the cap (the price the bot decided to pay), partial
    fills at the real ask sizes;
  * uses the weather TAKER FEE 0.05*p*(1-p)*shares (same as the live book), not
    the sports flat-bps model.

Gated by settings.WEATHER_REALISTIC_FILLS (paper server only). When OFF the
paper bot keeps its historical fantasy-fill (fill at the gamma outcomePrice,
zero slippage). When ON, a signal with no fillable ask <= cap produces NO trade
row (logged unfilled_no_liquidity).

The math functions are pure (no network) so the fill logic is unit-tested with
zero dependencies; fetch_book / resolve_token_id do the CLOB I/O.
"""
import logging
from typing import Optional

import httpx

log = logging.getLogger("trading_bot")

CLOB_HOST = "https://clob.polymarket.com"
_UA = {"User-Agent": "Mozilla/5.0"}


def taker_fee(price: float, shares: float) -> float:
    """Polymarket taker fee = 0.05 * price * (1-price) * shares (weather book)."""
    try:
        p = float(price)
        return 0.05 * p * (1.0 - p) * float(shares)
    except (TypeError, ValueError):
        return 0.0


def sweep_fill(asks, cap_price: float, desired_shares: float):
    """Sweep the ask side (any order) taking only levels priced <= cap_price, up
    to desired_shares. Returns (vwap, filled_shares); (0.0, 0.0) if nothing is
    fillable at/below the cap.

    asks: iterable of {'price','size'} dicts or (price, size) tuples; sizes are
    in shares. Levels are sorted ascending here so the cheapest fill first."""
    if desired_shares <= 0 or cap_price <= 0:
        return (0.0, 0.0)
    levels = []
    for lv in asks or []:
        if isinstance(lv, dict):
            p, s = float(lv.get("price", 0) or 0), float(lv.get("size", 0) or 0)
        else:
            p, s = float(lv[0]), float(lv[1])
        if p <= 0 or s <= 0:
            continue
        levels.append((p, s))
    levels.sort(key=lambda x: x[0])
    remaining = float(desired_shares)
    cost = filled = 0.0
    for p, s in levels:
        if p > cap_price + 1e-9:       # ENTRY CAP — never pay above the cap
            break
        take = min(remaining, s)
        cost += take * p
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0:
        return (0.0, 0.0)
    return (cost / filled, filled)


def realistic_fill(book: dict, cap_price: float, size_usd: float) -> Optional[dict]:
    """Fill `size_usd` against a real book, capped at `cap_price`. Returns a fill
    dict, or None when there is no fillable liquidity <= cap (the caller then
    writes NO trade row and logs unfilled_no_liquidity).

    desired shares = size_usd / cap_price (what we'd want if fully filled at the
    cap). The 5% taker fee is FOLDED INTO the effective entry price so the
    downstream share/PnL math (shares = size/entry) carries the fee in the cost
    basis, exactly like the live book — no schema change needed."""
    if cap_price <= 0 or size_usd <= 0:
        return None
    asks = (book or {}).get("asks", []) or []
    desired = size_usd / cap_price
    vwap, filled = sweep_fill(asks, cap_price, desired)
    if filled <= 0:
        return None
    fee = taker_fee(vwap, filled)
    # fee-inclusive effective entry: cost basis = notional + fee
    eff_entry = vwap + (fee / filled) if filled else vwap
    return {
        "fill_price": round(vwap, 6),
        "effective_entry_price": round(eff_entry, 6),
        "filled_shares": round(filled, 4),
        "notional": round(filled * vwap, 4),
        "fee": round(fee, 4),
        "cost": round(filled * vwap + fee, 4),   # bankroll debit
        "partial": filled < desired - 1e-9,
        "requested_shares": round(desired, 4),
    }


def fetch_book(token_id: str, client: Optional[httpx.Client] = None) -> dict:
    """CLOB order book for a token. Returns {'asks':[{price,size}], 'bids':[...]}
    (asks sorted ascending). Empty book on any failure (-> unfilled)."""
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    try:
        r = c.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, headers=_UA)
        if r.status_code != 200:
            return {"asks": [], "bids": []}
        b = r.json()
        asks = sorted(
            ({"price": float(a["price"]), "size": float(a["size"])}
             for a in (b.get("asks") or [])),
            key=lambda x: x["price"])
        bids = sorted(
            ({"price": float(x["price"]), "size": float(x["size"])}
             for x in (b.get("bids") or [])),
            key=lambda x: -x["price"])
        return {"asks": asks, "bids": bids}
    except Exception as e:
        log.warning(f"realistic-fill: book fetch failed for {token_id}: {e}")
        return {"asks": [], "bids": []}
    finally:
        if own:
            c.close()


def resolve_token_id(condition_id: str, direction: str,
                     client: Optional[httpx.Client] = None) -> Optional[str]:
    """Resolve the CLOB token_id for the trade side (direction 'yes'/'no') from
    the market's condition_id. gamma's event list does not populate clobTokenIds,
    so we read CLOB /markets/{condition_id} -> tokens[{outcome, token_id}]."""
    if not condition_id:
        return None
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    want = "yes" if str(direction).lower().startswith("y") else "no"
    try:
        r = c.get(f"{CLOB_HOST}/markets/{condition_id}", headers=_UA)
        if r.status_code != 200:
            return None
        for t in (r.json().get("tokens") or []):
            if str(t.get("outcome", "")).strip().lower() == want and t.get("token_id"):
                return str(t["token_id"])
        return None
    except Exception as e:
        log.warning(f"realistic-fill: token resolve failed for {condition_id}: {e}")
        return None
    finally:
        if own:
            c.close()
