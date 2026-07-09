"""Push notifications via ntfy.sh (private topic). Best-effort — NEVER raises into
the caller (a failed alert must never take down the live bot). Subscribe to the
topic in the ntfy mobile/desktop app to receive alerts. Added 2026-07-09 (JOB 1:
alert channel for watchdogs A/B/C). Topic is overridable via the NTFY_TOPIC env.
"""
import logging
import os
import urllib.request

log = logging.getLogger("trading_bot")

NTFY_BASE = os.environ.get("NTFY_BASE", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "trooth-live-120d6426e0")


def notify_push(title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """POST a push alert to the private ntfy topic. Returns True on success, False
    on any failure (logged, never raised). priority: min|low|default|high|urgent.
    tags: comma-separated ntfy emoji shortcodes (e.g. 'warning,moneybag')."""
    if not NTFY_TOPIC:
        return False
    try:
        # HTTP headers are latin-1; ntfy Title/Tags must be ASCII-safe (a unicode
        # em-dash etc. would raise on send). The message BODY stays full UTF-8.
        def _ascii(s):
            return str(s).encode("ascii", "replace").decode("ascii")
        req = urllib.request.Request(
            f"{NTFY_BASE}/{NTFY_TOPIC}",
            data=message.encode("utf-8"), method="POST")
        req.add_header("Title", _ascii(title))
        if priority:
            req.add_header("Priority", _ascii(priority))
        if tags:
            req.add_header("Tags", _ascii(tags))
        urllib.request.urlopen(req, timeout=8)
        return True
    except Exception as e:  # noqa: BLE001 — best-effort, must not raise
        log.warning(f"ntfy push failed ({e}) — alert NOT delivered: {title}")
        return False
