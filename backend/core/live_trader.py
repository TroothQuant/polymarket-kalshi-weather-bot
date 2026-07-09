"""Weather bot LIVE execution via the Polymarket CLOB.

MIGRATED to Polymarket CLOB V2 + pUSD (2026-06-24, round 3). Uses
`py-clob-client-v2` (1.0.1). The V2 SDK signs orders against the V2 CTF Exchange
exchange_v2=0xE111180000d2663C0091e4f400237545B87B996B (EIP-712 domain version
"2", default) with collateral=pUSD 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB.
L1/L2 API auth domain stays "1" (unchanged). Addresses verified against the V2
SDK's baked config AND docs.polymarket.com/resources/contracts. The V1 SDK is
NOT backward-compatible (order_version_mismatch) — do not import it here.

HARD GUARDRAILS (G2, weather-live-v1):
  * This module is imported ONLY when `settings.WEATHER_LIVE_TRADING` is True.
    The paper path never touches it, so `py-clob-client-v2` is never imported on
    the paper path. The flag ships False and STAYS False through G2.
  * `py-clob-client-v2` imports are LAZY (inside methods) for the same reason.
  * `build_order_args` is a pure static method (no py-clob, no network, no
    signing) so order-construction can be unit-tested with zero dependencies —
    this is the dry-run path. NO order is posted anywhere in G2.
"""
import logging
from typing import Optional

log = logging.getLogger("trading_bot")

# CLOB MARKET-order rounding per tick size (mirrors py_clob_client_v2 ROUNDING_CONFIG).
# A marketable FAK BUY is rounded as a MARKET order: maker (USDC) is ALWAYS <= 2 dp,
# and the taker (shares) to the tick's amount precision; price rounds DOWN to tick.
# Building it as a LIMIT order instead (maker<=4dp / taker<=2dp) is what got every
# real order rejected 2026-07-03 ("invalid amounts, maker max 2 / taker max 4 dp").
_MK_ROUND = {"0.1": (1, 3), "0.01": (2, 4), "0.001": (3, 5), "0.0001": (4, 6)}


def _round_down(x: float, dp: int) -> float:
    """Floor to `dp` decimals — used on the SPEND side so cost never rounds UP."""
    from math import floor
    return floor(x * (10 ** dp)) / (10 ** dp)


