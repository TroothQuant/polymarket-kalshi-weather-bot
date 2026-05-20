#!/usr/bin/env python3
"""Run the trading bot backend server."""
import os
import subprocess
import sys
import uvicorn
from backend.config import settings
from backend.models.database import init_db

# Audit 2026-05-19 CRITICAL #1, hardened on 2026-05-20 after a Claude bot
# zombie (PID 74431) was discovered: refuse to launch if another weather
# bot copy is alive. Closes the race-condition loop at the source.
_SELF_SIGNATURE = r"python.*run\.py($| )"


def _refuse_if_another_bot_alive() -> None:
    """Exit if another weather bot is already running.

    Matches `python run.py` (the run_backend.sh invocation). Returns silently
    when this process is the only match. Skipped if pgrep is unavailable.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", _SELF_SIGNATURE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    own = str(os.getpid())
    others = [p for p in result.stdout.split() if p.strip() and p.strip() != own]
    if not others:
        return

    print("=" * 70, file=sys.stderr)
    print("REFUSED — another weather bot instance is already running.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"Found PID(s): {', '.join(others)}", file=sys.stderr)
    print(file=sys.stderr)
    print("Stop the running copy first, then re-launch.", file=sys.stderr)
    print(f"  kill {' '.join(others)}", file=sys.stderr)
    print("Verify:", file=sys.stderr)
    print(f"  pgrep -f '{_SELF_SIGNATURE}' || echo '(no bot running)'", file=sys.stderr)
    print("  lsof -ti:8000 || echo '(port 8000 free)'", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    _refuse_if_another_bot_alive()
    print("Initializing database...")
    init_db()

    # Audit 2026-05-19 CRITICAL #2: bind to settings.API_HOST (default
    # 127.0.0.1) so the mutating endpoints aren't reachable from the LAN.
    # HOST env wins if set, then settings, then last-resort 127.0.0.1.
    host = os.environ.get("HOST", settings.API_HOST or "127.0.0.1")
    port = int(os.environ.get("PORT", settings.API_PORT))

    print(f"Starting server on http://{host}:{port}")
    print(f"API docs available at http://localhost:{port}/docs")
    if settings.API_AUTH_TOKEN:
        print("API_AUTH_TOKEN is set — mutating POSTs require "
              "Authorization: Bearer <token>.")
    else:
        print("API_AUTH_TOKEN is NOT set — mutating POSTs are open to any "
              "local request. Set API_AUTH_TOKEN in .env to require auth.")

    uvicorn.run(
        "backend.api.main:app",
        host=host,
        port=port,
        reload=os.environ.get("RAILWAY_ENVIRONMENT") is None
    )
