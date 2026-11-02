"""Build knowledge/<tier>/generated/<topic>/*.md from local JSON extractions.

This is a generic indexer. It does not know any server, person, or project name.

Input files (gitignored) live in tools_local/kb/raw_local/. Each file is JSON:

    {"group": "batch-name", "entries": [
        {"q": "When is the meeting?", "a": "Fridays at 6 PM.", "topic": "meetings", "ids": ["message-id"]}
    ]}

Environment:
    GUILD_ID   Discord guild id used only to build source links. Required when entries cite messages.
    BOT_DB     SQLite database. Defaults to bot.db next to the repo root.
    DOCS_DIR   Knowledge root. Defaults to ./knowledge.

Message rows supply the channel and date. Entries whose source channel is on the
blocklist are dropped. Role rosters come from the newest member snapshot.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tiers
from qa import is_gap_answer
from voice import get_runtime

GEN = "generated"
TOPICS = {
    "teams", "projects", "meetings", "officers", "events", "recruitment", "funding",
    "purchasing", "socials", "facilities", "testing", "training", "glossary",
    "timeline", "resources", "operations", "general", "roster",
}
PII = re.compile(r"[\w.+-]+@[\w-]+\.\w+|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}|password|\bgpa\b", re.I)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:60] or "entry"


def _tokens(text: str) -> set[str]:
    short = (get_runtime().org_short_name or "").casefold()
    stop = {
        "the", "a", "an", "of", "is", "are", "what", "when", "where", "who", "how",
        "does", "do", "for", "in", "on", "to", "and", "s",
    }
    if short:
        stop.add(short)
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in stop}


def dedupe(entries, jac_min=0.75):
    """Keep the newest near-duplicate question within a topic."""
    entries = sorted(entries, key=lambda e: e["modified"], reverse=True)
    kept_idx = []
    by_topic = collections.defaultdict(list)
    toks = [_tokens(e["title"] + " " + e["fact"][:80]) for e in entries]
    for i, entry in enumerate(entries):
        prev = by_topic[entry["topic"]]
        if prev and any(
            toks[i] and len(toks[i] & toks[j]) / len(toks[i] | toks[j]) >= jac_min for j in prev
        ):
            continue
        prev.append(i)
        kept_idx.append(i)
    return [entries[i] for i in kept_idx]


def chan_tier(channel_id, channels, role_map):
    info = channels.get(str(channel_id))
    if not info:
        return None
    viewers = info.get("viewers") or []
    found = []
    for role in viewers:
        if role == "@everyone":
            found.append("non-member")
        else:
            tier = role_map.get(str(role).casefold())
            if tier:
                found.append(tier)
    if not found:
        return "officer"
    return min(found, key=lambda name: tiers.RANK.get(name, 0))


def rosters(conn, day_role_map):
    snap = conn.execute("select max(snapshot_at) from member_snapshots").fetchone()[0]
    if not snap:
        return []
    day = snap[:10]
    by_role = collections.defaultdict(list)
    for row in conn.execute(
        "select display_name, username, roles from member_snapshots where snapshot_at=? and is_bot=0",
        (snap,),
    ):
        for role in json.loads(row["roles"] or "[]"):
            if "bot" in role.casefold():
                continue
            by_role[role].append(row["display_name"] or row["username"])
    out = []
    for role, names in sorted(by_role.items()):
        names = sorted(set(names), key=str.casefold)
        tier_of_role = day_role_map.get(role.casefold())
        if tier_of_role == "officer" and len(names) <= 25:
            out.append(dict(
                topic="officers",
                title=f"Who holds the {role} role",
                tier="member",
                channels=[],
                fact=f"As of {day}, the Discord members with the {role} role are: {', '.join(names)}.",
                source="Discord server role list (member snapshot)",
                modified=day,
                origin="roster",
            ))
        for index in range(0, len(names), 40):
            part = names[index:index + 40]
            suffix = f" (part {index // 40 + 1})" if len(names) > 40 else ""
            out.append(dict(
                topic="roster",
                title=f"{role} roster{suffix}",
                tier="member",
                channels=[],
                fact=f"Current members list: as of {day}, the members with the {role} role are: {', '.join(part)}.",
                source="Discord server role list (member snapshot)",
                modified=day,
                origin=f"roster-{role}",
            ))
    return out


def main() -> int:
    guild = os.environ.get("GUILD_ID", "").strip()
    db_path = Path(os.environ.get("BOT_DB", ROOT / "bot.db"))
    knowledge = Path(os.environ.get("DOCS_DIR", ROOT / "knowledge"))
    raw_dir = ROOT / "tools_local" / "kb" / "raw_local"
    files = sorted(raw_dir.glob("*.json")) if raw_dir.is_dir() else []
    if not files and not db_path.is_file():
        print("No raw_local JSON and no database. Nothing to build.")
        return 0
    if not db_path.is_file():
        print(f"Database not found: {db_path}")
        return 1
    blocklist = get_runtime().blocklist
    role_map = tiers.role_map()
    channel_path = ROOT / "data" / "channel_tiers.json"
    channels = json.loads(channel_path.read_text(encoding="utf-8")) if channel_path.is_file() else {}
    name_to_id = {}
    for key, value in channels.items():
        if isinstance(value, dict) and value.get("name"):
            name_to_id[value["name"]] = key

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {}

    def message(mid):
        if mid not in rows:
            rows[mid] = conn.execute(
                "select id, channel_id, channel_name, category, timestamp from messages where id=?",
                (str(mid),),
            ).fetchone()
        return rows[mid]

    entries, dropped = [], collections.Counter()
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        group = str(data.get("group") or path.stem)
        items = data.get("entries") if isinstance(data.get("entries"), list) else []
        if not items:
            dropped["unparsed"] += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = " ".join(str(item.get("a") or item.get("fact") or "").split())
            title = " ".join(str(item.get("q") or item.get("title") or "").split())
            if not fact or not title or len(fact) > 1000:
                dropped["empty/long"] += 1
                continue
            if is_gap_answer(fact):
                dropped["gap"] += 1
                continue
            if PII.search(fact):
                dropped["pii"] += 1
                continue
            sources = [message(i) for i in (item.get("ids") or [])[:3]]
            sources = [row for row in sources if row]
            if not sources:
                dropped["no-source"] += 1
                continue
            if not guild:
                print("Set GUILD_ID before building entries that cite messages.")
                return 1
            channels_used, tier_list, links, dates = [], [], [], []
            blocked = False
            for row in sources:
                if blocklist.matches(row["channel_id"], row["channel_name"], guild) or (
                    row["category"] and blocklist.matches(None, row["category"], guild)
                ):
                    blocked = True
                    break
                cid = row["channel_id"] if row["channel_id"] in channels else name_to_id.get(row["category"] or "", row["channel_id"])
                tier_list.append(chan_tier(cid, channels, role_map) or "member")
                channels_used.append(str(cid))
                links.append(f"https://discord.com/channels/{guild}/{row['channel_id']}/{row['id']}")
                dates.append((row["timestamp"] or "")[:10])
            if blocked:
                dropped["excluded"] += 1
                continue
            topic = item.get("topic") if item.get("topic") in TOPICS else "general"
            entries.append(dict(
                topic=topic,
                title=title,
                fact=fact,
                tier=max(tier_list, key=lambda name: tiers.RANK.get(name, 0)),
                channels=sorted(set(channels_used)),
                source=" ".join(links),
                modified=max(dates),
                origin="#" + group,
            ))
    raw_count = len(entries)
    entries = dedupe(entries)
    print("raw", raw_count, "after dedupe", len(entries))
    entries += rosters(conn, role_map)
    for base in tiers.BASES.values():
        generated = knowledge / base / GEN
        if generated.exists():
            shutil.rmtree(generated)
    used = set()
    for entry in entries:
        entry["tier"] = tiers.norm_tier(entry["tier"])
        dest = knowledge / tiers.BASES[entry["tier"]] / GEN / entry["topic"]
        dest.mkdir(parents=True, exist_ok=True)
        name = slug(entry["title"])
        unique = name
        n = 2
        while (entry["tier"], entry["topic"], unique) in used:
            unique = f"{name}-{n}"
            n += 1
        used.add((entry["tier"], entry["topic"], unique))
        front = [
            "---",
            f"title: {entry['title']}",
            f"topic: {entry['topic']}",
            f"base: {tiers.BASES[entry['tier']]}",
            f"channels: {','.join(entry['channels'])}",
            f"origin: {entry['origin']}",
            f"modified: {entry['modified']}",
            f"source: {entry['source']}",
            "---",
        ]
        (dest / f"{unique}.md").write_text(
            "\n".join(front) + f"\n# {entry['title']}\n\n{entry['fact']}\n\nSource: {entry['source']}\n",
            encoding="utf-8",
        )
    print(len(entries), "entries; dropped", dict(dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
