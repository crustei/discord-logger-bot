"""Officer CLI for the approval queue and the channel blocklist.

    python admin.py list
    python admin.py show 3
    python admin.py approve 3
    python admin.py deny 3 --reason "not for this channel"
    python admin.py blocklist list
    python admin.py blocklist add private-channel
    python admin.py blocklist add --guild 123456789 officers
    python admin.py blocklist remove private-channel
    python admin.py forget 123456789012345678
    python admin.py optout list
    python admin.py optout add 123456789012345678
    python admin.py optout remove 123456789012345678

Approving is the only path that calls the model and posts the answer.
The context stored on the row is filtered again before that call.
``blocklist`` edits config/settings.local.toml (gitignored). Pass --settings
to edit a different file. ``blocklist list`` with no --settings prints the
effective list from settings.toml plus the local file. An empty blocklist
excludes nothing. Adding a channel purges matching rows from the database.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from blocklist import (
    BlocklistError,
    add_channel,
    format_blocklist,
    load_blocklist_file,
    remove_channel,
)
from config import excluded_channels, load_config, load_dotenv
from db import (
    add_opt_out,
    connect,
    delete_user_data,
    get_approval,
    list_approvals,
    list_opt_outs,
    purge_excluded,
    remove_opt_out,
    resolve_approval,
)
from llm import ExtractiveProvider, provider_from_env
from permissions import message_in_scope
from qa import compose_answer, utcnow
from retrieve import Hit

def deny_notice() -> str:
    from voice import get_runtime

    return get_runtime().say("denied")


class AdminError(RuntimeError):
    pass


def hits_from_row(row) -> list[Hit]:
    try:
        payload = json.loads(row["proposed_context"] or "[]")
    except json.JSONDecodeError:
        return []
    return [Hit.from_dict(item) for item in payload if isinstance(item, dict)]


def scope_from_row(row) -> set[str]:
    try:
        payload = json.loads(row["scope_json"] or "[]")
    except json.JSONDecodeError:
        return set()
    return {str(item) for item in payload}


def safe_hits(row, excluded: set[str] | None = None) -> list[Hit]:
    excluded = excluded_channels() if excluded is None else excluded
    scope = scope_from_row(row)
    kept: list[Hit] = []
    for hit in hits_from_row(row):
        if hit.kind == "doc":
            if (hit.text or "").strip():
                kept.append(hit)
            continue
        if message_in_scope(
            hit.channel_id,
            hit.channel_name,
            scope=scope,
            excluded=excluded,
            guild_id=hit.guild_id,
        ):
            if (hit.text or "").strip():
                kept.append(hit)
    return kept


def discord_poster(token: str):
    token = token.strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()

    def post(channel_id: str, content: str) -> str:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        request = urllib.request.Request(
            url,
            data=json.dumps({"content": content[:2000]}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordQABot (server Q&A, 1.0)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise AdminError(f"Discord HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdminError(f"could not reach Discord ({exc.reason})") from exc
        return str(body.get("id", ""))

    return post


def require_pending(conn, approval_id: int):
    row = get_approval(conn, approval_id)
    if row is None:
        raise AdminError(f"No request #{approval_id}.")
    if row["status"] != "pending":
        raise AdminError(f"Request #{approval_id} is already {row['status']}.")
    return row


def build_approval_answer(conn, approval_id: int, llm):
    """Filter stored snippets and write the reply. Does not post or resolve."""
    row = require_pending(conn, approval_id)
    hits = safe_hits(row)
    if not hits:
        raise AdminError(
            f"Refusing to approve #{approval_id}: nothing safe left after the exclusion filter."
        )
    text, _block, _kept, _called = compose_answer(row["question"], hits, llm)
    return row, text


def approve_request(conn, approval_id: int, llm, poster, resolved_by: str = "cli") -> str:
    row, text = build_approval_answer(conn, approval_id, llm)
    if poster is None:
        raise AdminError("DISCORD_BOT_TOKEN is not set, so the answer cannot be posted.")
    message_id = poster(str(row["channel_id"]), text)
    if not resolve_approval(
        conn,
        approval_id,
        status="approved",
        answer=text,
        discord_message_id=message_id,
        resolved_by=resolved_by,
        resolved_at=utcnow(),
    ):
        raise AdminError(f"Request #{approval_id} changed before it could be marked approved.")
    return text


def deny_request(conn, approval_id: int, reason: str = "", poster=None, resolved_by: str = "cli") -> str:
    row = require_pending(conn, approval_id)
    if not resolve_approval(
        conn,
        approval_id,
        status="denied",
        deny_reason=reason or None,
        resolved_by=resolved_by,
        resolved_at=utcnow(),
    ):
        raise AdminError(f"Request #{approval_id} changed before it could be marked denied.")
    notice = ""
    if poster is not None:
        try:
            poster(str(row["channel_id"]), deny_notice())
            notice = " Asker notified."
        except AdminError as exc:
            notice = f" Could not notify Discord ({exc})."
    return f"Denied #{approval_id}.{notice}"


def format_list(rows) -> str:
    if not rows:
        return "No pending requests."
    lines = []
    for row in rows:
        try:
            count = len(json.loads(row["proposed_context"] or "[]"))
        except json.JSONDecodeError:
            count = 0
        question = " ".join((row["question"] or "").split())
        if len(question) > 80:
            question = question[:79] + "…"
        lines.append(
            f"#{row['id']}  {row['status']}  {row['created_at']}  "
            f"{row['asker_name'] or row['asker_id']} in #{row['channel_name'] or row['channel_id']}"
        )
        lines.append(f"    {question}")
        lines.append(f"    context: {count} snippet(s). Use show {row['id']} to read them.")
    return "\n".join(lines)


def format_show(row) -> str:
    lines = [
        f"#{row['id']}  {row['status']}  {row['created_at']}",
        f"Asker: {row['asker_name'] or row['asker_id']} ({row['asker_id']})",
        f"Channel: #{row['channel_name'] or row['channel_id']} ({row['channel_id']})",
        f"Question: {row['question']}",
        "",
        "Proposed context:",
    ]
    hits = hits_from_row(row)
    if not hits:
        lines.append("(empty)")
    for index, hit in enumerate(hits, 1):
        lines.append(f"[{index}] {hit.label()}")
        lines.append(hit.text)
        lines.append("")
    if row["answer"]:
        lines.append("Posted answer:")
        lines.append(row["answer"])
    if row["deny_reason"]:
        lines.append(f"Deny reason: {row['deny_reason']}")
    return "\n".join(lines).rstrip()


def settings_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    from voice import get_runtime

    return get_runtime().local_settings_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="Review held answers and edit the channel blocklist.")
    parser.add_argument("--db", help="SQLite path. Defaults to BOT_DB or ./bot.db")
    parser.add_argument(
        "--settings",
        help="Settings file to edit. Defaults to config/settings.local.toml. "
        "blocklist list without this flag prints the effective merged list.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List pending requests")
    show = sub.add_parser("show", help="Print one request and its proposed context")
    show.add_argument("id", type=int)
    approve = sub.add_parser("approve", help="Generate the answer and post it")
    approve.add_argument("id", type=int)
    deny = sub.add_parser("deny", help="Reject a request without posting the context")
    deny.add_argument("id", type=int)
    deny.add_argument("--reason", default="")

    block = sub.add_parser("blocklist", help="List or edit channels that are never logged or quoted")
    block_sub = block.add_subparsers(dest="block_cmd", required=True)
    block_sub.add_parser("list", help="Show the blocklist")
    add = block_sub.add_parser("add", help="Block a channel name or numeric id")
    add.add_argument("channel", help="Channel name or numeric id")
    add.add_argument("--guild", help="Limit this entry to one guild id")
    remove = block_sub.add_parser("remove", help="Remove a channel from the blocklist")
    remove.add_argument("channel", help="Channel name or numeric id")
    remove.add_argument("--guild", help="Guild id, when the entry is not global")

    forget = sub.add_parser("forget", help="Delete one member's rows from the local database")
    forget.add_argument("user_id", help="Numeric Discord user id")

    optout = sub.add_parser("optout", help="Stop logging one member and delete rows already stored")
    opt_sub = optout.add_subparsers(dest="opt_cmd", required=True)
    opt_sub.add_parser("list", help="Show opted-out Discord user ids")
    opt_add = opt_sub.add_parser("add", help="Opt a member out and purge their stored rows")
    opt_add.add_argument("user_id", help="Numeric Discord user id")
    opt_remove = opt_sub.add_parser("remove", help="Let a member be logged again")
    opt_remove.add_argument("user_id", help="Numeric Discord user id")

    args = parser.parse_args(argv)
    cfg = load_config()
    path = settings_path(args.settings)
    if args.cmd == "blocklist" and args.block_cmd == "list":
        try:
            if args.settings:
                shown = load_blocklist_file(path)
            else:
                from voice import get_runtime

                runtime = get_runtime()
                runtime.reload_if_changed()
                shown = runtime.blocklist
            print(format_blocklist(shown))
        except (OSError, BlocklistError) as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    conn = connect(args.db or cfg.db_path)
    try:
        if args.cmd == "list":
            print(format_list(list_approvals(conn, "pending")))
            return 0
        if args.cmd == "show":
            row = get_approval(conn, args.id)
            if row is None:
                print(f"No request #{args.id}.", file=sys.stderr)
                return 1
            print(format_show(row))
            return 0
        if args.cmd == "approve":
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            poster = discord_poster(token) if token else None
            try:
                llm = provider_from_env()
            except Exception as exc:  # LLMError or a config mistake
                print(exc, file=sys.stderr)
                return 1
            if isinstance(llm, ExtractiveProvider):
                pass
            text = approve_request(conn, args.id, llm, poster)
            print(text)
            return 0
        if args.cmd == "deny":
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            poster = discord_poster(token) if token else None
            print(deny_request(conn, args.id, reason=args.reason, poster=poster))
            return 0
        if args.cmd == "blocklist" and args.block_cmd == "add":
            try:
                status, blocklist = add_channel(path, args.channel, args.guild)
            except BlocklistError as exc:
                print(exc, file=sys.stderr)
                return 1
            removed = purge_excluded(conn, blocklist)
            conn.commit()
            print(status)
            if removed:
                print(f"Purged {removed} logged message(s) from the blocklist.")
            print(format_blocklist(blocklist))
            return 0
        if args.cmd == "blocklist" and args.block_cmd == "remove":
            try:
                status, blocklist = remove_channel(path, args.channel, args.guild)
            except BlocklistError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(status)
            print(format_blocklist(blocklist))
            return 0
        if args.cmd == "forget":
            try:
                counts = delete_user_data(conn, args.user_id)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"Deleted stored rows for {args.user_id}.")
            print(f"messages: {counts['messages']}")
            print(f"member snapshots: {counts['snapshots']}")
            print(f"member events: {counts['events']}")
            print(f"approval rows: {counts['approvals']}")
            return 0
        if args.cmd == "optout" and args.opt_cmd == "list":
            rows = list_opt_outs(conn)
            if not rows:
                print("No one has opted out.")
                return 0
            print("Opt-out list:")
            for user_id, created_at in rows:
                print(f"  {user_id}  {created_at}")
            return 0
        if args.cmd == "optout" and args.opt_cmd == "add":
            try:
                counts = add_opt_out(conn, args.user_id)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"Opted out {args.user_id}.")
            print(f"messages: {counts['messages']}")
            print(f"member snapshots: {counts['snapshots']}")
            print(f"member events: {counts['events']}")
            print(f"approval rows: {counts['approvals']}")
            return 0
        if args.cmd == "optout" and args.opt_cmd == "remove":
            try:
                removed = remove_opt_out(conn, args.user_id)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            if not removed:
                print(f"{args.user_id} is not on the opt-out list.", file=sys.stderr)
                return 1
            print(f"Removed {args.user_id} from the opt-out list. New messages can be stored again.")
            return 0
    except AdminError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
