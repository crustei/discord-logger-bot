#!/usr/bin/env bash
# Restart-on-crash loop. Start: ./supervise.sh start   Stop: ./supervise.sh stop
cd "$(dirname "$0")"
case "$1" in
 start) [ -f super.pid ] && kill -0 "$(cat super.pid)" 2>/dev/null && { echo "already running"; exit 0; }
   p=$(pgrep -f 'while true; do ./run.sh' | head -1); [ -n "$p" ] && { echo "$p" > super.pid; echo "already running (pid $p)"; exit 0; }
   nohup bash -c 'while true; do ./run.sh; echo "[supervise] bot exited $? at $(date), restarting in 15s"; sleep 15; done' >> bot.log 2>&1 &
   echo $! > super.pid; echo "started supervisor pid $(cat super.pid)"
   pgrep -f 'while true; do tools_local/synthetic_run.sh' >/dev/null || \
     nohup bash -c 'while true; do tools_local/synthetic_run.sh; sleep 60; done' >/dev/null 2>&1 &
   ;;
 synth) pgrep -f 'while true; do tools_local/synthetic_run.sh' >/dev/null && { echo "synthetic loop already running"; exit 0; }
   nohup bash -c 'while true; do tools_local/synthetic_run.sh; sleep 60; done' >/dev/null 2>&1 &
   echo "started synthetic loop";;
 stop) pkill -f 'while true; do tools_local/synthetic_run.sh'; pkill -f 'synthetic_test.py --loop'
   [ -f super.pid ] && { pkill -P "$(cat super.pid)"; kill "$(cat super.pid)"; rm -f super.pid; }; pkill -f "python bot.py" ; echo stopped;;
 status) [ -f super.pid ] && kill -0 "$(cat super.pid)" 2>/dev/null && echo running || echo stopped;;
esac
