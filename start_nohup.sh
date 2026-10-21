#!/usr/bin/env bash
# Background run with nohup; logs to bot.log, pid in bot.pid. Stop: kill $(cat bot.pid)
cd "$(dirname "$0")"; nohup ./run.sh >> bot.log 2>&1 & echo $! > bot.pid; echo "started pid $(cat bot.pid)"
