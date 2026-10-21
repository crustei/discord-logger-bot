"""Import DiscordChatExporter JSON exports (dedupe by message id).

Channels on the blocklist in config/settings.toml are skipped, then purged.
An empty blocklist skips nothing. Usage: python backfill.py [paths or dirs ...]
With no arguments, JSON files are read from ./exports. Set EXPORTS_DIR to use
another directory.
"""
import glob, json, os, sys
from db import connect, purge_excluded, upsert_message
DEFAULT = ["./exports"]

def export_roots():
    raw = os.environ.get("EXPORTS_DIR", "").strip()
    return [raw] if raw else list(DEFAULT)

def files(args):
    out = []
    for a in args or export_roots():
        for p in glob.glob(a):
            out += sorted(glob.glob(os.path.join(p, "*.json"))) if os.path.isdir(p) else [p]
    return out
def main(args):
    c = connect(); before = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]; seen = 0; nf = 0; skipped = 0
    for f in files(args):
        try: d = json.load(open(f, encoding="utf-8"))
        except Exception: continue
        if not isinstance(d, dict) or "messages" not in d or "channel" not in d: continue
        nf += 1; g, ch = d.get("guild", {}), d["channel"]
        for m in d["messages"]:
            a = m.get("author", {}); seen += 1
            stored = upsert_message(c, dict(id=m["id"], guild_id=g.get("id"), channel_id=ch.get("id"), channel_name=ch.get("name"),
                category=ch.get("category"), author_id=a.get("id"), author_name=a.get("name"),
                display_name=a.get("nickname") or a.get("name"), roles=[r["name"] for r in a.get("roles", [])],
                timestamp=m.get("timestamp"), edited_timestamp=m.get("timestampEdited"), content=m.get("content"),
                attachments=[x.get("url") for x in m.get("attachments", [])], source="export"))
            if not stored: skipped += 1
    purged = purge_excluded(c)
    c.commit(); after = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"files={nf} messages_read={seen} skipped_excluded={skipped} purged={purged} new_rows={after-before} total_in_db={after}")
if __name__ == "__main__": main(sys.argv[1:])
