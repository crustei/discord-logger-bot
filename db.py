"""SQLite storage for the Discord logger and the Q&A approval queue.

Live logging goes through ``upsert_message``, which refuses blocklisted channels.
Retrieval is a separate FTS5 index. ``purge_excluded`` deletes rows that should
not be kept; the bot calls it on startup and again when the blocklist changes.
The sample database plants a few blocklisted rows on purpose so the demo can
prove they are not retrieved. An empty blocklist deletes nothing.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path

from config import default_db_path, excluded_channels
from permissions import is_excluded

log = logging.getLogger("qa_bot")

MESSAGE_COLUMNS = [
    "id",
    "guild_id",
    "channel_id",
    "channel_name",
    "category",
    "author_id",
    "author_name",
    "display_name",
    "roles",
    "timestamp",
    "edited_timestamp",
    "content",
    "attachments",
    "source",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, guild_id TEXT, channel_id TEXT, channel_name TEXT, category TEXT,
  author_id TEXT, author_name TEXT, display_name TEXT, roles TEXT,
  timestamp TEXT, edited_timestamp TEXT, content TEXT, attachments TEXT,
  source TEXT, deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_msg_ts ON messages(timestamp);
CREATE INDEX IF NOT EXISTS ix_msg_channel ON messages(channel_id);
CREATE TABLE IF NOT EXISTS message_edits(
  message_id TEXT, edited_at TEXT, old_content TEXT, new_content TEXT);
CREATE TABLE IF NOT EXISTS member_snapshots(
  snapshot_at TEXT, user_id TEXT, username TEXT, display_name TEXT, roles TEXT,
  joined_at TEXT, is_bot INTEGER);
CREATE TABLE IF NOT EXISTS member_events(
  at TEXT, user_id TEXT, username TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS role_snapshots(
  snapshot_at TEXT, role_id TEXT, name TEXT, position INTEGER, member_count INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  guild_id TEXT,
  channel_id TEXT NOT NULL,
  channel_name TEXT,
  asker_id TEXT NOT NULL,
  asker_name TEXT,
  question TEXT NOT NULL,
  proposed_context TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  answer TEXT,
  resolved_at TEXT,
  resolved_by TEXT,
  deny_reason TEXT,
  discord_message_id TEXT);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status);
CREATE TABLE IF NOT EXISTS opt_outs(
  user_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_audit(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT,
  guild_id TEXT,
  action TEXT NOT NULL,
  detail TEXT);
CREATE INDEX IF NOT EXISTS ix_admin_audit_at ON admin_audit(at);
"""

_MESSAGES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  message_id UNINDEXED,
  channel_id UNINDEXED,
  channel_name,
  author_id UNINDEXED,
  author_name,
  display_name,
  timestamp UNINDEXED,
  content,
  tokenize='__TOKENIZER__'
)
"""

_DOCS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  path UNINDEXED,
  title,
  content,
  modified UNINDEXED,
  source UNINDEXED,
  visibility UNINDEXED,
  tokenize='__TOKENIZER__'
)
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    ensure_messages_fts(conn)
    ensure_docs_fts(conn)
    conn.commit()
    return conn


def _create_fts(conn: sqlite3.Connection, name: str, ddl: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    if row:
        return
    try:
        conn.execute(ddl.replace("__TOKENIZER__", "porter unicode61"))
    except sqlite3.OperationalError:
        conn.execute(ddl.replace("__TOKENIZER__", "unicode61"))


def ensure_messages_fts(conn: sqlite3.Connection) -> None:
    _create_fts(conn, "messages_fts", _MESSAGES_FTS)
    messages = conn.execute(
        """SELECT COUNT(*) AS n FROM messages
           WHERE deleted=0 AND content IS NOT NULL AND trim(content) != ''"""
    ).fetchone()["n"]
    indexed = conn.execute("SELECT COUNT(*) AS n FROM messages_fts").fetchone()["n"]
    if messages != indexed:
        rebuild_messages_fts(conn)


def ensure_docs_fts(conn: sqlite3.Connection) -> None:
    _create_fts(conn, "docs_fts", _DOCS_FTS)
    try:
        conn.execute("SELECT modified, source, visibility FROM docs_fts LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("DROP TABLE IF EXISTS docs_fts")
        conn.execute("DELETE FROM meta WHERE key='docs_signature'")
        _create_fts(conn, "docs_fts", _DOCS_FTS)


def rebuild_messages_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the search index from the messages table, including excluded rows.

    Search drops excluded channels. Keeping them in the index lets tests and the
    sample database prove that, instead of only proving they were never indexed.
    """
    _create_fts(conn, "messages_fts", _MESSAGES_FTS)
    conn.execute("DELETE FROM messages_fts")
    rows = conn.execute(
        """SELECT id, channel_id, channel_name, author_id, author_name, display_name,
                  timestamp, content
           FROM messages
           WHERE deleted=0 AND content IS NOT NULL AND trim(content) != ''"""
    ).fetchall()
    conn.executemany(
        """INSERT INTO messages_fts(
             message_id, channel_id, channel_name, author_id, author_name, display_name,
             timestamp, content)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (
                row["id"],
                row["channel_id"] or "",
                row["channel_name"] or "",
                row["author_id"] or "",
                row["author_name"] or "",
                row["display_name"] or "",
                row["timestamp"] or "",
                row["content"],
            )
            for row in rows
        ],
    )


def sync_message_fts(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute("DELETE FROM messages_fts WHERE message_id = ?", (message_id,))
    row = conn.execute(
        """SELECT id, channel_id, channel_name, author_id, author_name, display_name,
                  timestamp, content, deleted
           FROM messages WHERE id = ?""",
        (message_id,),
    ).fetchone()
    if row is None or row["deleted"] or not (row["content"] or "").strip():
        return
    conn.execute(
        """INSERT INTO messages_fts(
             message_id, channel_id, channel_name, author_id, author_name, display_name,
             timestamp, content)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            row["id"],
            row["channel_id"] or "",
            row["channel_name"] or "",
            row["author_id"] or "",
            row["author_name"] or "",
            row["display_name"] or "",
            row["timestamp"] or "",
            row["content"],
        ),
    )


