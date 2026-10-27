"""Consistent online copy of bot.db via the sqlite3 backup API (safe while the bot is writing; no raw WAL copy)."""
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
d = sqlite3.connect(dst)
s.backup(d, pages=4096)
d.execute("PRAGMA journal_mode=DELETE"); d.commit()
ok = d.execute("PRAGMA quick_check").fetchone()[0]
s.close(); d.close()
print(f"backup {dst}: quick_check={ok}")
sys.exit(0 if ok == "ok" else 1)
