#!/usr/bin/env bash
# Moves the live bot from the box to the VM. Downtime ~1-3 min (final DB copy).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
cd "$LOCAL_DIR"
echo "1/5 sync code"; "$HERE/sync_code.sh"
touch MIGRATED  # box heartbeat now checks the VM; run.sh refuses to start here
date "+stop %T"; echo "2/5 stop box bot + harness"; ./supervise.sh stop || true; sleep 3
pgrep -f '[p]ython bot.py' && { echo "box bot still running; aborting"; exit 1; }
echo "3/5 final DB backup + data copy"; FINAL=1 "$HERE/sync_data.sh"
echo "4/5 start VM bot"
"${SSH[@]}" --command "sudo rm -f $APP_DIR/STANDBY && sudo systemctl restart discord-bot && sudo systemctl start discord-bot-synthetic.timer discord-bot-weekly.timer"
date "+start %T"; echo "5/5 verify (waiting 90 s for the first check)"; sleep 90
"$HERE/heartbeat_check_gcp.sh"
echo "Rollback if needed: ssh in, 'sudo systemctl disable --now discord-bot', then on the box 'rm MIGRATED && ./supervise.sh start'."
