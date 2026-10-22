"""Catch up on messages missed while the bot was disconnected or paused.

Runs on ready and on resume. For each readable channel or thread that already
has stored messages, it fetches history after the newest stored id and saves it
through ``upsert_message`` (which dedupes and applies opt-outs and the blocklist).
Channels with nothing stored are skipped. Errors are handled per channel.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord

from db import connect, upsert_message
from permissions import is_excluded

log = logging.getLogger("catchup")

ALWAYS_SKIP: frozenset[str] = frozenset()  # extra channel names to never fetch; the blocklist is applied too
PER_CHANNEL_LIMIT = 500
RUN_LIMIT = 5000


def newest_ids(db_path) -> dict[str, int]:
    """Newest stored message id per channel (snowflakes sort by time)."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT channel_id, id FROM messages").fetchall()
    finally:
        conn.close()
    out: dict[str, int] = {}
    for ch, mid in rows:
        try:
            v = int(mid)
        except (TypeError, ValueError):
            continue
        if ch and v > out.get(ch, 0):
            out[ch] = v
    return out


def save_rows(db_path, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(db_path)
    try:
        saved = sum(1 for r in rows if upsert_message(conn, r))
        conn.commit()
    finally:
        conn.close()
    return saved


def should_skip(channel, blocklist, guild_id=None) -> bool:
    name = getattr(channel, "name", None)
    if name in ALWAYS_SKIP:
        return True
    parent = getattr(channel, "parent", None)
    if parent is not None and (getattr(parent, "name", None) in ALWAYS_SKIP
                               or is_excluded(parent.id, parent.name, blocklist, guild_id)):
        return True
    return is_excluded(getattr(channel, "id", None), name, blocklist, guild_id)


async def _channels(guild):
    seen = {}
    for ch in list(guild.text_channels) + list(guild.threads):
        seen[ch.id] = ch
    for ch in guild.text_channels:
        if not ch.permissions_for(guild.me).read_message_history:
            continue
        try:
            async for th in ch.archived_threads(limit=None):
                seen.setdefault(th.id, th)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return list(seen.values())


async def run_catchup(client, db_path, msg_row, blocklist, *, watching=lambda gid: True,
                      per_channel=PER_CHANNEL_LIMIT, run_limit=RUN_LIMIT) -> dict:
    t0 = time.monotonic()
    stats = dict(channels=0, fetched=0, saved=0, errors=0, skipped=0, capped=False)
    newest = await asyncio.to_thread(newest_ids, db_path)
    for guild in list(client.guilds):
        if not watching(guild.id):
            continue
        for ch in await _channels(guild):
            if stats["fetched"] >= run_limit:
                stats["capped"] = True
                break
            if should_skip(ch, blocklist, guild.id):
                stats["skipped"] += 1
                continue
            after = newest.get(str(ch.id))
            if not after:
                continue
            stats["channels"] += 1
            rows = []
            try:
                lim = min(per_channel, run_limit - stats["fetched"])
                async for m in ch.history(after=discord.Object(id=after), limit=lim, oldest_first=True):
                    rows.append(msg_row(m))
                if len(rows) >= lim:
                    stats["capped"] = True
            except discord.Forbidden:
                stats["errors"] += 1
            except Exception as exc:  # keep going on any per-channel failure
                stats["errors"] += 1
                log.warning("catch-up failed for a channel: %s", type(exc).__name__)
            stats["fetched"] += len(rows)
            for i in range(0, len(rows), 200):
                stats["saved"] += await asyncio.to_thread(save_rows, db_path, rows[i:i + 200])
    stats["secs"] = round(time.monotonic() - t0, 1)
    log.info("catch-up done: %(channels)s channel(s) checked, %(fetched)s fetched, %(saved)s saved, "
             "%(skipped)s skipped, %(errors)s error(s), capped=%(capped)s in %(secs)ss", stats)
    return stats
