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


def tripwire_check():
    """Auto-PAUSE the live book on a CONFIRMED reconcile divergence (the F3
    reconcile already grace-filters transient data-api lag, so a non-'ok' status =
    real-position accounting is wrong). Don't keep trading unattended on bad
    accounting. Safe-side: a false pause loses trades, never money. Fires once
    (no-op once already paused). 2026-07-10 (away-week tripwire)."""
    try:
        live = any(ln.strip() == "WEATHER_LIVE_TRADING=true" for ln in open(ENV_FILE))
    except Exception:
        return
    if not live:
        return  # already paused — nothing to trip
    try:
        c = sqlite3.connect(DB, timeout=8)
        row = c.execute("SELECT reconcile_status FROM bot_state LIMIT 1").fetchone()
        c.close()
        recon = row[0] if row else None
    except Exception:
        return
    if recon and recon != "ok":
        subprocess.run(["sed", "-i",
                        "s/^WEATHER_LIVE_TRADING=true$/WEATHER_LIVE_TRADING=false/", ENV_FILE])
        subprocess.run(["systemctl", "restart", "trooth-weather-live"])
        push("TRIPWIRE: reconcile divergence - LIVE PAUSED",
             f"reconcile_status='{recon}' → set WEATHER_LIVE_TRADING=false + restarted. "
             f"Real-position accounting is off; investigate before re-enabling.",
             prio="urgent", tags="rotating_light")


def main():
    tripwire_check()
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
