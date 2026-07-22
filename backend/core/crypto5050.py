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


LOGIC_VERSION = "v2"          # balance discipline (2026-07-22 PM)
BALANCE_ONLY_SECONDS = 60     # final-60s: no imbalance-increasing fills
BALANCE_SWEEP_AT = 20         # one taker balancing sweep at ~T-20s
BALANCE_SWEEP_MIN_SH = 5.0    # only sweep residues bigger than this
BALANCE_SWEEP_MAX_ASK = 0.97  # past this, eating the residue beats a worse pair
MARGINAL_PAIR_MAX = 0.99      # surplus-side fill needs an instantly-pairable bargain


def residue(up_shares: float, down_shares: float):
    """(side, shares) of the unhedged L1 excess; (None, 0.0) when balanced."""
    if up_shares > down_shares:
        return "up", up_shares - down_shares
    if down_shares > up_shares:
        return "down", down_shares - up_shares
    return None, 0.0


def choose_side_v2(up_shares: float, down_shares: float, up_ask, down_ask,
                   fill_shares: float = FILL_SHARES):
    """BALANCE DISCIPLINE (v2, 2026-07-22 PM — fixes the residue bleed; the
    execution-quality objective, stated: end-of-window unhedged shares beyond
    the traded lean ~ 0):
      * Balanced book → buy the cheaper side (as v1).
      * Imbalanced book → the DEFICIT side has priority whenever it's quoted.
        The SURPLUS side may be added ONLY on an instantly-pairable bargain:
        both sides quoted AND up_ask + down_ask < MARGINAL_PAIR_MAX ($0.99) —
        a genuine sub-$0.99 marginal pair justifies temporary imbalance;
        otherwise every fill reduces imbalance."""
    if up_ask is None and down_ask is None:
        return None
    surplus, sh = residue(up_shares, down_shares)
    deficit = ("down" if surplus == "up" else "up") if surplus else None
    if sh == 0.0:
        if up_ask is None:
            return "down"
        if down_ask is None:
            return "up"
        return "up" if up_ask <= down_ask else "down"
    deficit_ask = up_ask if deficit == "up" else down_ask
    if deficit_ask is not None:
        # surplus bargain exception: instantly-pairable sub-$0.99 pair
        if up_ask is not None and down_ask is not None                 and up_ask + down_ask < MARGINAL_PAIR_MAX - 1e-9:
            surplus = "down" if deficit == "up" else "up"
            surplus_ask = down_ask if deficit == "up" else up_ask
            if surplus_ask < deficit_ask:
                return surplus
        return deficit
    # deficit side unquoted: only the bargain exception may add surplus
    return None


def balance_only_allows(side: str, up_shares: float, down_shares: float) -> bool:
    """Final-60s BALANCE-ONLY mode: a fill is allowed only if it REDUCES the
    hedge imbalance (never increases it; balanced book → nothing allowed)."""
    surplus, sh = residue(up_shares, down_shares)
    return sh > 0 and side != surplus


def balance_sweep_wanted(up_shares: float, down_shares: float, deficit_ask,
                         min_sh: float = BALANCE_SWEEP_MIN_SH,
                         max_ask: float = BALANCE_SWEEP_MAX_ASK):
    """The ~T-20s one-shot taker balancing sweep: (side, shares) to buy, or
    None. Fires only when |residue| > min_sh AND the balancing ask <= max_ask
    (0.97) — past that, eating the residue beats locking a worse pair."""
    surplus, sh = residue(up_shares, down_shares)
    if surplus is None or sh <= min_sh:
        return None
    if deficit_ask is None or deficit_ask > max_ask + 1e-9:
        return None
    return ("down" if surplus == "up" else "up"), sh


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


# ── loss post-mortems (2026-07-22 PM) — arithmetic, not prose ────────────────

POSTMORTEM_DIR = "data/crypto5050_postmortems"


