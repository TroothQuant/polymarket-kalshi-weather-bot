"""FAK order-precision fix (2026-07-03). The first real actionable signal (LA
"high below 67F", NO @ ~0.51, $11) built a LIMIT order whose maker (shares×price)
carried >2 decimals and the CLOB rejected it ("invalid amounts, maker max 2 /
taker max 4 dp"). The fix builds a MARKET order (amount=USDC) so the maker rounds
to <=2 dp and the taker to <=4 dp, spend rounds DOWN (never over the cap), and the
15-share min is re-checked AFTER rounding.
"""
import pytest
from backend.core.live_trader import WeatherLiveTrader as W


def _within_dp(x, dp):
    """True iff x carries no more than dp decimals (float-repr robust)."""
    return abs(round(x, dp) - x) < 1e-9


# ── regression: the exact 2026-07-03 LA rejection scenario ───────────────────
def test_la_regression_maker_is_2dp_now():
    # OLD path: shares=round(11/0.53,2)=20.75 -> maker=20.75*0.53=10.9975 (>2dp) -> REJECT.
    a = W.build_order_args("TOK", 11.0, 0.51, "0.01")   # NO @ ~0.51 entry, +2 ticks -> 0.53
    assert a["price"] == 0.53
    assert _within_dp(a["amount_usd"], 2)      # maker (USDC) <= 2 dp  (the fix)
    assert _within_dp(a["size"], 4)            # taker (shares) <= 4 dp
    assert a["amount_usd"] <= 11.0             # round DOWN — never over the $11 cap
    assert a["size"] >= 15


# ── awkward decimals (the old verifier only passed because 15*0.02=0.30 was clean) ──
def test_awkward_decimals_price_037():
    a = W.build_order_args("TOK", 11.0, 0.35, "0.01")   # +2 ticks -> 0.37
    assert a["price"] == 0.37
    assert _within_dp(a["amount_usd"], 2)
    assert _within_dp(a["size"], 4)
    assert a["amount_usd"] <= 11.0
    # OLD path here would have been round(11/0.37,2)=29.73 -> 29.73*0.37=10.9999.. (>2dp).


# ── exhaustive invariant across a price/spend grid ───────────────────────────
def test_maker_taker_precision_and_no_overspend_grid():
    for mp in [0.09, 0.11, 0.23, 0.34, 0.37, 0.49, 0.51, 0.63, 0.68]:
        for usd in [11.0, 10.99, 8.33, 9.07]:
            try:
                a = W.build_order_args("TOK", usd, mp, "0.01")
            except ValueError:
                continue                       # <15 shares → clean refuse (fine)
            assert _within_dp(a["amount_usd"], 2), (mp, usd, a)
            assert _within_dp(a["size"], 4), (mp, usd, a)
            assert a["amount_usd"] <= usd + 1e-9, (mp, usd, a)   # never overspend


# ── 15-share minimum AFTER rounding ──────────────────────────────────────────
def test_15_share_min_after_rounding():
    with pytest.raises(ValueError):
        W.build_order_args("TOK", 11.0, 0.80, "0.01")   # $11 @ 0.82 -> ~13.4 sh < 15
    assert W.build_order_args("TOK", 11.0, 0.40, "0.01")["size"] >= 15


# ── tick awareness (finer tick → price/taker precision follow the tick config) ──
def test_tick_awareness_finer_tick():
    a = W.build_order_args("TOK", 11.0, 0.500, "0.001")  # +2 ticks (0.002) -> 0.502
    assert a["price"] == 0.502
    assert _within_dp(a["amount_usd"], 2)
    assert _within_dp(a["size"], 5)            # tick 0.001 → taker up to 5 dp


# ── keys/shape unchanged (execute_buy + dry-run tests depend on them) ────────
def test_return_shape_unchanged():
    a = W.build_order_args("TOK", 11.0, 0.40, "0.01")
    assert set(a.keys()) == {"token_id", "price", "size", "amount_usd", "side"}
    assert a["side"] == "BUY"


# ── FAK orderbook instrumentation (2026-07-06, observability) ─────────────────
class _OBClient:
    def __init__(self, ob, raise_it=False):
        self._ob = ob; self._raise = raise_it
    def get_order_book(self, token_id):
        if self._raise:
            raise RuntimeError("boom")
        return self._ob


def _trader_ob(ob, raise_it=False):
    t = W.__new__(W)
    t.client = _OBClient(ob, raise_it)
    return t


def test_orderbook_snapshot_empty_book():
    assert "NO ASKS" in _trader_ob({"asks": []})._orderbook_snapshot("TOK", 0.52)


def test_orderbook_snapshot_best_ask_and_fillable():
    # asks 0.50(100) fillable, 0.53(200)/0.60(500) above our 0.52 order price → not.
    ob = {"asks": [{"price": "0.60", "size": "500"},
                   {"price": "0.53", "size": "200"},
                   {"price": "0.50", "size": "100"}]}
    s = _trader_ob(ob)._orderbook_snapshot("TOK", 0.52)
    assert "best_ask=0.500" in s and "sz=100.0" in s
    assert "fillable<=px0.520=100.0sh" in s
    assert "n_asks=3" in s


def test_orderbook_snapshot_never_raises():
    assert "unavailable" in _trader_ob({}, raise_it=True)._orderbook_snapshot("TOK", 0.5)
