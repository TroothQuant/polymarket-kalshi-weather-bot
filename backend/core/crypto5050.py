"""CRYPTO5050 paper book — Polymarket 5-min BTC Up/Down two-layer sim (2026-07-22).

PAPER ONLY. Flag-gated by CRYPTO_5050_ENABLED (default False). Runs as its own
asyncio task, fully outside the weather scheduler; every loop body is wrapped so
a crash here can NEVER touch the weather scan. Writes ONLY crypto_windows /
crypto_fills — never the trades table, never the weather ledger.

STRATEGY UNDER TEST (Cowork strategy-project findings):
  L1 (hedge): accumulate BOTH sides in small fills through the 5-min window,
      buying whichever side just got cheaper while keeping shares balanced, so
      the combined pair VWAP ends < $1.00. Reference: DoggyStyIe 7/22 11:45 ET —
      1,950 sh Up @ 0.61 + 1,950 sh Down @ 0.365 = 0.975/pair = +$48.61 net.
      Measured wallets run ~50% maker / ~50% taker; the maker half is where the
      locked spread comes from (all-taker locks pairs > $1.00 and bleeds).
  L2 (lean): fixed +20-share directional lean at/after window midpoint. LIVE
      rule = SPOT-DRIFT. Two more rules are SHADOW-GRADED every window without
      trading them: (a) in-window market-price momentum, (b) book-depth
      imbalance → three hit-rate series from day one.

DISCOVERY (2026-07-22):
  * Series slug = btc-updown-5m-<unix epoch of window start> (5-min grid);
    title "Bitcoin Up or Down - <Mon> <D>, HH:MM-HH:MM ET"; outcomes
    ["Up","Down"]; tick 0.01; CLOB minimum order 5 shares; endDate = start+5min.
  * FEES: gamma AND CLOB both report maker_base_fee = taker_base_fee = 1000 bps
    (the protocol MAX, formula fee = rate x min(p, 1-p) x shares). But the real
    measured trade (DoggyStyIe above) nets $48.61 on $48.75 gross locked spread
    — ~0.3% total drag → EFFECTIVE fees ≈ 0 on this series today. The sim
    therefore defaults CRYPTO5050_MAKER/TAKER_FEE_RATE = 0.0 with the CLOB
    formula implemented behind the knobs, so a real fee is one env flip away.
    DO NOT copy the weather 5% x p x (1-p) — different series, different rule.

HONESTY CAVEATS (stamped per spec):
  * MAKER-FILL PROXY IS OPTIMISTIC: a maker fill is simulated when the book's
    best ask crosses down to <= our resting bid (same conservative proxy as the
    weather rest sim) — but QUEUE POSITION IS UNMODELED, so real fills would be
    a subset. The maker-fill % on the dashboard doubles as the honesty meter.
  * Fill count per window is bounded by the hedge budget, the 12s fill
    spacing (~25 slots/window) and the venue's 5-share minimum; at the $180
    hedge budget with ~15-share fills this lands in the spec's 15-25 range.

BUDGET (Cowork sizing revision, 2026-07-22 PM, supersedes the $40 split):
window cap $200 — $20 RESERVED for the L2 lean (covers 20 shares at any price
≤ 0.99; the skip-if-unaffordable guard remains as a safety no-op), hedge
budget $180 with ~15-share fills (~25 12s-spaced slots ≈ the budget).
Allocation $1,000; NO halts/stops (operator decision of record, 2026-07-22
PM) — the module pauses ONLY when allocation + cumulative net cannot fund a
window, resumes if settlements re-fund it, and is refillable via
CRYPTO5050_ALLOCATION_USD in weather.env.
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("trading_bot")

WINDOW_SECONDS = 300
FILL_SHARES = 5.0            # venue minimum order size on this series
LEAN_AT_SECONDS = 150        # lean decided at/after window midpoint
FILL_SPACING_SECONDS = 12.0  # min gap between simulated hedge fills
MAKER_QUOTE_TTL_SECONDS = 36.0  # unfilled sim maker quote expires → next attempt
                                # is a TAKER (fix 2026-07-22 PM: a never-crossed
                                # maker bid used to block the alternation for the
                                # whole window — window 11 sat 0-fill for 4.8 min)
RESOLVE_GRACE_SECONDS = 600  # gamma polling budget before spot fallback
                             # (resolution runs as a BACKGROUND task — observed
                             # 2026-07-22: gamma still 0.965/0.035 two minutes
                             # after close, so blocking on it would eat the head
                             # of every next window; long grace is free async)
DEPTH_LEVELS = 5             # book-depth imbalance: sum of top-N bid sizes

SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


# ── pure decision core (no I/O — unit-testable) ──────────────────────────────

def window_epoch(now_ts: float) -> int:
    """Start epoch of the 5-min window containing now_ts."""
    return int(now_ts) - (int(now_ts) % WINDOW_SECONDS)


MAX_JOIN_LAG_SECONDS = 30


def is_partial_join(now_ts: float, epoch: int,
                    max_lag: float = MAX_JOIN_LAG_SECONDS) -> bool:
    """Clean-data guard (2026-07-22, learned from the first live windows): a
    window joined more than max_lag after its start must NOT be traded. The
    lean rules baseline on spot-at-join — mid-window that baseline is wrong
    while the market prices off the TRUE open (observed: post-restart lean
    picked UP @ 0.10 because spot had ticked up since the late join, while BTC
    was $65 DOWN from the real window open). Partial windows also contaminate
    the pair-VWAP series with truncated accumulation time."""
    return (now_ts - epoch) > max_lag


def window_slug(epoch: int) -> str:
    return f"btc-updown-5m-{epoch}"


def fee_for(price: float, shares: float, rate: float) -> float:
    """Polymarket CLOB fee formula: rate x min(p, 1-p) x shares. Default rate
    0.0 per the real-trade evidence above (base-fee fields are the protocol
    max, not the charged rate)."""
    try:
        p = float(price)
        return max(0.0, float(rate)) * min(p, 1.0 - p) * float(shares)
    except (TypeError, ValueError):
        return 0.0


def side_vwap(cost: float, shares: float):
    return (cost / shares) if shares > 0 else None


def pair_vwap(up_cost: float, up_shares: float, down_cost: float, down_shares: float):
    """Combined per-pair cost (up VWAP + down VWAP). None until BOTH sides own
    shares — a pair does not exist before that."""
    u, d = side_vwap(up_cost, up_shares), side_vwap(down_cost, down_shares)
    return (u + d) if (u is not None and d is not None) else None


def vwap_allows_fill(up_cost, up_shares, down_cost, down_shares,
                     side: str, price: float, shares: float) -> bool:
    """HARD RULE: refuse any hedge fill that would push the window's pair VWAP
    to >= $1.00. One-sided accumulation (no pair yet) is always allowed — the
    balance logic pairs it up next fill."""
    cost = price * shares
    nu_c, nu_s = (up_cost + cost, up_shares + shares) if side == "up" else (up_cost, up_shares)
    nd_c, nd_s = (down_cost + cost, down_shares + shares) if side == "down" else (down_cost, down_shares)
    pv = pair_vwap(nu_c, nu_s, nd_c, nd_s)
    return pv is None or pv < 1.0 - 1e-9


def choose_side(up_shares: float, down_shares: float,
                up_ask, down_ask, fill_shares: float = FILL_SHARES):
    """L1 accumulation side: balance first (shares differ by a fill or more →
    buy the lagging side), else buy whichever side is currently cheaper.
    Returns 'up' / 'down' / None (no usable ask)."""
    if up_ask is None and down_ask is None:
        return None
    if down_ask is None:
        return "up" if up_shares <= down_shares else None
    if up_ask is None:
        return "down" if down_shares <= up_shares else None
    if up_shares - down_shares >= fill_shares:
        return "down"
    if down_shares - up_shares >= fill_shares:
        return "up"
    return "up" if up_ask <= down_ask else "down"


def lean_pick_spot_drift(spot_open, spot_now):
    """LIVE lean rule: lean toward the side confirmed by spot movement since
    window open. None = no signal (flat / missing data) → no lean placed."""
    if spot_open is None or spot_now is None or spot_now == spot_open:
        return None
    return "up" if spot_now > spot_open else "down"


def lean_pick_momentum(up_mid_open, up_mid_now):
    """SHADOW rule (a): in-window market-price momentum — Up mid now vs at
    window open."""
    if up_mid_open is None or up_mid_now is None or up_mid_now == up_mid_open:
        return None
    return "up" if up_mid_now > up_mid_open else "down"


def lean_pick_late_recency(spot_t60, spot_close):
    """SHADOW rule (c), added 2026-07-22 PM: side of the BTC spot move over the
    FINAL ~60s of the window (spot_t60 = first sample inside the last minute).
    Tests the rotation/recency thesis (7/22 article) against the midpoint
    rules. Recorded at window close, graded at resolution, NEVER traded."""
    if spot_t60 is None or spot_close is None or spot_close == spot_t60:
        return None
    return "up" if spot_close > spot_t60 else "down"


BROWNIAN_P_FLOOR = 0.80      # gengar gate 1: only pick when P(side) >= this
BROWNIAN_PRICE_DISC = 0.85   # gengar gate 2: side's ask must be <= this x P
BROWNIAN_MIN_SAMPLES = 10    # need a usable in-window vol estimate


def brownian_p_up(spot_open, spot_now, samples, seconds_remaining):
    """SHADOW rule (d) core, added 2026-07-22 PM: P(Up) via a simple Brownian
    estimate — z = move-since-open / (realized per-sqrt-second vol x
    sqrt(time remaining)), P = Phi(z). `samples` = [(ts, price), ...] in-window
    spot polls (~4s cadence). None when the estimate isn't computable (missing
    opens, < BROWNIAN_MIN_SAMPLES, degenerate timing). Zero realized vol with
    nonzero drift → P saturates to 1.0/0.0; zero drift too → None."""
    from math import erf, sqrt
    if spot_open is None or spot_now is None or not samples             or len(samples) < BROWNIAN_MIN_SAMPLES or seconds_remaining <= 0:
        return None
    diffs, dts = [], []
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        if p0 is not None and p1 is not None and t1 > t0:
            diffs.append(p1 - p0)
            dts.append(t1 - t0)
    if len(diffs) < BROWNIAN_MIN_SAMPLES - 1:
        return None
    mean_d = sum(diffs) / len(diffs)
    var = sum((d - mean_d) ** 2 for d in diffs) / max(1, len(diffs) - 1)
    mean_dt = sum(dts) / len(dts)
    drift = spot_now - spot_open
    if var <= 0 or mean_dt <= 0:
        if drift > 0:
            return 1.0
        if drift < 0:
            return 0.0
        return None
    sigma_per_sqrt_sec = sqrt(var) / sqrt(mean_dt)
    z = drift / (sigma_per_sqrt_sec * sqrt(seconds_remaining))
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def brownian_gated_pick(p_up, up_ask, down_ask,
                        p_floor: float = BROWNIAN_P_FLOOR,
                        price_disc: float = BROWNIAN_PRICE_DISC):
    """SHADOW rule (d) gates (gengar_polymarket_bot's): record a side ONLY when
    P(side) >= p_floor AND that side's market ask <= price_disc x P(side); else
    'abstain'. None = estimate unavailable (distinct from a gated abstain)."""
    if p_up is None:
        return None
    for side, p_side, ask in (("up", p_up, up_ask), ("down", 1.0 - p_up, down_ask)):
        if p_side >= p_floor - 1e-9:
            if ask is not None and ask <= price_disc * p_side + 1e-9:
                return side
            return "abstain"          # confident but the market is too rich
    return "abstain"                  # no side clears the probability floor


def arb_sum(up_ask, down_ask):
    """INSTANT-ARB VISIBILITY (2026-07-22 PM, item 8 — NEVER traded): the
    Jonmaa/Gabagool trigger is best_ask(Up) + best_ask(Down) < $1.00. Returns
    the sum when both asks exist, else None. Counted per ~4s book poll to
    measure whether same-second pair arb SURVIVES to 4s granularity or is a
    pure millisecond game — calibrates required execution speed before anyone
    dreams of a live version."""
    if up_ask is None or down_ask is None:
        return None
    return up_ask + down_ask


def lean_pick_depth(up_bid_depth, down_bid_depth):
    """SHADOW rule (b): book-depth imbalance — lean toward the side with the
    deeper top-N bid stack (stronger support)."""
    if up_bid_depth is None or down_bid_depth is None or up_bid_depth == down_bid_depth:
        return None
    return "up" if up_bid_depth > down_bid_depth else "down"


def lean_affordable(price, shares: float, reserve: float) -> bool:
    """Budget split (Cowork 2026-07-22 PM): the lean has its OWN reserved budget
    ($18 of the $40 window cap). 20 shares above price 0.90 exceeds it → the
    lean is SKIPPED that window (picks still record — the hit-rate series is
    unconditional)."""
    if price is None:
        return False
    return price * shares <= reserve + 1e-9


def grade_pick(pick, winner):
    """1 = hit, 0 = miss, None = rule produced no pick (excluded from n)."""
    if pick is None or winner not in ("up", "down"):
        return None
    return 1 if pick == winner else 0


def settle_window(up_cost, up_shares, down_cost, down_shares,
                  lean_side, lean_cost, lean_shares,
                  winner: str, fees_paid: float) -> dict:
    """Window economics at resolution. Locked pairs redeem $1 each regardless
    of outcome; unhedged excess shares win/lose with the outcome; the lean is
    graded separately. Costs are ex-fee; fees_paid subtracted once at the end."""
    pairs = min(up_shares, down_shares)
    pv = pair_vwap(up_cost, up_shares, down_cost, down_shares)
    locked_pnl = pairs * (1.0 - pv) if (pairs > 0 and pv is not None) else 0.0
    # unhedged excess (hedge legs only — lean tracked separately)
    if up_shares > down_shares:
        ex_side, ex_sh, ex_vwap = "up", up_shares - down_shares, side_vwap(up_cost, up_shares)
    elif down_shares > up_shares:
        ex_side, ex_sh, ex_vwap = "down", down_shares - up_shares, side_vwap(down_cost, down_shares)
    else:
        ex_side, ex_sh, ex_vwap = None, 0.0, None
    if ex_sh > 0 and ex_vwap is not None:
        unhedged_pnl = ex_sh * (1.0 - ex_vwap) if ex_side == winner else -(ex_sh * ex_vwap)
    else:
        unhedged_pnl = 0.0
    if lean_side and lean_shares > 0:
        lean_pnl = (lean_shares - lean_cost) if lean_side == winner else -lean_cost
    else:
        lean_pnl = 0.0
    net = locked_pnl + unhedged_pnl + lean_pnl - fees_paid
    return {"pairs": pairs, "pair_vwap": pv, "locked_pnl": round(locked_pnl, 6),
            "unhedged_pnl": round(unhedged_pnl, 6), "lean_pnl": round(lean_pnl, 6),
            "net_pnl": round(net, 6)}


def best_bid_ask(book: dict):
    """(best_bid, best_ask, bid_depth_topN) from a CLOB book dict. Levels are
    NOT sorted server-side (weather lesson) — min/max explicitly."""
    bids, asks = [], []
    for side_key, out in (("bids", bids), ("asks", asks)):
        for lvl in (book.get(side_key) or []):
            try:
                out.append((float(lvl["price"]), float(lvl["size"])))
            except (KeyError, TypeError, ValueError):
                pass
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    depth = sum(s for _, s in sorted(bids, key=lambda x: -x[0])[:DEPTH_LEVELS]) if bids else None
    return best_bid, best_ask, depth


# ── the async runner (all I/O lives here) ────────────────────────────────────

class Crypto5050Runner:
    """One instance, created at app startup when CRYPTO_5050_ENABLED. All state
    is per-window and rebuilt from scratch each window; module-level halt is
    checked against the DB every window open."""

    def __init__(self, settings, session_factory, log_event):
        self.settings = settings
        self.SessionLocal = session_factory
        self.log_event = log_event
        self._exhausted_logged = False
        self._client = None

    # -- I/O helpers (each returns None on any failure — never raises) --------
    async def _get_json(self, url, params=None):
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=4.0)
        try:
            r = await self._client.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None

    async def fetch_spot(self):
        d = await self._get_json(SPOT_URL)
        try:
            return float(d["data"]["amount"])
        except (TypeError, KeyError, ValueError):
            return None

    async def fetch_book(self, token_id):
        d = await self._get_json(CLOB_BOOK, params={"token_id": token_id})
        return d if isinstance(d, dict) else None

    async def fetch_event(self, slug):
        d = await self._get_json(GAMMA_EVENTS, params={"slug": slug})
        try:
            return d[0] if d else None
        except (TypeError, IndexError):
            return None

    # -- allocation funding (NO halts/stops: operator decision of record,
    # 2026-07-22 PM — the module pauses ONLY when the allocation can no longer
    # fund a window, and resumes if settlements re-fund it; refill = raise
    # CRYPTO5050_ALLOCATION_USD in weather.env + restart) --------------------
    def _cumulative_net(self, db) -> float:
        from sqlalchemy import func
        from backend.models.database import CryptoWindow
        return float(db.query(func.coalesce(func.sum(CryptoWindow.net_pnl), 0.0))
                     .filter(CryptoWindow.status == "settled").scalar() or 0.0)

    def _cannot_fund_window(self, db) -> bool:
        """True when allocation + cumulative net < one window's cap. Non-latching
        — a pending window settling as a win can re-fund the module."""
        available = self.settings.CRYPTO5050_ALLOCATION_USD + self._cumulative_net(db)
        exhausted = available < self.settings.CRYPTO5050_MAX_WINDOW_NOTIONAL_USD
        if exhausted != self._exhausted_logged:
            self._exhausted_logged = exhausted
            if exhausted:
                self.log_event("warning",
                    f"[c5050] 💸 ALLOCATION EXHAUSTED: ${available:.2f} available < "
                    f"${self.settings.CRYPTO5050_MAX_WINDOW_NOTIONAL_USD:.0f} window cap — pausing new "
                    f"windows (picks/arb metrics pause too). Refill: raise "
                    f"CRYPTO5050_ALLOCATION_USD in weather.env + restart.")
            else:
                self.log_event("success",
                    f"[c5050] allocation re-funded (${available:.2f} available) — resuming windows")
        return exhausted

    # -- main loop ----------------------------------------------------------
    async def run(self):
        self.log_event("info", "[c5050] CRYPTO5050 paper runner started "
                               f"(poll {self.settings.CRYPTO5050_POLL_SECONDS:.0f}s, "
                               f"cap ${self.settings.CRYPTO5050_MAX_WINDOW_NOTIONAL_USD:.0f}/window, "
                               f"allocation ${self.settings.CRYPTO5050_ALLOCATION_USD:.0f}, NO halts)")
        pending_resolution = []          # background resolution tasks
        while True:
            try:
                # Stale sweep EVERY pass (2026-07-22 PM; was startup-only): a
                # window orphaned "open" by a mid-window restart is skipped by
                # the partial-join guard, so without a per-pass sweep it would
                # strand until the NEXT restart. Cheap query; the in-flight
                # window is excluded (its 5 minutes aren't over).
                try:
                    pending_resolution.extend(await self._sweep_stale())
                except Exception as e:
                    log.exception(f"[c5050] stale-window sweep failed (continuing): {e}")
                epoch = window_epoch(datetime.utcnow().timestamp())
                await self._trade_window(epoch, pending_resolution)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # crash isolation: log + cool off; the weather loop is untouched.
                log.exception(f"[c5050] window loop error (isolated): {e}")
                await asyncio.sleep(10)

    async def _trade_window(self, epoch, pending_resolution):
        from backend.models.database import CryptoWindow, CryptoFill
        st = self.settings
        db = self.SessionLocal()
        try:
            if self._cannot_fund_window(db):
                await self._sleep_until(epoch + WINDOW_SECONDS)
                return
            slug = window_slug(epoch)
            now_ts = datetime.utcnow().timestamp()
            if is_partial_join(now_ts, epoch):
                self.log_event("info",
                    f"[c5050] joined {slug} {now_ts - epoch:.0f}s late — skipping "
                    f"partial window (clean-data guard); trading resumes next boundary")
                await self._sleep_until(epoch + WINDOW_SECONDS)
                return
            ev = await self.fetch_event(slug)
            if ev is None:
                self.log_event("info", f"[c5050] no gamma event for {slug} — skipping window")
                await self._sleep_until(epoch + WINDOW_SECONDS)
                return
            try:
                import json as _json
                m = ev["markets"][0]
                toks = m.get("clobTokenIds")
                toks = _json.loads(toks) if isinstance(toks, str) else toks
                up_token, down_token = str(toks[0]), str(toks[1])
                question = m.get("question") or ev.get("title") or slug
            except Exception:
                self.log_event("warning", f"[c5050] malformed gamma event for {slug} — skipping")
                await self._sleep_until(epoch + WINDOW_SECONDS)
                return
            row = CryptoWindow(slug=slug, window_start=datetime.utcfromtimestamp(epoch),
                               question=question, up_token=up_token, down_token=down_token,
                               status="open")
            db.add(row)
            db.commit()
            self.log_event("info", f"[c5050] window OPEN: {question}")

            spot_open = await self.fetch_spot()
            row.spot_open = spot_open
            up_mid_open = None
            # Budget split (Cowork 2026-07-22 PM): $18 of the window cap is
            # RESERVED for the L2 lean; the L1 hedge gets the remainder ($22 at
            # the $40 cap). `spent` below tracks HEDGE spend only.
            cash_cap = st.CRYPTO5050_MAX_WINDOW_NOTIONAL_USD
            lean_reserve = getattr(st, "CRYPTO5050_LEAN_RESERVE_USD", 20.0)
            hedge_cap = cash_cap - lean_reserve
            # Fill size scales with the budget (sizing rev 2026-07-22): ~25
            # 12s-spaced fill slots × fill_sh×~$0.5 must be able to SPEND the
            # hedge budget; 5-share fills capped real spend at ~$60 of $180.
            fill_sh = float(getattr(st, "CRYPTO5050_FILL_SHARES", 15.0))
            spent = 0.0
            fees = 0.0
            last_fill_at = 0.0
            next_kind = "maker"                     # alternate → ~50/50 target
            resting = None                          # (side, bid_price, posted_ts) maker quote
            lean_done = False
            end_ts = epoch + WINDOW_SECONDS
            spot_samples = []            # in-window spot series (brownian rule)
            arb_polls = 0                # instant-arb visibility (item 8)
            arb_hits = 0
            arb_best = None

            while datetime.utcnow().timestamp() < end_ts - 1:
                tick_start = datetime.utcnow().timestamp()
                up_book = await self.fetch_book(up_token)
                down_book = await self.fetch_book(down_token)
                spot_now = await self.fetch_spot()
                if spot_now is not None:
                    spot_samples.append((tick_start, spot_now))
                if up_book is None or down_book is None:
                    await asyncio.sleep(st.CRYPTO5050_POLL_SECONDS)
                    continue
                u_bid, u_ask, u_depth = best_bid_ask(up_book)
                d_bid, d_ask, d_depth = best_bid_ask(down_book)
                if up_mid_open is None and u_bid is not None and u_ask is not None:
                    up_mid_open = (u_bid + u_ask) / 2.0
                # instant-arb visibility tick (item 8 — never traded)
                _asum = arb_sum(u_ask, d_ask)
                if _asum is not None:
                    arb_polls += 1
                    if _asum < 1.0 - 1e-9:
                        arb_hits += 1
                    arb_best = _asum if arb_best is None else min(arb_best, _asum)
                # late-recency baseline + brownian-gated pick, both at the first
                # sample inside the final ~60s (T-60)
                if row.spot_t60 is None and spot_now is not None \
                        and tick_start >= end_ts - 60:
                    row.spot_t60 = spot_now
                    p_up = brownian_p_up(spot_open, spot_now, spot_samples,
                                         max(1.0, end_ts - tick_start))
                    row.p_up_brownian = p_up
                    row.pick_brownian = brownian_gated_pick(p_up, u_ask, d_ask)

                # -- maker-fill check on the standing quote (optimistic: queue
                # position unmodeled — see module docstring) --
                if resting is not None:
                    side, bid_px, posted_ts = resting
                    ask_now = u_ask if side == "up" else d_ask
                    if ask_now is not None and ask_now <= bid_px + 1e-9:
                        ok = self._apply_fill(db, row, side, "maker", bid_px, fill_sh,
                                              spent, hedge_cap, fees)
                        if ok:
                            spent, fees = ok
                            last_fill_at = tick_start
                        resting = None
                    elif tick_start - posted_ts >= MAKER_QUOTE_TTL_SECONDS:
                        # quote expired unfilled → free the slot, go taker next
                        # (the maker-deadlock fix; keeps the ~50/50 mix honest —
                        # quiet markets naturally shift toward takers, exactly
                        # like a real wallet that stops waiting)
                        resting = None
                        next_kind = "taker"

                # -- new fill attempt (spacing + budget + VWAP hard rule) --
                if tick_start - last_fill_at >= FILL_SPACING_SECONDS and resting is None:
                    side = choose_side(row.up_shares or 0.0, row.down_shares or 0.0,
                                       u_ask, d_ask, fill_shares=fill_sh)
                    if side is not None:
                        px = u_ask if side == "up" else d_ask
                        if next_kind == "maker":
                            bid_px = u_bid if side == "up" else d_bid
                            if bid_px is not None and vwap_allows_fill(
                                    row.up_cost or 0.0, row.up_shares or 0.0,
                                    row.down_cost or 0.0, row.down_shares or 0.0,
                                    side, bid_px, fill_sh) \
                                    and spent + bid_px * fill_sh <= hedge_cap:
                                resting = (side, bid_px, tick_start)
                                next_kind = "taker"
                        else:
                            if px is not None and vwap_allows_fill(
                                    row.up_cost or 0.0, row.up_shares or 0.0,
                                    row.down_cost or 0.0, row.down_shares or 0.0,
                                    side, px, fill_sh) \
                                    and spent + px * fill_sh <= hedge_cap:
                                ok = self._apply_fill(db, row, side, "taker", px,
                                                      fill_sh, spent, hedge_cap, fees)
                                if ok:
                                    spent, fees = ok
                                    last_fill_at = tick_start
                                    next_kind = "maker"

                # -- L2 lean at/after midpoint (once) --
                if not lean_done and tick_start >= epoch + LEAN_AT_SECONDS:
                    lean_done = True
                    pick_spot = lean_pick_spot_drift(spot_open, spot_now)
                    u_mid_now = ((u_bid + u_ask) / 2.0) if (u_bid is not None and u_ask is not None) else None
                    row.pick_spot_drift = pick_spot
                    row.pick_momentum = lean_pick_momentum(up_mid_open, u_mid_now)
                    row.pick_depth = lean_pick_depth(u_depth, d_depth)
                    if pick_spot is not None:
                        lean_px = u_ask if pick_spot == "up" else d_ask
                        lean_sh = st.CRYPTO5050_LEAN_SHARES
                        if not lean_affordable(lean_px, lean_sh, lean_reserve):
                            self.log_event("info",
                                f"[c5050] LEAN SKIPPED: {lean_sh:.0f}sh {pick_spot.upper()} "
                                f"@ {lean_px if lean_px is not None else 'n/a'} exceeds the "
                                f"${lean_reserve:.0f} reserve (px > 0.90) — picks still recorded")
                        else:
                            fee = fee_for(lean_px, lean_sh, st.CRYPTO5050_TAKER_FEE_RATE)
                            row.lean_side = pick_spot
                            row.lean_shares = lean_sh
                            row.lean_price = lean_px
                            row.lean_cost = round(lean_px * lean_sh, 6)
                            fees += fee
                            db.add(CryptoFill(window_id=row.id, side=pick_spot,
                                              fill_kind="taker", price=lean_px,
                                              shares=lean_sh, cost=row.lean_cost,
                                              fee=fee, note="lean(spot_drift)"))
                            self.log_event("trade",
                                f"[c5050] LEAN {pick_spot.upper()} {lean_sh:.0f}sh @ {lean_px:.2f} "
                                f"(spot {spot_open}→{spot_now}; shadow: mom={row.pick_momentum} "
                                f"depth={row.pick_depth})")
                    db.commit()

                elapsed = datetime.utcnow().timestamp() - tick_start
                await asyncio.sleep(max(0.5, st.CRYPTO5050_POLL_SECONDS - elapsed))

            # -- window over: snapshot + queue for resolution --
            row.fees_paid = round(fees, 6)
            row.spot_close = await self.fetch_spot()
            row.pick_late_recency = lean_pick_late_recency(row.spot_t60, row.spot_close)
            row.arb_polls = arb_polls
            row.arb_hits = arb_hits
            row.arb_best_sum = arb_best
            pv = pair_vwap(row.up_cost or 0.0, row.up_shares or 0.0,
                           row.down_cost or 0.0, row.down_shares or 0.0)
            row.pair_vwap = pv
            row.status = "closing"
            db.commit()
            self.log_event("info",
                f"[c5050] window CLOSED: {question} — {int(row.fills_count or 0)} fills "
                f"({int(row.maker_fills or 0)}M/{int(row.taker_fills or 0)}T), "
                f"pair VWAP {('%.3f' % pv) if pv is not None else 'n/a'}, "
                f"lean {row.lean_side or 'none'}")
            # Resolution runs as a BACKGROUND task (own DB session) so the main
            # loop opens the NEXT window immediately — gamma takes minutes to
            # flip past the strict 0.99 threshold, and awaiting it inline was
            # eating the head of every following window (2026-07-22 fix).
            task = asyncio.create_task(self._resolve_window_by_id(row.id),
                                       name=f"c5050-resolve-{row.slug}")
            pending_resolution.append(task)
            pending_resolution[:] = [t for t in pending_resolution if not t.done()]
        finally:
            db.close()

    async def _sweep_stale(self):
        """Startup sweep: any window left 'open'/'closing' by a restart whose
        5 minutes are over gets queued for background resolution instead of
        sitting stranded forever. Returns the spawned tasks."""
        from backend.models.database import CryptoWindow
        tasks = []
        db = self.SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=WINDOW_SECONDS + 5)
            stale = (db.query(CryptoWindow)
                     .filter(CryptoWindow.status.in_(("open", "closing")),
                             CryptoWindow.window_start < cutoff).all())
            for r in stale:
                r.status = "closing"
                db.commit()
                self.log_event("info",
                    f"[c5050] stale window {r.slug} queued for resolution (restart sweep)")
                tasks.append(asyncio.create_task(
                    self._resolve_window_by_id(r.id), name=f"c5050-sweep-{r.slug}"))
        finally:
            db.close()
        return tasks

    async def _resolve_window_by_id(self, window_id):
        """Background-task wrapper: resolve one window on its OWN session so it
        can outlive the window loop's session. Never raises."""
        from backend.models.database import CryptoWindow
        try:
            db = self.SessionLocal()
            try:
                row = db.query(CryptoWindow).filter(CryptoWindow.id == window_id).first()
                if row is not None and row.status == "closing":
                    await self._resolve_window(db, row)
            finally:
                db.close()
        except Exception as e:
            log.exception(f"[c5050] background resolution failed for window {window_id}: {e}")

    def _apply_fill(self, db, row, side, kind, price, shares, spent, cap, fees):
        """Book one simulated hedge fill onto the window row. Returns the new
        (spent, fees) or None if refused (cap)."""
        from backend.models.database import CryptoFill
        st = self.settings
        cost = round(price * shares, 6)
        if spent + cost > cap + 1e-9:
            return None
        rate = (st.CRYPTO5050_MAKER_FEE_RATE if kind == "maker"
                else st.CRYPTO5050_TAKER_FEE_RATE)
        fee = fee_for(price, shares, rate)
        if side == "up":
            row.up_shares = (row.up_shares or 0.0) + shares
            row.up_cost = round((row.up_cost or 0.0) + cost, 6)
        else:
            row.down_shares = (row.down_shares or 0.0) + shares
            row.down_cost = round((row.down_cost or 0.0) + cost, 6)
        row.fills_count = (row.fills_count or 0) + 1
        if kind == "maker":
            row.maker_fills = (row.maker_fills or 0) + 1
        else:
            row.taker_fills = (row.taker_fills or 0) + 1
        db.add(CryptoFill(window_id=row.id, side=side, fill_kind=kind,
                          price=price, shares=shares, cost=cost, fee=fee))
        db.commit()
        pv = pair_vwap(row.up_cost or 0.0, row.up_shares or 0.0,
                       row.down_cost or 0.0, row.down_shares or 0.0)
        self.log_event("trade",
            f"[c5050] FILL {kind} {side.upper()} {shares:.0f}sh @ {price:.2f} "
            f"(pair VWAP {'%.3f' % pv if pv is not None else 'building'})")
        return spent + cost, fees + fee

    async def _resolve_window(self, db, row):
        """Poll gamma for the authoritative resolution (strict 0.99/0.01 like the
        weather parser); fall back to spot direction after the grace budget."""
        import json as _json
        winner, source = None, None
        deadline = datetime.utcnow().timestamp() + RESOLVE_GRACE_SECONDS
        while datetime.utcnow().timestamp() < deadline:
            ev = await self.fetch_event(row.slug)
            try:
                prices = ev["markets"][0].get("outcomePrices")
                prices = _json.loads(prices) if isinstance(prices, str) else prices
                p_up = float(prices[0])
                if p_up >= 0.99:
                    winner, source = "up", "gamma"
                    break
                if p_up <= 0.01:
                    winner, source = "down", "gamma"
                    break
            except Exception:
                pass
            await asyncio.sleep(15)
        if winner is None:
            if row.spot_open is not None and row.spot_close is not None \
                    and row.spot_close != row.spot_open:
                winner = "up" if row.spot_close > row.spot_open else "down"
                source = "spot_fallback"
            else:
                row.status = "unresolved"
                db.commit()
                self.log_event("warning", f"[c5050] {row.slug} UNRESOLVED (no gamma, no spot delta)")
                return
        econ = settle_window(row.up_cost or 0.0, row.up_shares or 0.0,
                             row.down_cost or 0.0, row.down_shares or 0.0,
                             row.lean_side, row.lean_cost or 0.0, row.lean_shares or 0.0,
                             winner, row.fees_paid or 0.0)
        row.resolution = winner
        row.resolution_source = source
        row.locked_pairs = econ["pairs"]
        row.locked_pnl = econ["locked_pnl"]
        row.lean_pnl = econ["lean_pnl"]
        row.net_pnl = econ["net_pnl"]
        row.hit_spot_drift = grade_pick(row.pick_spot_drift, winner)
        row.hit_momentum = grade_pick(row.pick_momentum, winner)
        row.hit_depth = grade_pick(row.pick_depth, winner)
        row.hit_late_recency = grade_pick(row.pick_late_recency, winner)
        row.hit_brownian = grade_pick(
            row.pick_brownian if row.pick_brownian in ("up", "down") else None, winner)
        row.resolved_at = datetime.utcnow()
        row.status = "settled"
        db.commit()
        self.log_event("success",
            f"[c5050] SETTLED {winner.upper()} ({source}): locked {econ['locked_pnl']:+.2f} "
            f"unhedged {econ['unhedged_pnl']:+.2f} lean {econ['lean_pnl']:+.2f} "
            f"→ net {econ['net_pnl']:+.2f} | picks: spot={row.pick_spot_drift} "
            f"mom={row.pick_momentum} depth={row.pick_depth} late={row.pick_late_recency} "
            f"brown={row.pick_brownian} | arb<\$1: {row.arb_hits or 0}/{row.arb_polls or 0} "
            f"polls (best {row.arb_best_sum if row.arb_best_sum is not None else 'n/a'})")

    async def _sleep_until(self, ts):
        delta = ts - datetime.utcnow().timestamp()
        if delta > 0:
            await asyncio.sleep(min(delta, WINDOW_SECONDS))


def start_crypto5050(settings, session_factory, log_event):
    """Create the runner task. Called from the app startup hook, itself wrapped
    so even a failure HERE cannot affect the weather path."""
    runner = Crypto5050Runner(settings, session_factory, log_event)
    return asyncio.create_task(runner.run(), name="crypto5050")
