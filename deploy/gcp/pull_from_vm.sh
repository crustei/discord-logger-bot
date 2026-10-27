#!/usr/bin/env bash
# Read-only mirror of the VM bot's state onto the box (no LLM, no restart):
#   vm_mirror/bot.db (sqlite backup-API copy), vm_mirror/bot.log, vm_mirror/status.json, vm_mirror/counters.json
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
OUT="${OUT:-$LOCAL_DIR/vm_mirror}"; mkdir -p "$OUT"
"${SSH[@]}" --command "set -e; T=\$(mktemp -d); sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/deploy/gcp/backup_db.py $APP_DIR/bot.db /tmp/mirror_bot.db >/dev/null
sudo mv /tmp/mirror_bot.db \$T/bot.db; sudo cp $APP_DIR/bot.log $APP_DIR/diag/status.json $APP_DIR/diag/counters.json \$T/
sudo tar -C \$T -czf /tmp/vm_mirror.tgz .; sudo chown \$USER /tmp/vm_mirror.tgz; sudo rm -rf \$T" 2>&1 | grep -v -i "numpy\|increasing_the_tcp\|^WARNING: *$\|^$" || true
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
"${SCP[@]}" "$VM:/tmp/vm_mirror.tgz" "$tmp/" 2>&1 | grep -v -i "numpy\|increasing_the_tcp\|^WARNING: *$\|^$" || true
"${SSH[@]}" --command "rm -f /tmp/vm_mirror.tgz" >/dev/null 2>&1 || true
chmod -f u+w "$OUT"/* 2>/dev/null || true
tar -C "$OUT" -xzf "$tmp/vm_mirror.tgz"
chmod 444 "$OUT"/bot.db "$OUT"/bot.log "$OUT"/status.json "$OUT"/counters.json
date '+%Y-%m-%d %H:%M:%S %Z' > "$OUT/PULLED_AT"
echo "mirrored to $OUT: $(du -h "$OUT/bot.db" | cut -f1) db, $(wc -l < "$OUT/bot.log") log lines"
