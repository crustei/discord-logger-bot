"""Offline demo. Ask as a member without Discord.

    python demo.py --as ethan --channels general,business,projects,internal-announcements \\
        --in general "When did I say I would bring the purchase order form?"

    python demo.py --dry-run --as ethan --channels general "question"

    python demo.py
        interactive REPL. :as, :channels, :in, :dry, :reload, :quit

The default database is sample/sample.db (fictional). Pass --db to use a
logger database instead. Each run copies the sample to sample/demo-session.db
so the committed file stays put. --db uses that file in place.

Saving config/persona.md, config/settings.toml, or config/settings.local.toml
is picked up on the next question. :reload reads them immediately.

The sample database plants a private channel. When the demo uses that
database, sample/settings.toml is added to the blocklist so those rows stay
out of answers. --db does not add that fixture list.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from config import load_dotenv
from db import connect
from llm import ExtractiveProvider, provider_from_env
from permissions import norm_name
from qa import Result, handle_question
from voice import get_runtime
from asklog import record

LOG_QUESTIONS = False  # set by --log

ROOT = Path(__file__).resolve().parent
SAMPLE_DB = ROOT / "sample" / "sample.db"
SAMPLE_SETTINGS = ROOT / "sample" / "settings.toml"
SESSION_DB = ROOT / "sample" / "demo-session.db"
SAMPLE_DOCS = ROOT / "sample" / "docs"
DEFAULT_CHANNELS = "general,business,projects,internal-announcements"


def sample_blocklist():
    """Blocklist fixture for the fictional sample log. Not used by the live bot."""
    from blocklist import load_blocklist_file

    return load_blocklist_file(SAMPLE_SETTINGS)


def resolve_asker(conn, token: str) -> tuple[str, str]:
    token = token.strip()
    rows = conn.execute(
        "SELECT DISTINCT author_id, author_name, display_name FROM messages WHERE author_id IS NOT NULL"
    ).fetchall()
    known = sorted({(row["display_name"] or row["author_name"] or "?") + f" ({row['author_id']})" for row in rows})
    for row in rows:
        if str(row["author_id"]) == token:
            return str(row["author_id"]), row["display_name"] or row["author_name"] or token
    matches = []
    folded = norm_name(token)
    for row in rows:
        names = {norm_name(row["author_name"]), norm_name(row["display_name"])}
        if folded and folded in names:
            matches.append(row)
    ids = {row["author_id"] for row in matches}
    if len(ids) == 1:
        row = matches[0]
        return str(row["author_id"]), row["display_name"] or row["author_name"] or token
    if not matches:
        raise SystemExit("No member matching {0!r}. Known: {1}".format(token, ", ".join(known) or "(none)"))
    raise SystemExit(f"{token!r} matches more than one member. Use an id. Known: {', '.join(known)}")


def resolve_channel(conn, token: str) -> tuple[str, str]:
    text = token.strip().lstrip("#")
    rows = conn.execute("SELECT DISTINCT channel_id, channel_name FROM messages").fetchall()
    for row in rows:
        if str(row["channel_id"]) == text or norm_name(row["channel_name"]) == norm_name(text):
            return str(row["channel_id"]), row["channel_name"]
    return text, text.lstrip("#")


def channel_keys(conn, items: list[str]) -> tuple[set[str], list[tuple[str, str]]]:
    keys: set[str] = set()
    resolved: list[tuple[str, str]] = []
    for item in items:
        channel_id, name = resolve_channel(conn, item)
        resolved.append((channel_id, name))
        keys.add(channel_id)
        keys.add(name)
        keys.add(f"#{name}")
    return keys, resolved


def format_report(result: Result) -> str:
    lines = ["## Context"]
    lines.append(result.context_block or "(empty)")
    lines.append("")
    lines.append("## Decision")
    lines.append(result.kind)
    lines.append(result.reason)
    if result.dry_run and result.kind == "answer":
        lines.append("LLM: skipped (--dry-run)")
    elif result.llm_called:
        lines.append("LLM: called")
    else:
        lines.append("LLM: not called")
    if result.approval_id is not None:
        lines.append(f"request: #{result.approval_id}")
    lines.append("")
    lines.append("## Answer")
    if result.dry_run and result.kind == "answer":
        lines.append("(dry-run) LLM call skipped. No answer generated.")
        lines.append("length: 0")
    else:
        lines.append(result.text)
        lines.append(f"length: {len(result.text)}")
    return "\n".join(lines)


def ask(
    conn,
    *,
    asker: str,
    channels: list[str],
    here: str | None,
    question: str,
    docs_dir: str,
    public: set[str] | None,
    llm,
    dry_run: bool,
    apply_sample_blocklist: bool = False,
    roles: list[str] | None = None,
    tier_roles=None,
) -> Result:
    asker_id, asker_name = resolve_asker(conn, asker)
    visible, resolved = channel_keys(conn, channels)
    if not resolved:
        raise SystemExit("Pass at least one channel in --channels.")
    here_name = here or resolved[0][1]
    here_keys, here_resolved = channel_keys(conn, [here_name])
    channel_id, channel_name = here_resolved[0]
    runtime = get_runtime()
    runtime.reload_if_changed()
    excluded = runtime.blocklist
    if apply_sample_blocklist:
        excluded = excluded.union(sample_blocklist())
    result = handle_question(
        conn,
        question,
        asker_id=asker_id,
        asker_name=asker_name,
        channel_id=channel_id,
        channel_name=channel_name,
        visible=visible | here_keys,
        public_channels=set(runtime.public_channels) if public is None else public,
        here=here_keys,
        excluded=excluded,
        docs_dir=docs_dir,
        llm=llm,
        dry_run=dry_run,
        runtime=runtime,
        asker_roles=list(roles or []),
        tier_roles=tier_roles,
    )
    if LOG_QUESTIONS:
        record(conn, question, result, asker_id=asker_id)
    return result


def open_database(path: str | None) -> tuple[object, str]:
    if path:
        return connect(path), path
    if not SAMPLE_DB.is_file():
        raise SystemExit("Missing sample/sample.db. Build it with: python sample/build_sample.py")
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE_DB, SESSION_DB)
    return connect(SESSION_DB), str(SESSION_DB)


def make_llm(provider: str):
    if provider == "extractive":
        return ExtractiveProvider()
    runtime_provider = provider_from_env()
    if provider == "openai" and runtime_provider.name != "openai":
        raise SystemExit("Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL, or use --provider extractive.")
    return runtime_provider


def repl(conn, args, llm) -> None:
    asker = args.asker or "ethan"
    channels = args.channels or DEFAULT_CHANNELS
    here = args.here
    dry = args.dry_run
    print(f"Using database: {args._db_label}")
    print("Commands: :as <name>, :channels <a,b>, :in <channel>, :dry, :reload, :quit")
    while True:
        try:
            line = input(f"\n[{asker} in {here or channels.split(',')[0]}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {":quit", ":q", "quit", "exit"}:
            return
        if line == ":reload":
            runtime = get_runtime()
            runtime.reload()
            print(runtime.summary())
            continue
        if line == ":dry":
            dry = not dry
            print(f"dry-run {'on' if dry else 'off'}")
            continue
        if line.startswith(":as "):
            asker = line[4:].strip()
            print(f"asking as {asker}")
            continue
        if line.startswith(":channels "):
            channels = line[len(":channels ") :].strip()
            print(f"visible channels: {channels}")
            continue
        if line.startswith(":in "):
            here = line[4:].strip()
            print(f"asking in {here}")
            continue
        if line.startswith(":"):
            print("Unknown command. Try :as, :channels, :in, :dry, :reload, :quit.")
            continue
        try:
            result = ask(
                conn,
                asker=asker,
                channels=[part.strip() for part in channels.split(",") if part.strip()],
                here=here,
                question=line,
                docs_dir=args.docs,
                public=args._public,
                llm=llm,
                dry_run=dry,
                apply_sample_blocklist=args._sample,
            )
        except SystemExit as exc:
            print(exc)
            continue
        print(format_report(result))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Ask the bot against a local SQLite log.")
    parser.add_argument("--as", dest="asker", help="Discord username, display name, or user id")
    parser.add_argument("--channels", help="Comma-separated channel names or ids the asker can see")
    parser.add_argument("--in", dest="here", help="Channel the question is asked in (default: first of --channels)")
    parser.add_argument("--db", help="SQLite database. Default: a copy of sample/sample.db")
    parser.add_argument("--docs", default=str(SAMPLE_DOCS), help="Ops docs folder (default: sample/docs)")
    parser.add_argument("--public", help="Comma-separated public channels. Default: config/settings.toml")
    parser.add_argument("--dry-run", action="store_true", help="Show context and decision without calling the model")
    parser.add_argument("--provider", choices=["auto", "extractive", "openai"], default="auto")
    parser.add_argument("--log", action="store_true", help="Write each question and answer to the anonymous ask_log table in --db")
    parser.add_argument("question", nargs="*", help="Question. Omit it for the interactive REPL.")
    args = parser.parse_args(argv)
    global LOG_QUESTIONS
    LOG_QUESTIONS = bool(args.log)
    conn, label = open_database(args.db)
    args._db_label = label
    args._sample = args.db is None
    args._public = None if args.public is None else {part.strip() for part in args.public.split(",") if part.strip()}
    llm = make_llm("extractive" if args.provider == "extractive" else args.provider)
    try:
        if args.question:
            if not args.asker or not args.channels:
                parser.error("--as and --channels are required when you pass a question")
            result = ask(
                conn,
                asker=args.asker,
                channels=[part.strip() for part in args.channels.split(",") if part.strip()],
                here=args.here,
                question=" ".join(args.question),
                docs_dir=args.docs,
                public=args._public,
                llm=llm,
                dry_run=args.dry_run,
                apply_sample_blocklist=args._sample,
            )
            print(format_report(result))
            return 0
        if not args.asker:
            args.asker = "ethan"
        if not args.channels:
            args.channels = DEFAULT_CHANNELS
        repl(conn, args, llm)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