class WeatherLiveTrader:
    """Real execution via Polymarket CLOB (py-clob-client). Instantiated only on
    the live path; constructing it requires py-clob-client + a funded wallet."""

    # FAK statuses that unambiguously mean "no fill" — safe to treat as a routine
    # 0-fill without an extra verification round-trip (audit 3b).
    _KILL_STATUSES = {"unmatched", "canceled", "cancelled", "killed", "expired", "rejected"}

    def __init__(self, cfg=None):
        if cfg is None:
            from backend.config import settings as cfg  # singleton
        self.cfg = cfg
        # Lazy import — paper path must never pull py-clob-client.
        from py_clob_client_v2.client import ClobClient

        self.client = ClobClient(
            cfg.CLOB_HOST,
            key=cfg.POLYMARKET_PRIVATE_KEY or None,
            chain_id=cfg.POLYMARKET_CHAIN_ID,
            signature_type=cfg.POLYMARKET_SIGNATURE_TYPE,
            funder=cfg.POLYMARKET_FUNDER_ADDRESS or None,
        )
        # Pre-generated CLOB creds only when ALL THREE are present: the existing
        # POLYMARKET_API_KEY (config block above the live block) plus the new
        # POLYMARKET_API_SECRET / _PASSPHRASE. Otherwise derive them on-chain from
        # the private key (a network call at init).
        if (cfg.POLYMARKET_API_KEY and cfg.POLYMARKET_API_SECRET
                and cfg.POLYMARKET_API_PASSPHRASE):
            from py_clob_client_v2.clob_types import ApiCreds
            self.client.set_api_creds(ApiCreds(
                api_key=cfg.POLYMARKET_API_KEY,
                api_secret=cfg.POLYMARKET_API_SECRET,
                api_passphrase=cfg.POLYMARKET_API_PASSPHRASE,
            ))
        else:
            self.client.set_api_creds(self.client.create_or_derive_api_key())
        log.info("Weather live CLOB client initialized")

    # ── pure construction logic (no deps, no network — the dry-run unit) ──────
    @staticmethod
    def build_order_args(token_id: str, size_usd: float, market_price: float,
                         tick_size: str = "0.01") -> dict:
        """Return the ROUNDED market-BUY spec WITHOUT signing or posting, using the
        CLOB's MARKET-order rounding for the token's tick (FAK-precision fix,
        2026-07-03): maker (USDC) <= 2 dp, taker (shares) <= the tick's amount
        precision, price rounded DOWN to tick. +2-tick taker aggression so the FAK
        BUY crosses the spread. Spend is round DOWN → the maker (cost) NEVER exceeds
        `size_usd` (the per-trade cap). The >=15-share CLOB minimum is re-checked
        AFTER rounding. Refuses a missing token_id (P0 guard: never guess)."""
        if not token_id:
            raise ValueError("live order requires a token_id (P0 guard) — refusing market")
        if size_usd <= 0:
            raise ValueError("live order requires size_usd > 0")
        price_dp, taker_dp = _MK_ROUND.get(str(tick_size), (2, 4))
        tick = float(tick_size)
        # +2-tick aggression, rounded DOWN to tick precision, clamped to (tick, 1-tick).
        price = _round_down(market_price + 2 * tick, price_dp)
        price = min(price, _round_down(1 - tick, price_dp))
        price = max(price, tick)
        if price <= 0:
            raise ValueError("live order computed a non-positive price — refusing")
        # maker = USDC spend, rounded DOWN to 2 dp so cost can never exceed the cap
        # AND never carries >2 decimals (the CLOB's market-buy maker limit).
        maker_usd = _round_down(float(size_usd), 2)
        # taker = shares = maker/price, rounded DOWN to the tick's amount precision.
        shares = _round_down(maker_usd / price, taker_dp)
        if shares < 15:
            # CLOB rejects orders under 15 shares; refuse CLEANLY (execute_buy's try
            # converts this to a no-fill → the scheduler writes NO row). AFTER rounding
            # per the 2026-07-03 fix (audit 5c origin).
            raise ValueError(
                f"live order {shares} shares < 15-share CLOB minimum "
                f"(size_usd={size_usd} @ price={price}) — refusing")
        return {"token_id": str(token_id), "price": price, "size": shares,
                "amount_usd": maker_usd, "side": "BUY"}

    @staticmethod
    def build_limit_order_args(token_id: str, size_usd: float, limit_price: float,
                              tick_size: str = "0.01") -> dict:
        """Pure GTC-LIMIT BUY spec for the AGGRESSIVE-HYBRID path (v2, 2026-07-09).

        `limit_price` = the MOST we will ever pay (model_p_for_our_side - min-edge
        floor). Price rounds DOWN to tick so we never bid above limit_price; `size`
        = shares = floor(size_usd / price) to the tick's amount precision. A GTC
        limit priced AT/above the book SWEEPS every ask <= limit_price immediately
        (the aggressive TAKE, partial fills kept) and RESTS the remainder at
        limit_price (top of book, first for the next seller). The >=15-share CLOB
        minimum is re-checked AFTER rounding (the guard the caller also pre-checks
        via 15*limit_price > per-trade cap). Refuses a missing token (P0 guard)."""
        if not token_id:
            raise ValueError("live limit order requires a token_id (P0 guard) — refusing")
        if size_usd <= 0:
            raise ValueError("live limit order requires size_usd > 0")
        if limit_price <= 0:
            raise ValueError("live limit order requires limit_price > 0")
        price_dp, taker_dp = _MK_ROUND.get(str(tick_size), (2, 4))
        tick = float(tick_size)
        price = _round_down(float(limit_price), price_dp)
        price = min(price, _round_down(1 - tick, price_dp))
        price = max(price, tick)
        shares = _round_down(float(size_usd) / price, taker_dp)
        if shares < 15:
            raise ValueError(
                f"live limit order {shares} shares < 15-share CLOB minimum "
                f"(size_usd={size_usd} @ limit={price}) — refusing")
        return {"token_id": str(token_id), "price": price, "size": shares,
                "amount_usd": _round_down(shares * price, 2), "side": "BUY"}

    @staticmethod
    def _matched_shares(rec: dict) -> float:
        """AUTHORITATIVE filled-share count from an order record (get_order /
        get_open_orders). 0.0 if unknown/none — never guess a fill."""
        if not isinstance(rec, dict):
            return 0.0
        for k in ("size_matched", "sizeMatched", "matched_size"):
            v = rec.get(k)
            if v is not None:
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _safe_get_order(self, order_id) -> Optional[dict]:
        """get_order wrapped so an SDK/network error never raises into the caller."""
        try:
            rec = self.client.get_order(order_id)
            return rec if isinstance(rec, dict) else None
        except Exception as e:
            log.warning(f"Weather live get_order({order_id}) failed: {e}")
            return None

    def list_open_weather_orders(self) -> list:
        """Our resting (open) CLOB BUY orders — the source of truth for the
        cross-cycle resting-order lifecycle (survives restart; no local state).
        Returns [] on any failure (never raises)."""
        try:
            resp = self.client.get_open_orders()
        except Exception as e:
            log.warning(f"Weather live get_open_orders failed: {e}")
            return []
        orders = resp.get("data", resp) if isinstance(resp, dict) else resp
        out = []
        if isinstance(orders, list):
            for o in orders:
                if not isinstance(o, dict):
                    continue
                side = str(o.get("side", "")).upper()
                if side and side != "BUY":
                    continue
                out.append(o)
        return out

    def cancel(self, order_id) -> bool:
        """Cancel one resting order. True on success. Never raises."""
        try:
            self.client.cancel_order(order_id)
            log.info(f"Weather live cancelled resting order {order_id}")
            return True
        except Exception as e:
            log.warning(f"Weather live cancel_order({order_id}) failed: {e}")
            return False

    def execute_aggressive_hybrid(self, token_id: str, size_usd: float,
                                  limit_price: float) -> Optional[dict]:
        """AGGRESSIVE-HYBRID live BUY (v2, 2026-07-09) — replaces taker-FAK.

        Posts ONE marketable GTC LIMIT at `limit_price` (built by the SDK's
        create_order so LIMIT precision is correct — the 2026-07-03 hand-built
        rounding bug is avoided). The order SWEEPS all asks <= limit_price
        immediately (aggressive TAKE, partials kept) and RESTS any remainder at
        limit_price (top of book, first for the next seller). Polls briefly to
        capture the immediate take, then RETURNS while any remainder keeps resting
        (managed cross-cycle by the scheduler).

        Returns a lifecycle dict:
          {order_id, price, tick, filled_shares, filled_cost, fill_price,
           resting_shares, status} — filled_shares 0.0 means a pure rest (no row
           yet). None ONLY on a hard construction/post failure (caller skips)."""
        import time
        from py_clob_client_v2.clob_types import (
            OrderArgs, OrderType, PartialCreateOrderOptions)
        from py_clob_client_v2.order_builder.constants import BUY
        try:
            try:
                tick_size = str(self.client.get_tick_size(token_id))
            except Exception:
                tick_size = "0.01"
            args = self.build_limit_order_args(token_id, size_usd, limit_price, tick_size)
            log.info(
                f"Weather live HYBRID attempt {str(token_id)[:12]}… "
                f"${args['amount_usd']:.2f} ({args['size']} sh) @ <={args['price']} | "
                f"{self._orderbook_snapshot(token_id, args['price'])}")
            order_args = OrderArgs(token_id=args["token_id"], price=args["price"],
                                   size=args["size"], side=BUY)
            signed = self.client.create_order(
                order_args, options=PartialCreateOrderOptions(tick_size=tick_size))
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = (resp.get("orderID") or resp.get("id")) if isinstance(resp, dict) else None
            status = resp.get("status") if isinstance(resp, dict) else None
            log.info(f"Weather live CLOB GTC limit-BUY submitted: {order_id} "
                     f"(status={status}, {args['size']} sh @ <={args['price']}, tick={tick_size})")
        except Exception as e:
            log.error(f"Weather live HYBRID order failed: {e}")
            return None

        if order_id is None:
            log.error("Weather live HYBRID: no order id returned — treating as no-fill")
            return None

        # Poll briefly for the immediate TAKE (matched shares). The remainder rests.
        filled = 0.0
        for _ in range(3):
            time.sleep(1.5)
            rec = self._safe_get_order(order_id)
            m = self._matched_shares(rec)
            if m > filled:
                filled = m
            st = (rec or {}).get("status")
            if isinstance(st, str) and st.lower() in ("matched", "filled", "complete"):
                break
        price = args["price"]
        filled_cost = round(filled * price, 6) if filled > 0 else 0.0
        resting = max(0.0, args["size"] - filled)
        if filled > 0:
            log.info(f"Weather live HYBRID take: {filled:.2f} sh @ ~{price} "
                     f"(${filled_cost:.2f}); {resting:.2f} sh resting")
        else:
            log.info(f"Weather live HYBRID: 0 immediate take, {resting:.2f} sh resting @ {price}")
        return {"order_id": order_id, "price": price, "tick": tick_size,
                "filled_shares": filled, "filled_cost": filled_cost,
                "fill_price": price if filled > 0 else None,
                "resting_shares": resting, "status": status}

    def get_balance(self) -> Optional[float]:
        """Actual USDC collateral balance (atomic /1e6). None on failure."""
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(resp.get("balance", 0)) / 1_000_000.0
        except Exception as e:
            log.warning(f"Weather live balance check failed: {e}")
            return None

    @staticmethod
    def _parse_fill(resp: dict, order_id, size_usd: float = 0.0,
                    fallback_price: float = 0.0) -> Optional[dict]:
        """Resolve ACTUAL fill economics from a FAK order response.

        Under FAK (fill-and-kill) the response is AUTHORITATIVE: makingAmount =
        USDC actually paid, takingAmount = conditional tokens actually received.
        A killed / zero fill (taking<=0 or making<=0) returns None so the caller
        writes NO row. We deliberately do NOT estimate from size_usd/price — that
        GTC-era fallback would fabricate a phantom on a 0-fill now that this is
        called on every (incl. killed) response (audit E3, 2026-06-26).

        `fill_price` = making/taking (realized average); a stored row with
        size=cost, entry_price=fill_price recovers shares via size/entry_price.
        (size_usd / fallback_price kept for signature compatibility; unused.)
        """
        # A non-dict response (list/None/str) is a PARSE FAILURE, not a fill.
        # Guard first so `resp.get` can't raise AttributeError — which would escape
        # the (ValueError, TypeError) except below and propagate out of execute_buy
        # entirely (audit 3a, 2026-07-01).
        if not isinstance(resp, dict):
            return None
        try:
            making = float(resp.get("makingAmount") or 0)  # USDC paid
            taking = float(resp.get("takingAmount") or 0)  # conditional tokens received
        except (ValueError, TypeError):
            return None
        if taking <= 0 or making <= 0:
            return None
        return {"order_id": order_id, "fill_price": making / taking,
                "shares": taking, "cost": making}

    def _orderbook_snapshot(self, token_id, order_price) -> str:
        """OBSERVABILITY ONLY (no trading effect): best ask, size at best ask, and
        fillable ask depth at/below our order price — so a 'no match' FAK is
        diagnosable after the fact (empty book vs best-ask-above-us vs too-thin
        size). Asks are NOT ascending-sorted, so best ask = min price. Never
        raises; returns a one-line note (2026-07-06)."""
        try:
            ob = self.client.get_order_book(token_id)
            asks = (ob.get("asks") or []) if isinstance(ob, dict) else []
            levels = []
            for a in asks:
                try:
                    levels.append((float(a["price"]), float(a["size"])))
                except (KeyError, TypeError, ValueError):
                    pass
            if not levels:
                return "ob: NO ASKS (empty book)"
            best_p, best_s = min(levels, key=lambda x: x[0])
            fillable = sum(s for p, s in levels if p <= order_price)
            return (f"ob: best_ask={best_p:.3f} sz={best_s:.1f} | "
                    f"fillable<=px{order_price:.3f}={fillable:.1f}sh | n_asks={len(levels)}")
        except Exception as e:
            return f"ob: unavailable ({e})"

    def execute_buy(self, token_id: str, size_usd: float, market_price: float) -> Optional[dict]:
        """Post a FAK (fill-and-kill) BUY at the +2-tick taker price: it fills
        immediately against the book and KILLS any unfilled remainder — no
        resting order, no poll, no cancel, and no partial-fill PHANTOM (audit E3,
        switched from GTC poll-then-cancel 2026-06-26). The FAK response carries
        the ACTUAL makingAmount/takingAmount, so `_parse_fill` records the true
        fill, or None on a zero fill (killed) so the caller writes NO Trade row.

        FAK matches the existing +2-tick taker intent: we always wanted an
        immediate taker fill, never a resting order. A thin book fills LESS than
        requested but records it ACCURATELY (no phantom)."""
        from py_clob_client_v2.clob_types import (
            MarketOrderArgs, OrderType, PartialCreateOrderOptions)
        from py_clob_client_v2.order_builder.constants import BUY

        try:
            # Resolve the token's tick size (the SDK tolerates Number-or-String), then
            # build the tick-aware rounded spec + re-check the 15-share min BEFORE
            # posting. build_order_args is INSIDE the try so its guards convert to a
            # clean no-fill (→ caller writes no row).
            try:
                tick_size = str(self.client.get_tick_size(token_id))
            except Exception:
                tick_size = "0.01"
            args = self.build_order_args(token_id, size_usd, market_price, tick_size)
            # Observability (2026-07-06): snapshot the book we're about to cross, so a
            # 'no match' kill is diagnosable. Does NOT affect the order.
            log.info(f"Weather live FAK attempt {str(token_id)[:12]}… "
                     f"${args['amount_usd']:.2f} @ <={args['price']} | "
                     f"{self._orderbook_snapshot(token_id, args['price'])}")
            # A marketable FAK BUY MUST be built as a MARKET order (amount=USDC) so the
            # SDK rounds maker<=2dp / taker<=4dp to MATCH the CLOB. Building it as a
            # LIMIT order (size=shares) rounds the other way and the CLOB rejects it
            # ("invalid amounts") — the 2026-07-03 first-real-signal failure.
            order_args = MarketOrderArgs(
                token_id=args["token_id"], amount=args["amount_usd"],
                side=BUY, price=args["price"], order_type=OrderType.FAK)
            signed_order = self.client.create_market_order(
                order_args, options=PartialCreateOrderOptions(tick_size=tick_size))
            resp = self.client.post_order(signed_order, OrderType.FAK)
            order_id = (resp.get("orderID") or resp.get("id")) if isinstance(resp, dict) else None
            status = resp.get("status") if isinstance(resp, dict) else None
            log.info(f"Weather live CLOB FAK market-BUY submitted: {order_id} "
                     f"(status={status}, ${args['amount_usd']:.2f} @ <={args['price']}, tick={tick_size})")
        except Exception as e:
            log.error(f"Weather live CLOB order failed: {e}")
            return None

        # FAK is filled-or-killed at submit; the response is authoritative, so no
        # poll/cancel. A zero fill -> _parse_fill returns None -> caller writes no row.
        fill = self._parse_fill(resp, order_id, size_usd, args["price"])
        if fill is not None:
            log.info(f"Weather live fill: ${fill['cost']:.2f} "
                     f"({fill['shares']:.2f} sh @ {fill['fill_price']:.4f})")
            return fill

        # _parse_fill returned None. Treat it as a routine 0-fill ONLY when the
        # exchange EXPLICITLY says so (known kill status, or no order id at all).
        # Otherwise NEVER silently assume 0-fill on real money — verify once against
        # the order record; if we still can't confirm, log LOUDLY so a real fill can
        # never pass as a routine kill (audit 3b, 2026-07-01).
        status_l = status.lower() if isinstance(status, str) else ""
        if order_id is None or status_l in self._KILL_STATUSES:
            log.info(f"Weather live FAK order filled 0 shares (status={status}) — non-fill, no row")
            return None

        verified = self._verify_fill_via_lookup(order_id)
        if verified is not None:
            log.warning(
                f"Weather live FAK response was unparseable but the order lookup "
                f"CONFIRMED a fill: ${verified['cost']:.2f} "
                f"({verified['shares']:.2f} sh @ {verified['fill_price']:.4f})")
            return verified
        log.error(
            f"Weather live FAK order {order_id} (status={status}): UNVERIFIED non-fill — "
            f"parse failed AND the order lookup could not confirm a fill. NO row written; "
            f"CHECK THE WALLET for an untracked position.")
        return None

    def _verify_fill_via_lookup(self, order_id) -> Optional[dict]:
        """Best-effort: fetch the order record and build a fill ONLY from an
        AUTHORITATIVE positive matched size. Any uncertainty → None (never fabricate
        a position on real money). Wrapped so an SDK/network error can't raise into
        execute_buy."""
        try:
            rec = self.client.get_order(order_id)
        except Exception as e:
            log.error(f"Weather live order-lookup for {order_id} failed: {e}")
            return None
        if not isinstance(rec, dict):
            return None
        matched = None
        for k in ("size_matched", "sizeMatched", "matched_size"):
            if rec.get(k) is not None:
                try:
                    matched = float(rec.get(k))
                except (TypeError, ValueError):
                    matched = None
                break
        try:
            price = float(rec.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not matched or matched <= 0 or price <= 0:
            return None
        return {"order_id": order_id, "fill_price": price, "shares": matched,
                "cost": matched * price}

    def redeem_won(self, condition_id: str):
        """G2b SKELETON — claim winnings on a resolved-WON live position.

        The Python Claude bot never implemented redeem (auto-claim was .NET-only,
        per its CLAUDE.md), so there is no proven Python path to port here.
        Wiring the actual on-chain redeem + bankroll reconciliation is G2b
        (spec section 4). Until then this refuses loudly rather than silently
        no-op, so a live win can never be mis-accounted as claimed."""
        raise NotImplementedError(
            "redeem_won is a G2b item — on-chain claim not wired yet (weather-live-v1)")
