#!/bin/bash
set -e
cd "$HOME/Projects/trooth-weather-bot"
YESTERDAY_UTC=$(date -u -v-1d +%Y-%m-%d)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running calibration for $YESTERDAY_UTC"
exec .venv/bin/python scripts/kalshi_eod_calibration_2026-05-20.py --date "$YESTERDAY_UTC"