def upsert_message(conn: sqlite3.Connection, message: dict, *, allow_excluded: bool = False) -> bool:
    """Insert or keep the newer edit. Returns False when the channel is excluded or the author opted out.

    ``allow_excluded`` is only for the fictional sample database and tests.
    The bot and backfill leave it false. An opted-out author is always refused.
    """
    import json

    message = dict(message)
    if not allow_excluded and is_excluded(
        message.get("channel_id"),
        message.get("channel_name"),
        excluded_channels(),
        message.get("guild_id"),
    ):
        return False
    author_id = str(message.get("author_id") or "").strip()
    if author_id and is_opted_out(conn, author_id):
        if message.get("id"):
            delete_message_everywhere(conn, str(message["id"]))
        return False
    message["roles"] = json.dumps(message.get("roles") or [])
    message["attachments"] = json.dumps(message.get("attachments") or [])
    columns = MESSAGE_COLUMNS
    conn.execute(
        f"INSERT INTO messages({','.join(columns)}) VALUES({','.join('?' * len(columns))}) "
        "ON CONFLICT(id) DO UPDATE SET content=excluded.content, "
        "edited_timestamp=COALESCE(excluded.edited_timestamp, edited_timestamp), "
        "attachments=excluded.attachments "
        "WHERE excluded.edited_timestamp IS NOT NULL "
        "AND (edited_timestamp IS NULL OR excluded.edited_timestamp > edited_timestamp)",
        [message.get(column) for column in columns],
    )
    sync_message_fts(conn, str(message["id"]))
    return True


