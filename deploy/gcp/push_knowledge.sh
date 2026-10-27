#!/usr/bin/env bash
# Push box knowledge/ to the VM: add/update only (never deletes), mtimes preserved.
# No restart: the bot re-indexes changed files on its next search (refresh_docs); we warm it now.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
tar -C "$LOCAL_DIR" -czf "$tmp/kn.tgz" knowledge
"${SCP[@]}" "$tmp/kn.tgz" "$VM:/tmp/kn.tgz" 2>&1 | grep -v -i "numpy\|increasing_the_tcp\|^WARNING: *$\|^$" || true
"${SSH[@]}" --command "set -e; sudo -u $APP_USER tar -C $APP_DIR -xzf /tmp/kn.tgz --no-same-owner; rm -f /tmp/kn.tgz
cd $APP_DIR && sudo -u $APP_USER bash -c 'set -a; . ./.env; set +a; .venv/bin/python -c \"
import db, retrieve, os
c = db.connect(os.environ[\\\"BOT_DB\\\"]); retrieve.refresh_docs(c, os.environ[\\\"DOCS_DIR\\\"]); print(\\\"reindexed\\\")\"'" 2>&1 | grep -v -i "numpy\|increasing_the_tcp\|^WARNING: *$\|^$"
echo "knowledge pushed ($(find "$LOCAL_DIR/knowledge" -type f | wc -l) files on box)"
