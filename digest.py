"""Write a digest of the last N hours to digests/YYYY-MM-DD.md and .json.
Usage: python digest.py [--hours 24] [--end ISO8601]   Heuristic (keyword) extraction, no LLM."""
import argparse, collections, datetime as dt, json, os, re
from config import excluded_channels
from db import connect, is_opted_out
from permissions import is_excluded
from voice import get_runtime
HERE = os.path.dirname(os.path.abspath(__file__))
DECISION = re.compile(r"\b(decid|decision|agreed|approved|we will|we're going|final(ized)?|confirmed|going with|plan is)\w*", re.I)
ANNOUNCE = re.compile(r"(@everyone|@here|\bannounc\w*|\breminder\b|\bdeadline\b|\bdue\b|\bmeeting\b|\bGM\b)", re.I)
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--hours", type=float, default=24); ap.add_argument("--end")
    a = ap.parse_args()
    end = dt.datetime.fromisoformat(a.end) if a.end else dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None: end = end.astimezone()
    start = end - dt.timedelta(hours=a.hours)
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE deleted=0 ORDER BY timestamp")]
    rows = [r for r in rows if r["timestamp"] and start <= dt.datetime.fromisoformat(r["timestamp"]) <= end]
    rows = [r for r in rows if not is_opted_out(conn, r.get("author_id"))]
    conn.close()
    blocklist = excluded_channels()
    rows = [r for r in rows if not is_excluded(r["channel_id"], r["channel_name"], blocklist, r.get("guild_id"))]
    chans = collections.OrderedDict()
    for r in rows:
        c = chans.setdefault(r["channel_name"], {"category": r["category"], "count": 0, "active": collections.Counter(), "decisions": [], "announcements": [], "messages": []})
        c["count"] += 1; c["active"][r["display_name"]] += 1
        item = {"id": r["id"], "ts": r["timestamp"], "author": r["display_name"], "roles": json.loads(r["roles"]), "content": r["content"], "attachments": json.loads(r["attachments"])}
        c["messages"].append(item); txt = r["content"] or ""
        if DECISION.search(txt): c["decisions"].append(item)
        if ANNOUNCE.search(txt) or "announce" in (r["channel_name"] or ""): c["announcements"].append(item)
    os.makedirs(os.path.join(HERE, "digests"), exist_ok=True)
    day = end.astimezone().strftime("%Y-%m-%d"); base = os.path.join(HERE, "digests", day)
    short = lambda s: (s or "").replace("\n", " ")[:240]
    title = get_runtime().display_title
    md = [f"# {title} digest {day}", f"Window: {start.astimezone():%Y-%m-%d %H:%M} to {end.astimezone():%Y-%m-%d %H:%M %Z} ({a.hours:g}h). {len(rows)} messages in {len(chans)} channels.", ""]
    for name, c in sorted(chans.items(), key=lambda kv: -kv[1]["count"]):
        md += [f"## #{name} ({c['category'] or '-'}) - {c['count']} msgs", "Active: " + ", ".join(f"{n} ({k})" for n, k in c["active"].most_common(10))]
        for label, key in (("Key decisions", "decisions"), ("Announcements", "announcements")):
            if c[key]: md += [f"**{label}:**"] + [f"- {i['author']}: {short(i['content'])}" for i in c[key][:8]]
        md.append("")
    open(base + ".md", "w").write("\n".join(md))
    out = {"window": {"start": start.isoformat(), "end": end.isoformat(), "hours": a.hours}, "total": len(rows),
           "channels": {n: {**c, "active": dict(c["active"])} for n, c in chans.items()}}
    json.dump(out, open(base + ".json", "w"), indent=1, ensure_ascii=False)
    print(f"wrote {base}.md and {base}.json ({len(rows)} messages)")
if __name__ == "__main__": main()
