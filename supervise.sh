#!/usr/bin/env bash
# Restart-on-crash loop. Start: ./supervise.sh start   Stop: ./supervise.sh stop
cd "$(dirname "$0")"
case "$1" in
 start) [ -f super.pid ] && kill -0 "$(cat super.pid)" 2>/dev/null && { echo "already running"; exit 0; }
   nohup bash -c 'while true; do ./run.sh; echo "[supervise] bot exited $? at $(date), restarting in 15s"; sleep 15; done' >> bot.log 2>&1 &
   echo $! > super.pid; echo "started supervisor pid $(cat super.pid)";;
 stop) [ -f super.pid ] && { pkill -P "$(cat super.pid)"; kill "$(cat super.pid)"; rm -f super.pid; }; pkill -f "python bot.py" ; echo stopped;;
 status) [ -f super.pid ] && kill -0 "$(cat super.pid)" 2>/dev/null && echo running || echo stopped;;
esac
