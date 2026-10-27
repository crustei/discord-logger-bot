#!/usr/bin/env bash
# Foreground run. Reads ./.env if it exists, then starts the bot.
cd "$(dirname "$0")"
if [ -f MIGRATED ] && [ -z "${FORCE_LOCAL:-}" ]; then echo "MIGRATED exists: bot runs on the VM; rm MIGRATED (or FORCE_LOCAL=1) to run here" >&2; sleep 3600; exit 1; fi
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
if [ ! -x .venv/bin/python ]; then  # box resets can drop the venv; rebuild it once
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1 \
    && echo "$(date '+%Y-%m-%d %H:%M:%S') WARNING rebuilt .venv after box reset"
fi
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" bot.py
