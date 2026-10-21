#!/usr/bin/env bash
# Foreground run. Reads ./.env if it exists, then starts the bot.
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" bot.py
