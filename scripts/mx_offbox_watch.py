#!/usr/bin/env python3
"""Off-box liveness watch — the NYC PAPER server watches the MX LIVE box.

Closes the box-DOWN blind spot (2026-07-09): the MX-local watchdog A can't report
its own host dying. A healthy MX tunnel returns HTTP 302 (Cloudflare Access login)
from bot.troothquant.com; 502/530/000/timeout = MX box or tunnel DOWN. On 3
consecutive failures → ntfy CRITICAL, then re-alert at most hourly (no spam), and
a RECOVERED note when 302 returns. Runs on trooth-server via a 5-min systemd timer.
"""
import json
import os
import subprocess
import time
import urllib.request

STATE = "/home/trooth/.local/state/trooth/mx_offbox_watch.json"
TOPIC = os.environ.get("NTFY_TOPIC", "trooth-live-120d6426e0")
URL = os.environ.get("MX_WATCH_URL", "https://bot.troothquant.com/")
HEALTHY_CODE = "302"
THRESHOLD = 3
REALERT_SEC = 3600


def http_code():
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-m", "15", "-w", "%{http_code}", URL],
            capture_output=True, text=True, timeout=25)
        return r.stdout.strip() or "000"
    except Exception:
        return "000"


def push(title, msg, tags="rotating_light"):
    try:
        rq = urllib.request.Request(f"https://ntfy.sh/{TOPIC}",
                                    data=msg.encode("utf-8"), method="POST")
        rq.add_header("Title", str(title).encode("ascii", "replace").decode("ascii"))
        rq.add_header("Priority", "urgent")
        rq.add_header("Tags", tags)
        urllib.request.urlopen(rq, timeout=10)
        return True
    except Exception:
        return False


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"fails": 0, "last_alert": 0}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"))


def main():
    st = load()
    code = http_code()
    healthy = (code == HEALTHY_CODE)
    now = time.time()
    if healthy:
        if st.get("fails", 0) >= THRESHOLD:
            push("MX box RECOVERED", "bot.troothquant.com is 302 again — MX live box reachable.",
                 tags="white_check_mark")
        st = {"fails": 0, "last_alert": 0}
    else:
        st["fails"] = st.get("fails", 0) + 1
        first_trip = st["fails"] == THRESHOLD
        due = (now - st.get("last_alert", 0)) >= REALERT_SEC
        if st["fails"] >= THRESHOLD and (first_trip or due):
            push("MX LIVE box UNREACHABLE (CRITICAL)",
                 f"bot.troothquant.com returned {code} (expected 302), {st['fails']}x "
                 f"consecutive. MX live box or tunnel DOWN — the MX-local watchdogs "
                 f"cannot self-report. Check the box.")
            st["last_alert"] = now
    save(st)
    print(f"code={code} healthy={healthy} fails={st['fails']}")


if __name__ == "__main__":
    main()
