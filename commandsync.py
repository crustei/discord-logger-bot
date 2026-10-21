"""Decide which Discord command sets to publish and which leftovers to clear.

Guild-scoped sync (``GUILD_IDS``) copies commands into those servers and then
clears the global command set, so an earlier global sync does not leave a
second copy in the picker.

Global sync publishes the global set. It clears guild-scoped commands left
from an earlier guild sync. The database remembers which scope was published
(``command_scope`` and ``command_guilds`` in ``meta``). When that record is
missing, guild commands are cleared once for the servers the bot can see.
Later global startups skip that clear.
"""
from __future__ import annotations

from dataclasses import dataclass

META_SCOPE = "command_scope"
META_GUILDS = "command_guilds"


@dataclass(frozen=True)
class SyncPlan:
    mode: str
    sync_guilds: tuple[int, ...]
    publish_global: bool
    clear_global: bool
    clear_guilds: tuple[int, ...]
    next_scope: str
    next_guilds: tuple[int, ...]
    needs_cache: bool


def unique_ids(values) -> tuple[int, ...]:
    seen: set[int] = set()
    out: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text.isdigit():
            continue
        number = int(text)
        if number in seen or number <= 0:
            continue
        seen.add(number)
        out.append(number)
    return tuple(out)


def parse_id_list(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    return unique_ids(str(text).split(","))


def sync_plan(
    *,
    numeric_guild_ids,
    previous_scope: str | None,
    previous_guilds,
    cached_guild_ids,
) -> SyncPlan:
    """Return the publishes and clears for this startup.

    ``cached_guild_ids`` is empty during ``setup_hook`` because the guild
    cache is not ready yet. ``needs_cache`` means a later pass, once
    ``on_ready`` can see guilds, should clear any ids this pass could not.
    """
    current = unique_ids(numeric_guild_ids)
    prev = unique_ids(previous_guilds)
    cached = unique_ids(cached_guild_ids)
    scope = previous_scope if previous_scope in {"guild", "global"} else None

    if current:
        clear: list[int] = [gid for gid in prev if gid not in set(current)]
        needs_cache = False
        if scope is None:
            if cached:
                known = set(current) | set(clear)
                clear.extend(gid for gid in cached if gid not in known)
            else:
                needs_cache = True
        return SyncPlan(
            mode="guild",
            sync_guilds=current,
            publish_global=False,
            clear_global=True,
            clear_guilds=tuple(clear),
            next_scope="guild",
            next_guilds=current,
            needs_cache=needs_cache,
        )

    needs_cache = False
    if scope == "global" and not prev:
        clear = []
    elif scope == "guild":
        clear = list(prev)
        if cached:
            known = set(clear)
            clear.extend(gid for gid in cached if gid not in known)
        else:
            needs_cache = True
    elif cached:
        clear = list(cached)
    else:
        clear = []
        needs_cache = True
    return SyncPlan(
        mode="global",
        sync_guilds=(),
        publish_global=True,
        clear_global=False,
        clear_guilds=tuple(clear),
        next_scope="global",
        next_guilds=(),
        needs_cache=needs_cache,
    )
