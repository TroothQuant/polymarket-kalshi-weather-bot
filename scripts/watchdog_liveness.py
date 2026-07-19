#!/usr/bin/env python3
"""Watchdog A (liveness) — external checker, runs OUTSIDE the bot process via a
systemd timer so it catches a crashed/hung bot while the box is up. Alerts via
ntfy on ANY failure. 2026-07-09 (JOB 1).

Checks: (1) trooth-weather-live / trooth-live-dashboard / cloudflared-trooth-vps
all systemd-active; (2) bot_state.last_run < 15 min old (heartbeat writes it every
minute); (3) the :8003 live dashboard returns HTTP 200.

Limitation: if the whole BOX is down the timer can't fire — box-down detection
needs an OFF-box monitor (a future add; e.g. an uptime service watching ntfy or
:8003). This catches the far more common bot-crash-while-box-up case.
"""
import os
import subprocess
import sqlite3
import sys
import urllib.request
from datetime import datetime

TOPIC = os.environ.get("NTFY_TOPIC", "trooth-live-120d6426e0")
NTFY = f"https://ntfy.sh/{TOPIC}"
DB = "/root/trooth-weather-live/tradingbot_live.db"
UNITS = ("trooth-weather-live", "trooth-live-dashboard", "cloudflared-trooth-vps")


def push(title, msg, prio="urgent", tags="rotating_light"):
    try:
        # HTTP headers are latin-1 — keep Title/Tags ASCII-safe.
        def _a(s):
            return str(s).encode("ascii", "replace").decode("ascii")
        r = urllib.request.Request(NTFY, data=msg.encode("utf-8"), method="POST")
        r.add_header("Title", _a(title))
        r.add_header("Priority", _a(prio))
        r.add_header("Tags", _a(tags))
        urllib.request.urlopen(r, timeout=8)
    except Exception:
        pass


def is_active(unit):
    return subprocess.run(["systemctl", "is-active", unit],
                          capture_output=True, text=True).stdout.strip() == "active"


ENV_FILE = "/root/.config/trooth/weather-live-mac.env"


OPERATOR_ALLOWLIST = "/root/.config/trooth/operator_tokens.txt"
RECON_ALERT_STATE = "/root/.local/state/trooth/reconcile_alert.json"


def _read_operator_allowlist():
    """Tokens the operator has marked as their own manual positions (one token id
    per line; '#' comments ok). Classifies as 'operator-manual', not truly-unknown."""
    try:
        return {ln.split("#")[0].strip() for ln in open(OPERATOR_ALLOWLIST)
                if ln.split("#")[0].strip()}
    except Exception:
        return set()


def reconcile_alert_check():
    """ALERT-ONLY reconcile-divergence monitor (2026-07-19, downgraded from auto-
    pause — which false-fired twice on benign divergences). NEVER pauses. Classifies
    every on-chain-not-recorded token as redeemable-win / operator-manual /
    TRULY-UNKNOWN and RE-PUSHES HOURLY while unresolved (like watchdog D), so a real
    unrecorded bot fill (truly-unknown) can never hide as one missed push. Clears
    its state when the divergence resolves. Never raises."""
    try:
        import sys, time, json
        if "/root/trooth-weather-live" not in sys.path:
            sys.path.insert(0, "/root/trooth-weather-live")
        from backend.config import settings
        from backend.core.scheduler import _fetch_onchain_positions
        funder = getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)
        if not funder:
            return
        onchain = set(_fetch_onchain_positions(funder).keys())
    except Exception as e:
        push("Weather LIVE reconcile check FAILED", f"could not fetch on-chain to classify divergence: {e}",
             prio="high", tags="warning")
        return

    def _save(state):
        try:
            os.makedirs(os.path.dirname(RECON_ALERT_STATE), exist_ok=True)
            json.dump(state, open(RECON_ALERT_STATE, "w"))
        except Exception:
            pass
    def _load():
        try:
            return json.load(open(RECON_ALERT_STATE))
        except Exception:
            return {}

    try:
        c = sqlite3.connect(DB, timeout=8)
        recorded = {r[0] for r in c.execute("SELECT token_id FROM trades WHERE result='pending' AND token_id IS NOT NULL")}
        won = {r[0] for r in c.execute("SELECT token_id FROM trades WHERE result='win' AND token_id IS NOT NULL")}
        c.close()
    except Exception:
        return
    allow = _read_operator_allowlist()
    divergent = onchain - recorded
    if not divergent:
        _save({})  # resolved → clear
        return
    classes = []
    unknown = 0
    for tok in sorted(divergent):
        if tok in won:
            classes.append(("redeemable-win", tok))
        elif tok in allow:
            classes.append(("operator-manual", tok))
        else:
            classes.append(("TRULY-UNKNOWN", tok)); unknown += 1
    summary = "/".join(sorted({cl for cl, _ in classes}))
    now = time.time()
    st = _load()
    due = (st.get("summary") != summary) or (now - st.get("last", 0) >= 3600)
    if due:
        prio = "urgent" if unknown else "high"
        body = "; ".join(f"{cl} {tok[:14]}…" for cl, tok in classes)
        note = (" ⚠ TRULY-UNKNOWN = possible UNRECORDED BOT FILL — investigate now."
                if unknown else " (benign; close/redeem or allowlist to clear).")
        push(f"Weather LIVE reconcile divergence: {summary}",
             f"{len(divergent)} on-chain position(s) not recorded — {body}.{note} "
             f"Alert-only (bot NOT paused); re-pushes hourly until resolved.",
             prio=prio, tags="warning")
        _save({"summary": summary, "last": now})


def main():
    reconcile_alert_check()
    fails = []
    for u in UNITS:
        if not is_active(u):
            fails.append(f"{u} NOT active")
    try:
        c = sqlite3.connect(DB, timeout=8)
        row = c.execute("SELECT last_run FROM bot_state LIMIT 1").fetchone()
        c.close()
        lr = row[0] if row else None
        if not lr:
            fails.append("bot_state.last_run is NULL")
        else:
            last = datetime.fromisoformat(str(lr).replace("Z", ""))
            age_min = (datetime.utcnow() - last).total_seconds() / 60.0
            if age_min > 15:
                fails.append(f"last_run {age_min:.0f} min ago (>15)")
    except Exception as e:
        fails.append(f"DB read failed: {e}")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8003/", timeout=8) as r:
            if getattr(r, "status", 200) != 200:
                fails.append(f":8003 HTTP {r.status}")
    except Exception as e:
        fails.append(f":8003 unreachable: {e}")

    if fails:
        push("Weather LIVE watchdog: FAILURE", " | ".join(fails))
        print("FAIL:", fails)
        sys.exit(1)
    print("OK: all liveness checks passed")


if __name__ == "__main__":
    main()
