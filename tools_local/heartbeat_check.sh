#!/usr/bin/env bash
# Hourly heartbeat (no LLM). Reads diag/status.json written by the bot.
# Prints RUNNING, or restarts through the supervise loop and prints RESTARTED or DOWN <reason>.
cd "$(dirname "$0")/.."
[ -f MIGRATED ] && exec bash deploy/gcp/heartbeat_check_gcp.sh  # bot now runs on the GCP VM
MAX_AGE="${MAX_AGE:-1320}"   # 22 min (in-bot check runs every 10 min)
WAIT="${WAIT:-150}"         # supervisor sleeps 15 s, bot's first light diag runs 60 s after start

status() {  # prints "ok" or a short reason
  [ -f diag/status.json ] || { echo "no status.json"; return; }
  python3 - "$MAX_AGE" <<'P'
import json, sys, time, datetime as dt
try:
    s = json.load(open("diag/status.json"))
    age = time.time() - dt.datetime.fromisoformat(s["timestamp"]).timestamp()
except Exception as e:
    print(f"unreadable status.json ({type(e).__name__})"); sys.exit()
if age > float(sys.argv[1]):
    print(f"stale status.json ({int(age)}s old)")
elif not s.get("ok"):
    print("failing: " + ",".join(k for k, v in s.get("checks", {}).items() if not v.get("ok")))
else:
    print("ok")
P
}

loop_pid() {  # the supervise loop: a bash running "while true; do ./run.sh"
  pgrep -f 'while true; do ./run.sh' | head -1
}

synthetic() {  # prints a WARN line when the external synthetic test failed or is stale; never restarts
  python3 - <<'P'
import json, time, datetime as dt
try:
    s = json.load(open("diag/status.json")).get("synthetic")
except Exception:
    s = None
if not s:
    print("WARN synthetic: no result yet"); raise SystemExit
age = time.time() - dt.datetime.fromisoformat(s["timestamp"]).timestamp()
if age > 2 * 3600 + 600:
    print(f"WARN synthetic: stale ({int(age // 60)} min since last run)")
elif not s.get("ok"):
    bad = [f"{r['name']}: {r['detail']}" for r in s.get("results", []) if not r.get("ok")]
    print(f"WARN synthetic: {s.get('passed')}/{s.get('total')} passed; " + "; ".join(bad)[:500])
P
}

why="$(status)"
if [ "$why" != ok ]; then  # box may have just resumed from a suspend; the bot's check catches up within ~30 s
  sleep "${RESUME_WAIT:-45}"; why="$(status)"
fi
if [ "$why" = ok ]; then echo RUNNING; synthetic; exit 0; fi

if [ -n "$(loop_pid)" ]; then
  pkill -f '[p]ython.*bot\.py'   # supervise loop restarts it after ~15 s
  how="killed bot; supervise loop restarting"
else
  pkill -f '[p]ython.*bot\.py'
  rm -f super.pid
  ./supervise.sh start >/dev/null
  how="supervise loop was missing; started it"
fi
python3 counters.py heartbeat_restarts "$why; $how" >> bot.log 2>&1
start=$(date +%s)
while [ $(( $(date +%s) - start )) -lt "$WAIT" ]; do
  sleep 10
  # fresh status written after the restart?
  if [ "$(status)" = ok ] && [ "$(stat -c %Y diag/status.json)" -ge "$start" ]; then
    echo "RESTARTED ($why; $how)"; exit 0
  fi
done
echo "DOWN ($why; $how; after restart: $(status))"; exit 1
