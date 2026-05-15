#!/usr/bin/env bash
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec python run.py
