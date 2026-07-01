#!/bin/bash
# launchd launcher for the :8003 LIVE dashboard (reads ONLY tradingbot_live.db).
export WEATHER_DB_PATH="$HOME/Projects/trooth-weather-live/tradingbot_live.db"
export DASHBOARD_DATA_DIR="$HOME/.local/state/trooth/live-dashboard-empty"
export WEATHER_BOT_URL="http://localhost:8000"
# LIVE view: readiness scorecard counts only real fills (order_id NOT NULL) so
# paper rows can never inflate the live dashboard (audit 5d, 2026-07-01).
export DASHBOARD_LIVE_ONLY=1
mkdir -p "$DASHBOARD_DATA_DIR"
cd "$HOME/Projects/trooth-claude-bot" || exit 1
exec "$HOME/Projects/trooth-weather-live/.venv/bin/uvicorn" dashboard_server.server:app \
  --host 127.0.0.1 --port 8003 --log-level warning
