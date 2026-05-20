"""Refuse to mutate the weather bot's DB while the live backend is running.

Used by every cleanup / migration script that writes to tradingbot.db.
Without this guard, the backend's scheduler jobs (heartbeat every 60s,
scan every few minutes) can overwrite or race with the script's writes.

Audit 2026-05-19, CRITICAL #1.
"""
from __future__ import annotations

import subprocess
import sys

# Match the weather bot's entry point. `scripts/run_backend.sh` execs
# `python run.py` (the .venv/bin/python from the project venv), which boots
# uvicorn + APScheduler in the same process. The signature below matches
# any python invocation whose argv ends with run.py — specific enough that
# unrelated processes named "run" don't trip the guard.
_BOT_SIGNATURE = r"python.*run\.py($| )"


def _own_pid() -> str:
    import os
    return str(os.getpid())


def refuse_if_bot_running(*, signature: str = _BOT_SIGNATURE) -> None:
    """Exit the calling script if the live weather bot is still alive.

    Prints a clear, copy-pasteable instruction for stopping the bot and
    re-running the script. Returns silently when no live bot is detected.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", signature],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        print("WARNING: pgrep not found; race guard skipped.", file=sys.stderr)
        return
    except subprocess.TimeoutExpired:
        print("WARNING: pgrep timed out; race guard skipped.", file=sys.stderr)
        return

    own = _own_pid()
    pids = [p for p in result.stdout.split() if p.strip() and p.strip() != own]
    if not pids:
        return

    print("=" * 70)
    print("REFUSED — the live weather bot is still running.")
    print("=" * 70)
    print(f"Found process(es) matching '{signature}': {', '.join(pids)}")
    print()
    print("Stop the bot first, then re-run this script.")
    print()
    print("To stop the bot, run in your bot's terminal tab:")
    print(f"  kill {' '.join(pids)}")
    print()
    print("If that doesn't take, force it:")
    print(f"  kill -9 {' '.join(pids)}")
    print()
    print("To verify the bot is fully down, run:")
    print(f"  ps aux | grep -v grep | grep '{signature}' || echo '(no bot running)'")
    print("  lsof -ti:8000 || echo '(port 8000 free)'")
    print()
    print("Then re-run this script.")
    sys.exit(2)