def record_edit(conn, message_id: str, new_content: str, edited_at: str, cached_row: dict | None = None) -> None:
    """Apply an edit only for a message we stored. Excluded channels are dropped."""
    old = conn.execute(
        "SELECT content, channel_id, channel_name, guild_id, author_id FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    author_id = ""
    if old is not None and old["author_id"]:
        author_id = str(old["author_id"])
    elif cached_row and cached_row.get("author_id"):
        author_id = str(cached_row.get("author_id"))
    if author_id and is_opted_out(conn, author_id):
        delete_message_everywhere(conn, message_id)
        return
    if old and is_excluded(old["channel_id"], old["channel_name"], excluded_channels(), old["guild_id"]):
        delete_message_everywhere(conn, message_id)
        return
    if cached_row and is_excluded(
        cached_row.get("channel_id"),
        cached_row.get("channel_name"),
        excluded_channels(),
        cached_row.get("guild_id"),
    ):
        return
    if old is None and not cached_row:
        return
    conn.execute(
        "INSERT INTO message_edits(message_id, edited_at, old_content, new_content) VALUES (?,?,?,?)",
        (message_id, edited_at, old["content"] if old else None, new_content),
    )
    if old:
        conn.execute(
            "UPDATE messages SET content=?, edited_timestamp=? WHERE id=?",
            (new_content, edited_at, message_id),
        )
        sync_message_fts(conn, message_id)
    elif cached_row:
        upsert_message(conn, cached_row)


def mark_deleted(conn, message_id: str) -> None:
    conn.execute("UPDATE messages SET deleted=1 WHERE id=?", (message_id,))
    conn.execute("DELETE FROM messages_fts WHERE message_id=?", (message_id,))


def delete_message_everywhere(conn, message_id: str) -> None:
    conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
    conn.execute("DELETE FROM messages_fts WHERE message_id=?", (message_id,))
    conn.execute("DELETE FROM message_edits WHERE message_id=?", (message_id,))


def _approval_includes_author(proposed_context: str | None, user_id: str) -> bool:
    import json

    try:
        payload = json.loads(proposed_context or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, list):
        return False
    for item in payload:
        if isinstance(item, dict) and str(item.get("author_id") or "") == user_id:
            return True
    return False


def delete_user_data(conn, user_id: str) -> dict[str, int]:
    """Delete one member's rows from this database.

    Removes their messages (including rows only marked deleted), edit history,
    search-index entries, member snapshots, member events, and approval rows
    they asked for or that stored their message text. Role snapshots stay,
    because those rows are server roles and counts, not a person.
    """
    user_id = str(user_id).strip()
    if not user_id.isdigit():
        raise ValueError("Pass a numeric Discord user id.")
    ids = {row["id"] for row in conn.execute("SELECT id FROM messages WHERE author_id=?", (user_id,))}
    ids.update(
        row["message_id"]
        for row in conn.execute("SELECT message_id FROM messages_fts WHERE author_id=?", (user_id,))
    )
    for message_id in ids:
        delete_message_everywhere(conn, message_id)
    snapshots = conn.execute("DELETE FROM member_snapshots WHERE user_id=?", (user_id,)).rowcount
    events = conn.execute("DELETE FROM member_events WHERE user_id=?", (user_id,)).rowcount
    approval_ids = [
        row["id"]
        for row in conn.execute("SELECT id, asker_id, proposed_context FROM approvals")
        if str(row["asker_id"]) == user_id or _approval_includes_author(row["proposed_context"], user_id)
    ]
    for approval_id in approval_ids:
        conn.execute("DELETE FROM approvals WHERE id=?", (approval_id,))
    conn.commit()
    return {
        "messages": len(ids),
        "snapshots": snapshots,
        "events": events,
        "approvals": len(approval_ids),
    }


def is_opted_out(conn, user_id) -> bool:
    if user_id is None or str(user_id).strip() == "":
        return False
    row = conn.execute(
        "SELECT 1 FROM opt_outs WHERE user_id=?",
        (str(user_id).strip(),),
    ).fetchone()
    return row is not None


def list_opt_outs(conn) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT user_id, created_at FROM opt_outs ORDER BY user_id").fetchall()
    return [(row["user_id"], row["created_at"]) for row in rows]


def add_opt_out(conn, user_id: str) -> dict[str, int]:
    """Remember a member who opted out, then delete the rows ``forget`` would delete."""
    import datetime as dt

    user_id = str(user_id).strip()
    if not user_id.isdigit():
        raise ValueError("Pass a numeric Discord user id.")
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO opt_outs(user_id, created_at) VALUES(?, ?) ON CONFLICT(user_id) DO NOTHING",
        (user_id, created),
    )
    return delete_user_data(conn, user_id)


def remove_opt_out(conn, user_id: str) -> bool:
    user_id = str(user_id).strip()
    if not user_id.isdigit():
        raise ValueError("Pass a numeric Discord user id.")
    cursor = conn.execute("DELETE FROM opt_outs WHERE user_id=?", (user_id,))
    conn.commit()
    return cursor.rowcount == 1


def record_member_snapshot(
    conn,
    snapshot_at: str,
    user_id: str,
    username: str,
    display_name: str,
    roles: str,
    joined_at: str | None,
    is_bot: int,
) -> bool:
    """Store one member snapshot. Opted-out members are skipped."""
    if is_opted_out(conn, user_id):
        return False
    conn.execute(
        "INSERT INTO member_snapshots VALUES(?,?,?,?,?,?,?)",
        (snapshot_at, str(user_id), username, display_name, roles, joined_at, int(is_bot)),
    )
    return True


def record_member_event(conn, user_id: str, username: str, event: str, detail: str, at: str) -> bool:
    """Store one member event. Opted-out members are skipped."""
    if is_opted_out(conn, user_id):
        return False
    conn.execute(
        "INSERT INTO member_events VALUES(?,?,?,?,?)",
        (at, str(user_id), username, event, detail),
    )
    return True


