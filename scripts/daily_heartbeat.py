#!/usr/bin/env python3
"""Daily live heartbeat (proof-of-life) — MX box pushes live state to ntfy once a
day (13:00 UTC via timer). 7 lines expected across the away week. Read-only.
Paper-vs-live PARITY comparison is the return review; this is live proof-of-life."""
import os, sqlite3, urllib.request, datetime
DB = "/root/trooth-weather-live/tradingbot_live.db"
TOPIC = os.environ.get("NTFY_TOPIC", "trooth-live-120d6426e0")

def push(title, msg, tags="heartbeat"):
    try:
        r = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode("utf-8"), method="POST")
        r.add_header("Title", str(title).encode("ascii","replace").decode("ascii"))
        r.add_header("Priority", "default"); r.add_header("Tags", tags)
        urllib.request.urlopen(r, timeout=10); return True
    except Exception: return False

def main():
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    bank = c.execute("SELECT bankroll FROM bot_state LIMIT 1").fetchone()[0]
    op = c.execute("SELECT COUNT(*) n FROM trades WHERE result='pending'").fetchone()["n"]
    exposure = c.execute("SELECT COALESCE(SUM(size),0) s FROM trades WHERE result='pending'").fetchone()["s"]
    fills_today = c.execute("SELECT COUNT(*) n FROM trades WHERE substr(timestamp,1,10)=? AND order_id IS NOT NULL", (today,)).fetchone()["n"]
    settled_today = c.execute("SELECT COUNT(*) n FROM trades WHERE substr(settlement_time,1,10)=?", (today,)).fetchone()["n"]
    recon = c.execute("SELECT reconcile_status FROM bot_state LIMIT 1").fetchone()["reconcile_status"] or "ok"
    c.close()
    push(f"TROOTH live heartbeat {today}",
         f"bankroll ${bank:.2f} | open {op} (${exposure:.2f} exp) | fills today {fills_today} | "
         f"settled today {settled_today} | reconcile {recon}")
    print("heartbeat sent")

if __name__ == "__main__": main()
