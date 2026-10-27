#!/usr/bin/env bash
# Hourly synthetic harness loop (separate process from the bot). Started by ./supervise.sh start.
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
exec .venv/bin/python tools_local/synthetic_test.py --loop >/dev/null 2>>bot.log