def purge_excluded(conn, excluded=None) -> int:
    """Delete logged messages (and their edits) from blocklisted channels. Returns the count."""
    excluded = excluded_channels() if excluded is None else excluded
    ids: set[str] = set()
    guilds: dict[str, str | None] = {}
    for row in conn.execute("SELECT id, channel_id, channel_name, guild_id FROM messages"):
        if row["channel_id"]:
            guilds[row["channel_id"]] = row["guild_id"]
        if is_excluded(row["channel_id"], row["channel_name"], excluded, row["guild_id"]):
            ids.add(row["id"])
    for row in conn.execute("SELECT message_id, channel_id, channel_name FROM messages_fts"):
        guild_id = guilds.get(row["channel_id"])
        if is_excluded(row["channel_id"], row["channel_name"], excluded, guild_id):
            ids.add(row["message_id"])
    for message_id in ids:
        delete_message_everywhere(conn, message_id)
    return len(ids)


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def replace_docs(conn, rows: list[tuple]) -> None:
    ensure_docs_fts(conn)
    conn.execute("DELETE FROM docs_fts")
    stored = []
    for row in rows:
        if len(row) == 3:
            path, title, content = row
            stored.append((path, title, content, "", "", ""))
        else:
            path, title, content, modified, source, visibility = row[:6]
            stored.append((path, title, content, modified or "", source or "", visibility or ""))
    conn.executemany(
        "INSERT INTO docs_fts(path, title, content, modified, source, visibility) VALUES (?,?,?,?,?,?)",
        stored,
    )


def create_approval(
    conn,
    *,
    created_at: str,
    guild_id: str | None,
    channel_id: str,
    channel_name: str | None,
    asker_id: str,
    asker_name: str | None,
    question: str,
    context: list[dict],
    scope: set[str],
    excluded=None,
) -> int | None:
    """Store a pending question. Drops context outside scope or on an excluded channel."""
    import json

    from permissions import message_in_scope

    excluded = excluded_channels() if excluded is None else excluded
    safe: list[dict] = []
    for item in context:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if item.get("kind") == "doc":
            safe.append(item)
            continue
        if message_in_scope(
            item.get("channel_id"),
            item.get("channel_name"),
            scope=scope,
            excluded=excluded,
            guild_id=item.get("guild_id"),
        ):
            safe.append(item)
    if not safe:
        return None
    cursor = conn.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            created_at,
            guild_id,
            str(channel_id),
            channel_name,
            str(asker_id),
            asker_name,
            question,
            json.dumps(safe, ensure_ascii=False),
            json.dumps(sorted(str(item) for item in scope)),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_approval(conn, approval_id: int):
    return conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()


def list_approvals(conn, status: str | None = "pending"):
    if status:
        return conn.execute(
            "SELECT * FROM approvals WHERE status=? ORDER BY id",
            (status,),
        ).fetchall()
    return conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()


def record_admin_action(
    conn,
    *,
    user_id: str,
    username: str,
    guild_id: str,
    action: str,
    detail: str,
    at: str | None = None,
) -> None:
    """Store one officer action and append the same line to the bot log.

    ``forget`` and opt-out do not remove these rows.
    """
    when = at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    user_id = str(user_id)[:32]
    username = " ".join(str(username or "").split())[:80]
    guild_id = str(guild_id or "")[:32]
    action = " ".join(str(action).split())[:80]
    detail = " ".join(str(detail or "").split())[:1000]
    conn.execute(
        """INSERT INTO admin_audit(at, user_id, username, guild_id, action, detail)
           VALUES(?,?,?,?,?,?)""",
        (when, user_id, username, guild_id, action, detail),
    )
    conn.commit()
    line = f"{when} admin user={user_id} name={username} guild={guild_id} action={action} detail={detail}"
    log.info("%s", line)
    path = Path(os.environ.get("BOT_LOG", "bot.log"))
    try:
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        log.exception("could not append the admin audit line to the bot log")


def resolve_approval(
    conn,
    approval_id: int,
    *,
    status: str,
    answer: str | None = None,
    discord_message_id: str | None = None,
    resolved_by: str = "cli",
    deny_reason: str | None = None,
    resolved_at: str,
) -> bool:
    cursor = conn.execute(
        """UPDATE approvals
           SET status=?, answer=?, discord_message_id=?, resolved_at=?, resolved_by=?, deny_reason=?
           WHERE id=? AND status='pending'""",
        (status, answer, discord_message_id, resolved_at, resolved_by, deny_reason, approval_id),
    )
    conn.commit()
    return cursor.rowcount == 1
