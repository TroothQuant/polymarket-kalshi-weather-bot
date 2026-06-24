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
import time
from typing import Optional

log = logging.getLogger("trading_bot")


class WeatherLiveTrader:
    """Real execution via Polymarket CLOB (py-clob-client). Instantiated only on
    the live path; constructing it requires py-clob-client + a funded wallet."""

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
    def build_order_args(token_id: str, size_usd: float, market_price: float) -> dict:
        """Return the order spec WITHOUT signing or posting. 2-tick taker
        aggression (+0.02, capped 0.99) so the BUY crosses the spread and fills
        as a taker — same as the Claude bot. Refuses a missing token_id (P0
        guard: never guess)."""
        if not token_id:
            raise ValueError("live order requires a token_id (P0 guard) — refusing market")
        if size_usd <= 0:
            raise ValueError("live order requires size_usd > 0")
        price = min(round(market_price + 0.02, 2), 0.99)
        # py-clob OrderArgs.size is SHARES (conditional tokens), NOT USD, and there
        # is NO `amount` field — passing amount= raises TypeError (the latent bug
        # that would have crashed the FIRST live order; fixed 2026-06-24). To spend
        # ~size_usd at `price`, buy size_usd/price shares (cost = shares*price).
        shares = round(size_usd / price, 2)
        return {"token_id": str(token_id), "price": price, "size": shares,
                "amount_usd": float(size_usd), "side": "BUY"}

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
    def _parse_fill(resp: dict, order_id, size_usd: float, fallback_price: float) -> Optional[dict]:
        """Resolve ACTUAL fill economics from a CLOB order response.

        `fill_price` is the REALIZED AVERAGE (cost / shares), so a stored row with
        size=cost and entry_price=fill_price implies EXACTLY `shares` via the
        settlement identity shares = size / entry_price. Returns None when no
        shares filled, so the caller writes no row.
        """
        cost = float(size_usd)
        shares = size_usd / fallback_price if fallback_price > 0 else 0.0
        try:
            making = float(resp.get("makingAmount", 0))  # USDC paid
            taking = float(resp.get("takingAmount", 0))  # conditional tokens received
            if making > 0:
                cost = making
            if taking > 0:
                shares = taking
        except (ValueError, TypeError):
            pass
        if shares <= 0:
            return None
        return {"order_id": order_id, "fill_price": cost / shares,
                "shares": shares, "cost": cost}

    def execute_buy(self, token_id: str, size_usd: float, market_price: float) -> Optional[dict]:
        """Post a GTC BUY, poll 5×3s for MATCHED, cancel-if-unfilled. Returns
        {order_id, fill_price, shares, cost} on a confirmed fill, else None (so
        the caller writes NO Trade row on a non-fill)."""
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY

        args = self.build_order_args(token_id, size_usd, market_price)
        try:
            order_args = OrderArgs(token_id=args["token_id"], size=args["size"],
                                   price=args["price"], side=BUY)
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("id")
            log.info(f"Weather live CLOB GTC order submitted: {order_id}")
        except Exception as e:
            log.error(f"Weather live CLOB order failed: {e}")
            return None

        matched = False
        for attempt in range(5):
            time.sleep(3)
            try:
                info = self.client.get_order(order_id)
                status = info.get("status") if isinstance(info, dict) else None
                log.info(f"Weather live order poll {attempt+1}: status={status}")
                if status == "MATCHED":
                    matched = True
                    break
                if status in ("CANCELLED", "DELAYED"):
                    break
            except Exception as e:
                log.warning(f"Weather live order status check failed: {e}")
                break

        if not matched:
            log.warning(f"Weather live GTC order not filled after 15s, cancelling: {order_id}")
            try:
                self.client.cancel_order(order_id)
            except Exception as e:
                log.warning(f"Weather live cancel failed: {e}")
            return None

        fill = self._parse_fill(resp, order_id, size_usd, args["price"])
        if fill is None:
            log.warning("Weather live order MATCHED but reported zero shares — treating as non-fill")
            return None
        log.info(f"Weather live fill: ${fill['cost']:.2f} "
                 f"({fill['shares']:.2f} sh @ {fill['fill_price']:.4f})")
        return fill

    def redeem_won(self, condition_id: str):
        """G2b SKELETON — claim winnings on a resolved-WON live position.

        The Python Claude bot never implemented redeem (auto-claim was .NET-only,
        per its CLAUDE.md), so there is no proven Python path to port here.
        Wiring the actual on-chain redeem + bankroll reconciliation is G2b
        (spec section 4). Until then this refuses loudly rather than silently
        no-op, so a live win can never be mis-accounted as claimed."""
        raise NotImplementedError(
            "redeem_won is a G2b item — on-chain claim not wired yet (weather-live-v1)")