def postmortem_decompose(row) -> dict:
    """P&L decomposition + counterfactual arithmetic for one settled window.
    Pure given the row (+ optional .polls list of CryptoPoll-like objects).
    Approximations are LABELED: pre-crypto_polls windows have no book history,
    so opposite-side prices fall back to the 1-price complement."""
    up_sh, dn_sh = row.up_shares or 0.0, row.down_shares or 0.0
    up_c, dn_c = row.up_cost or 0.0, row.down_cost or 0.0
    winner = row.resolution
    pairs = min(up_sh, dn_sh)
    pv = pair_vwap(up_c, up_sh, dn_c, dn_sh)
    locked = pairs * (1.0 - pv) if (pairs > 0 and pv is not None) else 0.0
    surplus, res_sh = residue(up_sh, dn_sh)
    res_vwap = side_vwap(up_c, up_sh) if surplus == "up" else side_vwap(dn_c, dn_sh)
    if res_sh > 0 and res_vwap is not None:
        residue_pnl = res_sh * (1.0 - res_vwap) if surplus == winner else -(res_sh * res_vwap)
    else:
        residue_pnl = 0.0
    lean_pnl = row.lean_pnl or 0.0
    fees = row.fees_paid or 0.0
    net = row.net_pnl if row.net_pnl is not None else (locked + residue_pnl + lean_pnl - fees)

    polls = getattr(row, "polls", None) or []
    last_poll = polls[-1] if polls else None

    # counterfactual 1: balanced at close — buy res_sh of the deficit side at
    # the last known deficit ask (poll → close mark → complement approximation)
    cf = {}
    approx_notes = []
    if res_sh > 0:
        deficit = "down" if surplus == "up" else "up"
        close_ask = None
        src = None
        if last_poll is not None:
            close_ask = last_poll.up_ask if deficit == "up" else last_poll.down_ask
            src = "last poll"
        if close_ask is None:
            close_ask = row.up_mark if deficit == "up" else row.down_mark
            src = "close mark"
        if close_ask is None and res_vwap is not None:
            close_ask = max(0.01, min(0.99, 1.0 - res_vwap))
            src = "complement approx"
        if close_ask is not None:
            # new pairs redeem $1 each; delta vs the actual residue outcome
            delta = (res_sh * (1.0 - (res_vwap + close_ask))) - residue_pnl
            cf["balanced_at_close"] = round(net + delta, 2)
            cf["balanced_delta"] = round(delta, 2)
            approx_notes.append(f"balancing ask from {src} ({close_ask:.2f})")
    if "balanced_at_close" not in cf:
        cf["balanced_at_close"] = round(net, 2)
        cf["balanced_delta"] = 0.0

    # counterfactual 2: no lean
    cf["no_lean"] = round(net - lean_pnl, 2)

    # counterfactual 3: opposite lean (opposite ask at lean time: polls if
    # available, else 1 - lean_price complement, labeled)
    if row.lean_side and (row.lean_shares or 0) > 0 and row.lean_price is not None:
        opp = "down" if row.lean_side == "up" else "up"
        opp_ask = None
        if polls:
            # nearest poll to the lean fill: first poll after midpoint works —
            # keep simple: median poll of the window's second half
            half = [q for q in polls][len(polls) // 2:]
            for q in half:
                opp_ask = q.up_ask if opp == "up" else q.down_ask
                if opp_ask is not None:
                    break
        if opp_ask is None:
            opp_ask = max(0.01, min(0.99, 1.0 - row.lean_price))
            approx_notes.append("opposite-lean price = 1 − lean price (no polls)")
        opp_cost = opp_ask * row.lean_shares
        opp_pnl = (row.lean_shares - opp_cost) if opp == winner else -opp_cost
        cf["opposite_lean"] = round(net - lean_pnl + opp_pnl, 2)
    else:
        cf["opposite_lean"] = None

    # counterfactual 4: each rule's pick traded as the lean
    rule_cf = {}
    for rule, pick in (("spot_drift", row.pick_spot_drift),
                       ("momentum", row.pick_momentum),
                       ("depth", row.pick_depth),
                       ("late_recency", row.pick_late_recency),
                       ("brownian", row.pick_brownian)):
        if pick not in ("up", "down"):
            rule_cf[rule] = None
            continue
        if row.lean_side and pick == row.lean_side:
            rule_cf[rule] = round(net, 2)      # identical to what was traded
            continue
        px = None
        if polls:
            half = [q for q in polls][len(polls) // 2:]
            for q in half:
                px = q.up_ask if pick == "up" else q.down_ask
                if px is not None:
                    break
        if px is None and row.lean_price is not None:
            px = max(0.01, min(0.99, 1.0 - row.lean_price))
        if px is None:
            rule_cf[rule] = None
            continue
        sh = row.lean_shares or 20.0
        pk_pnl = (sh - px * sh) if pick == winner else -(px * sh)
        rule_cf[rule] = round(net - lean_pnl + pk_pnl, 2)

    # verdict by arithmetic: the most negative component names the loss
    components = {"residue-loss": residue_pnl, "lean-wrong": lean_pnl,
                  "hedge-overpaid": locked, "structural": -fees}
    negs = {k: v for k, v in components.items() if v < -0.005}
    verdict = min(negs, key=negs.get) if negs else "structural"

    return {"locked": round(locked, 2), "residue_pnl": round(residue_pnl, 2),
            "residue_side": surplus, "residue_shares": round(res_sh, 1),
            "lean_pnl": round(lean_pnl, 2), "fees": round(fees, 2),
            "net": round(net, 2), "cf": cf, "rule_cf": rule_cf,
            "verdict": verdict, "approx_notes": approx_notes}


def postmortem_markdown(row, decomp, fills, polls_n: int) -> str:
    """Render the post-mortem .md from the arithmetic (no prose speculation)."""
    d = decomp
    lines = [
        f"# Post-mortem — window {row.id} ({row.question or row.slug})",
        "",
        f"- **Net: {d['net']:+.2f}** | resolution: **{(row.resolution or '?').upper()}**"
        f" ({row.resolution_source or '?'}) | logic: {row.logic_version or 'v1'}",
        f"- **VERDICT: `{d['verdict']}`** (most-negative component, by arithmetic)",
        "",
        "## (a) P&L decomposition",
        f"| locked | residue | lean | fees | net |",
        f"|---|---|---|---|---|",
        f"| {d['locked']:+.2f} | {d['residue_pnl']:+.2f}"
        f" ({d['residue_shares']:.0f}sh {d['residue_side'] or '—'})"
        f" | {d['lean_pnl']:+.2f} | {d['fees']:.2f} | **{d['net']:+.2f}** |",
        "",
        "## (b) Timeline (fills vs spot)",
        f"spot: open {row.spot_open} → T-60 {row.spot_t60} → close {row.spot_close}"
        f" · poll snapshots stored: {polls_n}"
        + ("" if polls_n else " ⚠ pre-crypto_polls window — book timeline unavailable"),
        "",
        "| t | side | kind | px | sh | note |", "|---|---|---|---|---|---|",
    ]
    for f in fills:
        lines.append(f"| {str(f.ts)[11:19]} | {f.side} | {f.fill_kind} "
                     f"| {f.price:.2f} | {f.shares:.0f} | {f.note or ''} |")
    lines += [
        "",
        "## (c) Rule table (pick vs outcome)",
        "| rule | pick | hit |", "|---|---|---|",
        f"| spot_drift | {row.pick_spot_drift} | {row.hit_spot_drift} |",
        f"| momentum | {row.pick_momentum} | {row.hit_momentum} |",
        f"| depth | {row.pick_depth} | {row.hit_depth} |",
        f"| late_recency | {row.pick_late_recency} | {row.hit_late_recency} |",
        f"| brownian | {row.pick_brownian} | {row.hit_brownian} |",
        "",
        "## (d) Computed counterfactual nets",
        f"- balanced-at-close: **{d['cf']['balanced_at_close']:+.2f}**"
        f" (Δ {d['cf']['balanced_delta']:+.2f})",
        f"- no lean: {d['cf']['no_lean']:+.2f}",
        f"- opposite lean: {d['cf']['opposite_lean'] if d['cf']['opposite_lean'] is not None else 'n/a'}",
        "- rule-as-lean: " + ", ".join(
            f"{k}={v if v is not None else 'n/a'}" for k, v in d["rule_cf"].items()),
    ]
    if d["approx_notes"]:
        lines += ["", "*Approximations: " + "; ".join(d["approx_notes"]) + "*"]
    lines += ["", f"## (e) Verdict", f"`{d['verdict']}` — chosen by the arithmetic above."]
    return "\n".join(lines) + "\n"


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
        try:
            await self._backfill_postmortems()
        except Exception as e:
            log.exception(f"[c5050] post-mortem backfill failed (continuing): {e}")
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
                               status="open", logic_version=LOGIC_VERSION)
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
            balance_swept = False        # the one-shot ~T-20s balancing taker
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
                # poll snapshot (post-mortem timeline; pruned after ~3 days)
                try:
                    from backend.models.database import CryptoPoll
                    db.add(CryptoPoll(window_id=row.id, spot=spot_now,
                                      up_bid=u_bid, up_ask=u_ask,
                                      down_bid=d_bid, down_ask=d_ask))
                except Exception:
                    pass
                # live marks for the dashboard's open-window unrealized P&L
                row.up_mark = ((u_bid + u_ask) / 2.0
                               if (u_bid is not None and u_ask is not None) else u_ask or u_bid)
                row.down_mark = ((d_bid + d_ask) / 2.0
                                 if (d_bid is not None and d_ask is not None) else d_ask or d_bid)
                if int(tick_start) % 20 < st.CRYPTO5050_POLL_SECONDS:
                    db.commit()          # flush marks ~every 20s even with no fills
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

                # -- BALANCE DISCIPLINE (v2): final-60s balance-only mode +
                # the one-shot ~T-20s taker balancing sweep --
                in_final_60 = tick_start >= end_ts - BALANCE_ONLY_SECONDS
                if in_final_60 and not balance_swept and tick_start >= end_ts - BALANCE_SWEEP_AT:
                    balance_swept = True
                    surplus_side, _sh = residue(row.up_shares or 0.0, row.down_shares or 0.0)
                    d_side_ask = (u_ask if surplus_side == "down" else d_ask) if surplus_side else None
                    sweep = balance_sweep_wanted(row.up_shares or 0.0, row.down_shares or 0.0, d_side_ask)
                    if sweep is not None:
                        sw_side, sw_sh = sweep
                        sw_px = u_ask if sw_side == "up" else d_ask
                        # balancing REDUCES risk → allowed up to the full window
                        # cap (hedge budget + unused lean reserve), never beyond
                        if sw_px is not None and spent + (row.lean_cost or 0.0)                                 + sw_px * sw_sh <= cash_cap:
                            ok = self._apply_fill(db, row, sw_side, "taker", sw_px,
                                                  sw_sh, spent, cash_cap, fees)
                            if ok:
                                spent, fees = ok
                                last_fill_at = tick_start
                                self.log_event("info",
                                    f"[c5050] BALANCE SWEEP {sw_side.upper()} {sw_sh:.0f}sh "
                                    f"@ {sw_px:.2f} (residue worked off at T-20)")
                # -- new fill attempt (spacing + budget + VWAP hard rule) --
                if tick_start - last_fill_at >= FILL_SPACING_SECONDS and resting is None:
                    side = choose_side_v2(row.up_shares or 0.0, row.down_shares or 0.0,
                                          u_ask, d_ask, fill_shares=fill_sh)
                    if side is not None and in_final_60 and not balance_only_allows(
                            side, row.up_shares or 0.0, row.down_shares or 0.0):
                        side = None      # final-60s: no imbalance-increasing fills
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

    def _write_postmortem(self, db, row) -> None:
        """Generate data/crypto5050_postmortems/window_<id>.md for a settled
        LOSING window; stamps row.verdict + row.cf_balanced_delta. Idempotent
        (existing file → still recomputes the row fields; file overwritten with
        the same arithmetic). Never raises."""
        import os
        try:
            from backend.models.database import CryptoFill, CryptoPoll
            fills = (db.query(CryptoFill).filter(CryptoFill.window_id == row.id)
                     .order_by(CryptoFill.id).all())
            polls = (db.query(CryptoPoll).filter(CryptoPoll.window_id == row.id)
                     .order_by(CryptoPoll.id).all())
            row.polls = polls
            decomp = postmortem_decompose(row)
            row.verdict = decomp["verdict"]
            row.cf_balanced_delta = decomp["cf"]["balanced_delta"]
            md = postmortem_markdown(row, decomp, fills, len(polls))
            os.makedirs(POSTMORTEM_DIR, exist_ok=True)
            path = os.path.join(POSTMORTEM_DIR, f"window_{row.id}.md")
            with open(path, "w") as fh:
                fh.write(md)
            db.commit()
            self.log_event("info",
                f"[c5050] post-mortem written: window {row.id} → `{decomp['verdict']}` "
                f"(net {decomp['net']:+.2f}, balanced-close Δ {decomp['cf']['balanced_delta']:+.2f})")
        except Exception as e:
            log.exception(f"[c5050] post-mortem failed for window {row.id}: {e}")

    async def _backfill_postmortems(self):
        """Startup backfill (item 6): every settled net<0 window without a
        verdict gets a post-mortem. Idempotent — verdict-stamped rows skipped.
        Pre-crypto_polls windows are data-limited and their .md says so."""
        from backend.models.database import CryptoWindow
        db = self.SessionLocal()
        try:
            rows = (db.query(CryptoWindow)
                    .filter(CryptoWindow.status == "settled",
                            CryptoWindow.net_pnl < 0,
                            CryptoWindow.verdict.is_(None)).all())
            for r in rows:
                self._write_postmortem(db, r)
            if rows:
                self.log_event("info", f"[c5050] post-mortem backfill: {len(rows)} window(s)")
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
            # prune poll snapshots older than ~3 days (post-mortems long written)
            try:
                from backend.models.database import CryptoPoll
                db.query(CryptoPoll).filter(
                    CryptoPoll.ts < datetime.utcnow() - timedelta(days=3)
                ).delete(synchronize_session=False)
                db.commit()
            except Exception:
                pass
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
        if (row.net_pnl or 0.0) < 0:
            self._write_postmortem(db, row)
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
