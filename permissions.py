"""Who a question is allowed to use. Pure functions: no Discord connection, no database.

Default scope (what retrieval may read at all):
  knowledge files, the asker's own messages, and messages in channels the
  asker can see, minus the blocklist.

General questions are answered from public knowledge files and from that
scope. Knowledge files are ranked above loose messages. Questions about a
specific person's private information wait for an officer. Channels they
cannot see are never retrieved. Blocklisted channels are never used.
An empty blocklist excludes nothing.
"""
from __future__ import annotations

from blocklist import ensure_blocklist, norm_name

# View Channels + Send Messages + Read Message History + Use Application Commands.
# Keep this equal to the integer in the README invite URL.
INVITE_PERMISSIONS = (1 << 10) + (1 << 11) + (1 << 16) + (1 << 31)


def is_excluded(channel_id, channel_name, excluded=None, guild_id=None) -> bool:
    """True when the channel's name or id is on ``excluded``.

    ``excluded`` is a blocklist or an iterable of names and ids. An empty
    blocklist excludes nothing. Per-guild names apply only to that guild when
    ``guild_id`` is set. A numeric channel id matches in every guild. When
    ``guild_id`` is omitted, a name listed for any guild is excluded too, so a
    missing guild id cannot leak a blocklisted channel.
    """
    return ensure_blocklist(excluded).matches(channel_id, channel_name, guild_id)


def _scope_keys(scope: set[str]) -> set[str]:
    keys: set[str] = set()
    for item in scope:
        text = str(item).strip()
        if not text:
            continue
        if text.isdigit():
            keys.add(text)
        else:
            keys.add(norm_name(text))
    return keys


def _channel_keys(channel_id, channel_name) -> set[str]:
    keys: set[str] = set()
    if channel_id is not None and str(channel_id).strip():
        keys.add(str(channel_id))
    name = norm_name(channel_name)
    if name:
        keys.add(name)
    return keys


def channel_visible(channel_id, channel_name, scope: set[str]) -> bool:
    """True when the channel id or name is in ``scope`` (ids, names, or #names)."""
    if not scope:
        return False
    return bool(_channel_keys(channel_id, channel_name) & _scope_keys(scope))


def message_in_scope(
    channel_id,
    channel_name,
    *,
    scope: set[str],
    excluded=None,
    guild_id=None,
) -> bool:
    """In the default retrieval scope: visible to the asker (or public) and not blocklisted."""
    if is_excluded(channel_id, channel_name, excluded, guild_id):
        return False
    return channel_visible(channel_id, channel_name, scope)


def safe_to_auto_post(
    *,
    kind: str,
    author_id,
    asker_id,
    channel_id,
    channel_name,
    here: set[str],
    public: set[str],
    excluded=None,
    guild_id=None,
) -> bool:
    """Own messages that are safe to repeat in the channel where the question was asked.

    Docs and other people's messages are never automatic. The asker's messages
    in some other channel they can see are not automatic either: posting them
    here could show a narrower channel to a wider audience. Those wait in the
    approval queue.
    """
    if kind != "message":
        return False
    if asker_id is None or author_id is None or str(author_id) != str(asker_id):
        return False
    if is_excluded(channel_id, channel_name, excluded, guild_id):
        return False
    return channel_visible(channel_id, channel_name, set(here) | set(public))


def visible_channel_ids(guild, member) -> set[str]:
    """Channel ids this member can view. Fail closed if guild or member is missing.

    ``guild.channels`` items must provide ``id`` and ``permissions_for(member).view_channel``,
    which is what discord.py channels do. A permissions error skips that channel.
    """
    if guild is None or member is None:
        return set()
    found: set[str] = set()
    channels = list(getattr(guild, "channels", []) or [])
    threads = list(getattr(guild, "threads", []) or [])
    for channel in channels:
        _add_if_visible(channel, member, found)
        for thread in list(getattr(channel, "threads", []) or []):
            _add_if_visible(thread, member, found)
    for thread in threads:
        _add_if_visible(thread, member, found)
    return found


def _add_if_visible(channel, member, found: set[str]) -> None:
    try:
        perms = channel.permissions_for(member)
    except Exception:
        return
    if not getattr(perms, "view_channel", False):
        return
    channel_id = getattr(channel, "id", None)
    if channel_id is not None:
        found.add(str(channel_id))


def here_keys(channel) -> set[str]:
    """Id and name for the channel the question was asked in, plus its parent.

    A thread's parent text channel counts as "here", so the asker's own messages
    in that channel can still be quoted when they ask from the thread.
    """
    keys: set[str] = set()
    current = channel
    for _ in range(3):
        if current is None:
            break
        channel_id = getattr(current, "id", None)
        name = getattr(current, "name", None)
        if channel_id is not None:
            keys.add(str(channel_id))
        if name:
            keys.add(str(name))
            keys.add(f"#{name}")
        current = getattr(current, "parent", None)
    return keys


def snowflake_id(obj) -> str | None:
    """String id for a Discord object. ``None`` stays ``None`` (DMs have no guild)."""
    if obj is None:
        return None
    value = getattr(obj, "id", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def channel_guild_id(channel, guild_id=None):
    """Guild id from the argument, or from ``channel.guild`` when the caller has one."""
    if guild_id is not None and str(guild_id).strip():
        return str(guild_id)
    if channel is None:
        return None
    return snowflake_id(getattr(channel, "guild", None))


def invocation_allowed(channel, allowlist: set[str], excluded=None, guild_id=None) -> bool:
    """Whether the bot may post in this channel.

    An empty allowlist means every channel that is not blocklisted. Blocklisted
    channels never qualify, even if they are listed in the allowlist. A thread
    is allowed when its parent channel is.
    """
    guild_id = channel_guild_id(channel, guild_id)
    current = channel
    for _ in range(3):
        if current is None:
            return False
        channel_id = getattr(current, "id", None)
        name = getattr(current, "name", None)
        if is_excluded(channel_id, name, excluded, guild_id):
            return False
        if not allowlist:
            return True
        if channel_visible(channel_id, name, allowlist):
            return True
        current = getattr(current, "parent", None)
    return False
