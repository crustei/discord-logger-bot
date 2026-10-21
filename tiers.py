"""Access tiers for answers.

``tiers`` is ordered from lowest to highest. A person's tier is the highest
tier among their roles. A channel with no ``channel_tiers`` entry takes the
lowest tier among roles that can view it. With no permission data, that
channel is the lowest tier. A knowledge file's ``tier:`` front matter wins.
An older ``visibility: public`` line is the lowest tier. ``visibility: approval``
is the officer tier when that name exists, otherwise the highest tier.

Content above the asker's tier is not retrieved.
"""
from __future__ import annotations

from pathlib import Path

from blocklist import BlocklistError, write_local_table

DEFAULT_TIERS = ["non-member", "member", "officer", "executive"]


class TierError(BlocklistError):
    pass


def parse_tiers(value) -> list[str]:
    """Ordered tier names. Blanks and repeats are dropped. Empty uses the default."""
    if value is None:
        return list(DEFAULT_TIERS)
    if isinstance(value, str):
        parts = value.split(",")
    else:
        try:
            parts = list(value)
        except TypeError:
            return list(DEFAULT_TIERS)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = " ".join(str(part).split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out or list(DEFAULT_TIERS)


def parse_tier_roles(value, tiers: list[str]) -> dict[str, list[str]]:
    """Map each tier to role ids or names. Unknown tier names are ignored."""
    known = {tier.casefold(): tier for tier in tiers}
    out = {tier: [] for tier in tiers}
    if not isinstance(value, dict):
        return out
    for key, roles in value.items():
        name = known.get(str(key).strip().casefold())
        if name is None:
            continue
        out[name] = _role_list(roles)
    return out


def parse_channel_tiers(value, tiers: list[str]) -> dict[str, str]:
    """Map a channel id or name to a tier. Unknown tiers are ignored."""
    known = {tier.casefold(): tier for tier in tiers}
    out: dict[str, str] = {}
    if not isinstance(value, dict):
        return out
    for key, tier in value.items():
        token = str(key).strip().lstrip("#")
        resolved = known.get(str(tier).strip().casefold())
        if not token or resolved is None:
            continue
        stored = token if token.isdigit() else token.casefold()
        out[stored] = resolved
    return out


def _role_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    else:
        try:
            parts = list(value)
        except TypeError:
            return []
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def canonical_tier(name, tiers: list[str]) -> str:
    key = str(name or "").strip().casefold()
    for tier in tiers:
        if tier.casefold() == key:
            return tier
    known = ", ".join(tiers) if tiers else "(none)"
    raise TierError(f"Unknown tier {str(name).strip()!r}. Tiers are: {known}.")


def tier_index(name: str, tiers: list[str]) -> int:
    key = str(name or "").strip().casefold()
    for index, tier in enumerate(tiers):
        if tier.casefold() == key:
            return index
    return len(tiers) - 1 if tiers else 0


def asker_tier_index(roles, tiers: list[str], tier_roles: dict[str, list[str]]) -> int:
    """Highest tier among ``roles`` (ids or names). No match is the lowest tier."""
    if not tiers:
        return 0
    tokens = [str(role).strip() for role in (roles or []) if str(role).strip()]
    ids = {token for token in tokens if token.isdigit()}
    names = {token.casefold() for token in tokens}
    best = 0
    for index, tier in enumerate(tiers):
        for item in tier_roles.get(tier, []):
            if item.isdigit() and item in ids:
                best = index
            elif item.casefold() in names:
                best = index
    return best


def resolve_doc_tier(raw: str | None, tiers: list[str]) -> str:
    """Front-matter tier, or the legacy visibility line.

    ``public`` and a blank value are the lowest tier. ``approval`` is
    ``officer`` when that tier exists, otherwise the highest tier. A name
    that is not in ``tiers`` is the highest tier, so an unknown label is
    not treated as public.
    """
    if not tiers:
        return ""
    text = str(raw or "").strip().casefold()
    known = {tier.casefold(): tier for tier in tiers}
    if text in known:
        return known[text]
    if text in {"", "public"}:
        return tiers[0]
    if text == "approval" and "officer" in known:
        return known["officer"]
    return tiers[-1]


def _is_everyone(role_id: str, role_name: str) -> bool:
    name = str(role_name or "").strip().casefold()
    if name in {"@everyone", "everyone"}:
        return True
    return str(role_id or "").strip().casefold() == "everyone"


def _role_index(role_id: str, role_name: str, tiers: list[str], tier_roles: dict[str, list[str]]) -> int | None:
    """Tier index for one role. ``None`` when the role is not mapped."""
    for index, tier in enumerate(tiers):
        for item in tier_roles.get(tier, []):
            if item.isdigit() and role_id and item == str(role_id):
                return index
            if role_name and item.casefold() == str(role_name).strip().casefold():
                return index
    return None


def lookup_channel_tier(channel_id, channel_name, channel_tiers: dict[str, str]) -> str | None:
    if channel_id is not None and str(channel_id).strip():
        found = channel_tiers.get(str(channel_id).strip())
        if found:
            return found
    name = str(channel_name or "").strip().lstrip("#").casefold()
    if name:
        return channel_tiers.get(name)
    return None


def channel_tier_name(
    channel_id,
    channel_name,
    *,
    tiers: list[str],
    channel_tiers: dict[str, str],
    tier_roles: dict[str, list[str]],
    viewers: list[tuple[str, str, bool]] | None,
) -> str:
    """Explicit map, else the lowest tier among roles that can view the channel.

    ``viewers`` is ``None`` when permission data is unavailable. That channel
    is the lowest tier. An explicit ``channel_tiers`` entry still wins.
    ``@everyone`` able to view makes the channel the lowest tier. A role that
    can view but is not mapped is also the lowest tier. When every listed
    role is denied, the channel is the highest tier.
    """
    if not tiers:
        return ""
    explicit = lookup_channel_tier(channel_id, channel_name, channel_tiers)
    if explicit:
        return explicit
    if viewers is None:
        return tiers[0]
    lowest: int | None = None
    everyone = False
    saw_answer = False
    for role_id, role_name, can_view in viewers:
        saw_answer = True
        if not can_view:
            continue
        if _is_everyone(role_id, role_name):
            everyone = True
            continue
        index = _role_index(str(role_id), str(role_name), tiers, tier_roles)
        if index is None:
            index = 0
        if lowest is None or index < lowest:
            lowest = index
    if not saw_answer:
        return tiers[0]
    if everyone:
        return tiers[0]
    if lowest is not None:
        return tiers[lowest]
    return tiers[-1]


def viewers_for(channel_id, channel_name, channel_viewers) -> list[tuple[str, str, bool]] | None:
    """Viewer rows for one channel. ``None`` when this channel was not measured."""
    if channel_viewers is None:
        return None
    keys = []
    if channel_id is not None and str(channel_id).strip():
        keys.append(str(channel_id).strip())
    name = str(channel_name or "").strip().lstrip("#").casefold()
    if name:
        keys.append(name)
    for key in keys:
        if key in channel_viewers:
            return list(channel_viewers[key])
    return None


def hit_tier_name(
    hit,
    *,
    tiers: list[str],
    tier_roles: dict[str, list[str]],
    channel_tiers: dict[str, str],
    channel_viewers,
) -> str:
    if getattr(hit, "kind", "") == "doc":
        return resolve_doc_tier(getattr(hit, "visibility", None), tiers)
    return channel_tier_name(
        getattr(hit, "channel_id", None),
        getattr(hit, "channel_name", None),
        tiers=tiers,
        channel_tiers=channel_tiers,
        tier_roles=tier_roles,
        viewers=viewers_for(getattr(hit, "channel_id", None), getattr(hit, "channel_name", None), channel_viewers),
    )


def hit_within_tier(
    hit,
    *,
    asker_index: int,
    tiers: list[str],
    tier_roles: dict[str, list[str]],
    channel_tiers: dict[str, str],
    channel_viewers,
) -> bool:
    """True when the hit is at or below the asker's tier."""
    name = hit_tier_name(
        hit,
        tiers=tiers,
        tier_roles=tier_roles,
        channel_tiers=channel_tiers,
        channel_viewers=channel_viewers,
    )
    return tier_index(name, tiers) <= int(asker_index)


def format_tiers(runtime) -> str:
    """Effective tier list, role map, and channel map."""
    tiers = list(getattr(runtime, "tiers", []) or [])
    roles = getattr(runtime, "tier_roles", {}) or {}
    channels = getattr(runtime, "channel_tiers", {}) or {}
    lines = ["Tiers (lowest first): " + (", ".join(tiers) if tiers else "(none)")]
    lines.append("Roles:")
    for tier in tiers:
        items = list(roles.get(tier) or [])
        lines.append(f"  {tier}: {', '.join(items) if items else '(none)'}")
    lines.append("Channels:")
    if not channels:
        lines.append("  (none)")
    else:
        for key in sorted(channels):
            lines.append(f"  {key}: {channels[key]}")
    return "\n".join(lines)


def _same_role(stored: str, role_id: str, role_name: str) -> bool:
    if role_id and stored == role_id:
        return True
    if role_name and stored.casefold() == role_name.strip().casefold():
        return True
    return False


def assign_role_tier(
    path: Path,
    tier: str,
    *,
    role_id: str | None = None,
    role_name: str | None = None,
    tiers: list[str],
    tier_roles: dict[str, list[str]],
) -> str:
    """Map one role onto a tier in the local settings file. Other tiers lose that role."""
    chosen = canonical_tier(tier, tiers)
    role_id = str(role_id).strip() if role_id else ""
    role_name = str(role_name or "").strip()
    if role_id and not role_id.isdigit():
        raise TierError("Role id must be numeric.")
    if not role_id and not role_name:
        raise TierError("Pick a role or type a name.")
    token = role_id or role_name
    updated: dict[str, list[str]] = {}
    for name in tiers:
        kept = [item for item in (tier_roles.get(name) or []) if not _same_role(item, role_id, role_name)]
        if name == chosen:
            kept.append(token)
        if kept:
            updated[name] = kept
    write_local_table(path, "tier_roles", updated, arrays=True)
    label = role_name or role_id
    if role_id and role_name:
        label = f"{role_name} ({role_id})"
    return f"Set {label} to {chosen}."


def set_channel_tier(
    path: Path,
    tier: str,
    *,
    channel_id: str | None = None,
    channel_name: str | None = None,
    tiers: list[str],
    channel_tiers: dict[str, str],
) -> str:
    """Map a channel id, and its name when given, onto a tier in the local file."""
    chosen = canonical_tier(tier, tiers)
    channel_id = str(channel_id).strip() if channel_id else ""
    channel_name = str(channel_name or "").strip().lstrip("#")
    if channel_id and not channel_id.isdigit():
        raise TierError("Channel id must be numeric.")
    if not channel_id and not channel_name:
        raise TierError("Pick a channel or type a name.")
    updated = dict(channel_tiers)
    if channel_id:
        updated[channel_id] = chosen
    if channel_name:
        updated[channel_name.casefold()] = chosen
    write_local_table(path, "channel_tiers", updated, arrays=False)
    if channel_id and channel_name:
        label = f"#{channel_name} ({channel_id})"
    else:
        label = f"#{channel_name}" if channel_name else channel_id
    return f"Set {label} to {chosen}."
