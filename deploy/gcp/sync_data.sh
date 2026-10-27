#!/usr/bin/env bash
# Copies bot.db (online backup), knowledge/, data/, diag/, and .env to the VM. .env is never printed.
set -euo pipefail
. "$(dirname "$0")/config.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
"$LOCAL_DIR/.venv/bin/python" "$(dirname "$0")/backup_db.py" "$LOCAL_DIR/bot.db" "$tmp/bot.db"
if [ -n "${FINAL:-}" ]; then DIRS="data diag"; else DIRS="knowledge data diag config"; fi
tar -C "$LOCAL_DIR" -czf "$tmp/data.tgz" $DIRS bot.log $( [ -d logs ] && echo logs )
umask 077; sed "s#$LOCAL_DIR#$APP_DIR#g" "$LOCAL_DIR/.env" > "$tmp/env"
# Secrets the box injects via the environment instead of .env (never printed).
for k in DISCORD_BOT_TOKEN; do
  grep -q "^$k=" "$tmp/env" || { [ -n "${!k:-}" ] && printf '%s=%s\n' "$k" "${!k}" >> "$tmp/env"; }
done
"${SCP[@]}" "$tmp/bot.db" "$tmp/data.tgz" "$tmp/env" "$VM:/tmp/"
"${SSH[@]}" --command "set -e; sudo mkdir -p $APP_DIR; sudo tar -C $APP_DIR -xzf /tmp/data.tgz; \
  sudo rm -f $APP_DIR/bot.db-wal $APP_DIR/bot.db-shm; sudo mv /tmp/bot.db $APP_DIR/bot.db; \
  sudo install -m 600 /tmp/env $APP_DIR/.env; rm -f /tmp/env /tmp/data.tgz; \
  (id $APP_USER >/dev/null 2>&1 && sudo chown -R $APP_USER:$APP_USER $APP_DIR || true)"
echo "data synced (db $(du -h "$tmp/bot.db" | cut -f1))"
