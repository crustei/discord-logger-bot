"""Access tiers: public < member < officer < admin.

* admin   -> anyone with the Discord Administrator permission. No setup needed.
* officer -> holders of any role an admin mapped to "officer".
* member  -> holders of any role an admin mapped to "member".
* public  -> everyone else.

Admins map roles with ``/admin roles set|remove|show``. The mapping is stored
by role id per server in data/role_tiers.json (survives restarts, reloads when
the file changes). A server with no saved mapping falls back to the optional
role-name seed file config/tiers.toml, which is also used once to seed the
id mapping on startup. Roles are always read live from the interaction.

Each tier owns one knowledge base folder under knowledge/ (admins use officer's):
  public/  -> public askers search only this
  member/  -> members search member + public, member entries ranked first
  officer/ -> officers and admins search all three, officer entries first
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

TIERS = ["public", "member", "officer", "admin"]
CONFIGURABLE = ("member", "officer")
BASES = {"public": "public", "member": "member", "officer": "officer", "admin": "officer"}
BASE_TIER = {"public": "public", "member": "member", "officer": "officer"}
RANK = {name: index for index, name in enumerate(TIERS)}
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config" / "tiers.toml"
CHANNEL_FILE = ROOT / "data" / "channel_tiers.json"
ROLE_FILE = Path(os.environ.get("ROLE_TIERS_FILE", ROOT / "data" / "role_tiers.json"))

_cache: dict = {"mtime": None, "roles": {}}
_store_cache: dict = {"key": None, "data": {}}

_ALIASES = {"nonmember": "public", "non-member": "public", "everyone": "public", "guest": "public",
            "exec": "officer", "executive": "officer", "officers": "officer",
            "administrator": "admin", "admins": "admin"}


def norm_tier(value) -> str:
    text = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    text = _ALIASES.get(text, text)
    return text if text in RANK else "member"


def at_least(tier, minimum: str) -> bool:
    return RANK[norm_tier(tier)] >= RANK[norm_tier(minimum)]


def is_officer(tier) -> bool:
    """Officer verification: officers and admins count."""
    return at_least(tier, "officer")


# ---- optional role-name seed (config/tiers.toml) ---------------------------------
def role_map(path: Path = CONFIG) -> dict[str, str]:
    """Role name (casefolded) -> tier from the seed file. Missing file: {}."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache["mtime"] != (str(path), mtime):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        roles: dict[str, str] = {}
        for key, names in (data.get("roles", {}) or {}).items():
            tier = norm_tier(key)
            if tier not in CONFIGURABLE:
                continue  # public is the default; admin comes from Discord permissions
            for name in names or []:
                roles[str(name).casefold()] = tier
        _cache.update(mtime=(str(path), mtime), roles=roles)
    return dict(_cache["roles"])


# ---- role-id store (data/role_tiers.json) -----------------------------------------
def load_store(path: Path | None = None) -> dict[str, dict[str, str]]:
    path = Path(path or ROLE_FILE)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _store_cache["key"] != key:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            raw = {}
        data = {str(g): {str(r): norm_tier(t) for r, t in (m or {}).items() if norm_tier(t) in CONFIGURABLE}
                for g, m in raw.items() if isinstance(m, dict)}
        _store_cache.update(key=key, data=data)
    return {g: dict(m) for g, m in _store_cache["data"].items()}


