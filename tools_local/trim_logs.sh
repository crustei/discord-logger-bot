#!/usr/bin/env bash
# Weekly manual trim (not scheduled). Moves bot.log lines older than 7 days
# into logs/archive/<name>-<date>.gz; keeps newer lines in place.
# Lines without a timestamp (tracebacks) follow the timestamped line above them.
set -euo pipefail
[ -f "$(dirname "$0")/../MIGRATED" ] && { echo "bot runs on the VM; trim runs there (discord-bot-weekly.timer)"; exit 0; }
cd "$(dirname "$0")/.."
DAYS="${DAYS:-7}"
cutoff="$(date -d "-$DAYS days" '+%Y-%m-%d %H:%M:%S')"
mkdir -p logs/archive
stamp="$(date '+%Y%m%d-%H%M%S')"
for f in bot.log; do
  [ -s "$f" ] || continue
  c="$cutoff"
  old="$(mktemp)"; new="$(mktemp)"
  awk -v c="$c" -v O="$old" -v N="$new" '
    /^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}/ { old = (substr($0,1,19) < c) }
    { print > (old ? O : N) }' "$f"
  n=$(wc -l < "$old")
  if [ "$n" -gt 0 ]; then
    gzip -c "$old" > "logs/archive/$(basename "$f" .log)-$stamp.gz"
    # rewrite in place (same inode) so the running bot's open handle keeps working
    cat "$new" > "$f"
    echo "$f: archived $n line(s) to logs/archive/$(basename "$f" .log)-$stamp.gz"
  else
    echo "$f: nothing older than $DAYS days"
  fi
  rm -f "$old" "$new"
done

# Weekly DB maintenance (keeps the DB compact so it can grow within the 30 GB free disk).
# ANALYZE + optimize always; full VACUUM only when >20% of pages are free and there is room for a copy.
if [ -f bot.db ]; then
  PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
  "$PY" - <<'P' || echo "db maintenance skipped (busy)"
import os, shutil, sqlite3
c = sqlite3.connect("bot.db", timeout=60)
c.execute("PRAGMA busy_timeout=60000")
c.execute("ANALYZE"); c.execute("PRAGMA optimize")
pages, free = c.execute("PRAGMA page_count").fetchone()[0], c.execute("PRAGMA freelist_count").fetchone()[0]
size = os.path.getsize("bot.db")
did = ""
if pages and free / pages > 0.2 and shutil.disk_usage(".").free > 2 * size:
    c.execute("VACUUM"); did = ", vacuumed"
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
print(f"db: {size // 2**20} MB, {100 * free // max(pages, 1)}% free pages, analyzed{did}")
P
fi
