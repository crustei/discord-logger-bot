#!/usr/bin/env bash
# Hourly heartbeat for the VM (no LLM). Same output as tools_local/heartbeat_check.sh:
# RUNNING / RESTARTED (...) / DOWN (...), plus a WARN synthetic line when the harness is failing or stale.
set -uo pipefail
. "$(dirname "$0")/config.sh"
MAX_AGE="${MAX_AGE:-1320}"
"${SSH[@]}" --command "sudo env MAX_AGE=$MAX_AGE APP_DIR=$APP_DIR APP_USER=$APP_USER bash -s" <<'R' 2>/dev/null || echo "DOWN (VM unreachable over IAP SSH)"
cd "$APP_DIR"
st() { python3 - "$MAX_AGE" <<'P'
import json, sys, time, datetime as dt
try:
    s = json.load(open("diag/status.json")); age = time.time() - dt.datetime.fromisoformat(s["timestamp"]).timestamp()
except Exception as e:
    print(f"unreadable status.json ({type(e).__name__})"); sys.exit()
print("ok" if age <= float(sys.argv[1]) and s.get("ok") else f"stale or failing ({int(age)}s old, ok={s.get('ok')})")
P
}
syn() { python3 - <<'P'
import json, time, datetime as dt
s = (json.load(open("diag/status.json")) or {}).get("synthetic")
if not s: print("WARN synthetic: no result yet"); raise SystemExit
age = time.time() - dt.datetime.fromisoformat(s["timestamp"]).timestamp()
if age > 2 * 3600 + 600: print(f"WARN synthetic: stale ({int(age // 60)} min since last run)")
elif not s.get("ok"): print(f"WARN synthetic: {s.get('passed')}/{s.get('total')} passed")
P
}
why="$(st)"
if [ "$why" = ok ]; then echo RUNNING; syn; exit 0; fi
sudo -u "$APP_USER" python3 counters.py heartbeat_restarts "$why; systemctl restart" >> bot.log 2>&1
systemctl restart discord-bot
for i in $(seq 15); do sleep 10; [ "$(st)" = ok ] && { echo "RESTARTED ($why)"; exit 0; }; done
echo "DOWN ($why; after restart: $(st))"
R