def _save_store(data, path: Path | None = None) -> None:
    path = Path(path or ROLE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    _store_cache["key"] = None


def guild_roles(guild_id, path: Path | None = None) -> dict[str, str] | None:
    """Role id -> tier for a server, or None if the server was never configured."""
    return load_store(path).get(str(guild_id))


def set_role(guild_id, role_id, tier: str, path: Path | None = None) -> str | None:
    """Map a role. Returns the previous tier for that role, if any."""
    tier = norm_tier(tier)
    if tier not in CONFIGURABLE:
        raise ValueError("Tier must be member or officer.")
    data = load_store(path)
    roles = data.setdefault(str(guild_id), {})
    old = roles.get(str(role_id))
    roles[str(role_id)] = tier
    _save_store(data, path)
    return old


def remove_role(guild_id, role_id, path: Path | None = None) -> str | None:
    data = load_store(path)
    roles = data.setdefault(str(guild_id), {})
    old = roles.pop(str(role_id), None)
    _save_store(data, path)  # an emptied server stays configured (no name fallback)
    return old


def seed_guild(guild, path: Path | None = None, names: dict[str, str] | None = None) -> int | None:
    """First start in a server: turn the role-name seed into role ids. None if already configured."""
    gid = str(getattr(guild, "id", ""))
    if guild_roles(gid, path) is not None:
        return None
    names = role_map() if names is None else names
    mapped = {str(r.id): names[r.name.casefold()] for r in getattr(guild, "roles", []) or []
              if getattr(r, "name", "").casefold() in names}
    data = load_store(path)
    data[gid] = mapped
    _save_store(data, path)
    return len(mapped)


# ---- resolving a member ---------------------------------------------------------
def _mapping_for(guild_id) -> tuple[dict[str, str], bool]:
    """(mapping, by_id). Configured servers use role ids; others the name seed."""
    ids = guild_roles(guild_id) if guild_id is not None else None
    if ids is not None:
        return ids, True
    return role_map(), False


def tier_for_roles(role_keys, mapping: dict[str, str] | None = None) -> str:
    """Highest configured tier among role keys (ids or casefolded names)."""
    mapping = role_map() if mapping is None else mapping
    best = "public"
    for key in role_keys or []:
        tier = mapping.get(str(key)) or mapping.get(str(key).casefold())
        if tier and RANK[tier] > RANK[best]:
            best = tier
    return best


def is_admin(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(getattr(perms, "administrator", False))


def member_tier(member, mapping: dict[str, str] | None = None) -> str:
    """Live tier from the member's current permissions and roles."""
    if is_admin(member):
        return "admin"
    roles = getattr(member, "roles", []) or []
    if mapping is not None:
        by_id = False
    else:
        guild = getattr(member, "guild", None)
        mapping, by_id = _mapping_for(getattr(guild, "id", None))
    keys = [str(getattr(r, "id", "")) if by_id else getattr(r, "name", "") for r in roles]
    return tier_for_roles(keys, mapping)


def _role_tier(role, mapping, by_id) -> str | None:
    if getattr(role, "name", "") == "@everyone":
        return "public"
    key = str(getattr(role, "id", "")) if by_id else getattr(role, "name", "").casefold()
    return mapping.get(key)


def base_of(display_path: str) -> str | None:
    """Which base a knowledge file lives in (first folder under knowledge/), or None."""
    parts = [p for p in str(display_path or "").replace("\\", "/").split("/") if p]
    if parts and parts[0] == "knowledge":
        parts = parts[1:]
    return parts[0] if len(parts) > 1 and parts[0] in BASE_TIER else None


def searchable_bases(asker_tier: str) -> list[str]:
    """Bases this asker may search, most preferred first."""
    rank = RANK[norm_tier(asker_tier)]
    return [BASE_TIER_ORDER for BASE_TIER_ORDER in ("officer", "member", "public")
            if RANK[BASE_TIER[BASE_TIER_ORDER]] <= rank]


def allows(asker_tier: str, entry_tier: str) -> bool:
    return RANK[norm_tier(asker_tier)] >= RANK[norm_tier(entry_tier)]


def channel_min_tier(channel, roles, mapping: dict[str, str] | None = None, by_id: bool = False) -> str | None:
    """Lowest tier holding a role that can view ``channel``. None if no mapped role can."""
    mapping = role_map() if mapping is None else mapping
    found = None
    for role in roles:
        tier = _role_tier(role, mapping, by_id)
        if tier is None:
            continue
        try:
            if not channel.permissions_for(role).view_channel:
                continue
        except Exception:
            continue
        if found is None or RANK[tier] < RANK[found]:
            found = tier
    return found


def write_channel_tiers(guild, path: Path = CHANNEL_FILE) -> int:
    """Snapshot channel id -> {name, tier} so the knowledge builder can tag entries."""
    roles = list(getattr(guild, "roles", []) or [])
    mapping, by_id = _mapping_for(getattr(guild, "id", None))
    out: dict[str, dict] = {}
    channels = list(getattr(guild, "channels", []) or []) + list(getattr(guild, "threads", []) or [])
    for channel in channels:
        tier = channel_min_tier(channel, roles, mapping, by_id)
        viewers = []
        for role in roles:
            try:
                if channel.permissions_for(role).view_channel:
                    viewers.append(role.name)
            except Exception:
                pass
        parent = getattr(channel, "category", None)
        out[str(channel.id)] = {"name": getattr(channel, "name", ""), "tier": tier or "officer",
                                "category": getattr(parent, "name", None), "viewers": viewers}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    return len(out)
