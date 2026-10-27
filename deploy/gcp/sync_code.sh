#!/usr/bin/env bash
# Copies code (no secrets, no DB, no venv) to the VM. Safe to re-run.
set -euo pipefail
. "$(dirname "$0")/config.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
tar -C "$LOCAL_DIR" -czf "$tmp/code.tgz" --exclude=.git --exclude=.venv --exclude=.env --exclude='.env.*' \
  --exclude='bot.db*' --exclude=bot.log --exclude=bot.lock --exclude=STANDBY --exclude=MIGRATED --exclude=vm_mirror --exclude=super.pid --exclude=__pycache__ \
  --exclude=logs --exclude=diag --exclude=knowledge --exclude=data .
"${SCP[@]}" "$tmp/code.tgz" "$VM:/tmp/code.tgz"
"${SSH[@]}" --command "sudo mkdir -p $APP_DIR && sudo tar -C $APP_DIR -xzf /tmp/code.tgz && rm /tmp/code.tgz && (id $APP_USER >/dev/null 2>&1 && sudo chown -R $APP_USER:$APP_USER $APP_DIR || true)"
echo "code synced"
