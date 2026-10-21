"""User-defined channel blocklist.

Channels on this list are never logged, retrieved, or answered from.
A name or numeric id in ``excluded_channels`` applies to every guild.
``[blocklist.guilds]`` limits a name to one guild id. Numeric channel ids match
everywhere they are listed, because Discord snowflakes are unique.

An empty blocklist excludes nothing. Nothing is added in code.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


class BlocklistError(RuntimeError):
    pass


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip().lstrip("#").casefold()


def clean_token(raw) -> str:
    """Channel name or id as stored: no surrounding space, no leading #, mention ids unwrapped."""
    text = str(raw).strip() if raw is not None else ""
    if text.startswith("<#") and text.endswith(">") and text[2:-1].isdigit():
        return text[2:-1]
    if text.startswith("#"):
        text = text[1:].strip()
    return text


def string_items(value) -> list[str]:
    """Ordered, de-duplicated channel tokens. Skips blanks and tokens with spaces."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = clean_token(part)
        if not text or any(ch.isspace() for ch in text):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    out.sort(key=str.casefold)
    return out


class Blocklist:
    """Global names/ids plus per-guild names/ids. Immutable."""

    def __init__(self, global_channels=(), by_guild=None):
        self.global_channels = tuple(string_items(list(global_channels)))
        guilds: dict[str, tuple[str, ...]] = {}
        for gid, channels in (by_guild or {}).items():
            key = str(gid).strip()
            if not key:
                continue
            items = tuple(string_items(channels))
            if items:
                guilds[key] = items
        self.by_guild = guilds

    def __bool__(self) -> bool:
        return bool(self.global_channels) or bool(self.by_guild)

    def __iter__(self):
        seen: set[str] = set()
        for token in self.global_channels:
            key = token.casefold()
            if key not in seen:
                seen.add(key)
                yield token
        for gid in sorted(self.by_guild):
            for token in self.by_guild[gid]:
                key = token.casefold()
                if key not in seen:
                    seen.add(key)
                    yield token

    def __contains__(self, item) -> bool:
        if item is None:
            return False
        text = clean_token(item)
        if not text:
            return False
        channel_id = text if text.isdigit() else None
        return self.matches(channel_id, text, guild_id=None)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def union(self, other: "Blocklist") -> "Blocklist":
        """Combine two blocklists. A channel listed in either one is excluded."""
        guilds: dict[str, list[str]] = {gid: list(channels) for gid, channels in self.by_guild.items()}
        for gid, channels in other.by_guild.items():
            guilds[gid] = list(guilds.get(gid, [])) + list(channels)
        return Blocklist(list(self.global_channels) + list(other.global_channels), guilds)

    def fingerprint(self) -> str:
        parts = ["*:" + ",".join(self.global_channels)]
        for gid in sorted(self.by_guild):
            parts.append(gid + ":" + ",".join(self.by_guild[gid]))
        return "|".join(parts)

    def describe(self) -> str:
        if not self:
            return "none"
        parts: list[str] = []
        if self.global_channels:
            parts.append(", ".join(self.global_channels))
        for gid in sorted(self.by_guild):
            names = ", ".join(self.by_guild[gid])
            parts.append(f"guild {gid}: {names}")
        return "; ".join(parts)

    def matches(self, channel_id, channel_name, guild_id=None) -> bool:
        """True when this channel is blocklisted.

        Global names and ids always match. A numeric id listed under any guild
        matches in every guild. A per-guild name matches that guild. When the
        guild id is unknown, a per-guild name matches too, so a missing guild
        cannot leak the channel.
        """
        if _token_hit(self.global_channels, channel_id, channel_name):
            return True
        id_text = "" if channel_id is None else str(channel_id).strip()
        if id_text:
            for tokens in self.by_guild.values():
                if _id_hit(tokens, id_text):
                    return True
        if guild_id is not None and str(guild_id).strip():
            return _name_hit(self.by_guild.get(str(guild_id).strip(), ()), channel_name)
        for tokens in self.by_guild.values():
            if _name_hit(tokens, channel_name):
                return True
        return False


def _id_hit(tokens, id_text: str) -> bool:
    for token in tokens:
        if clean_token(token) == id_text:
            return True
    return False


def _name_hit(tokens, channel_name) -> bool:
    name = norm_name(channel_name)
    if not name:
        return False
    for token in tokens:
        if norm_name(token) == name:
            return True
    return False


def _token_hit(tokens, channel_id, channel_name) -> bool:
    id_text = "" if channel_id is None else str(channel_id).strip()
    if id_text and _id_hit(tokens, id_text):
        return True
    return _name_hit(tokens, channel_name)


def ensure_blocklist(excluded) -> Blocklist:
    """Accept a Blocklist or any iterable of global names and ids."""
    if isinstance(excluded, Blocklist):
        return excluded
    if excluded is None:
        return Blocklist()
    return Blocklist(global_channels=list(excluded))


def blocklist_from_settings(data: dict) -> Blocklist:
    if not isinstance(data, dict):
        raise ValueError("settings file must be a table")
    by_guild: dict[str, list[str]] = {}
    block = data.get("blocklist")
    if isinstance(block, dict):
        raw = block.get("guilds")
        if isinstance(raw, dict):
            for gid, channels in raw.items():
                key = str(gid).strip()
                if key:
                    by_guild[key] = string_items(channels)
    return Blocklist(string_items(data.get("excluded_channels")), by_guild)


def normalize_channel_token(raw: str) -> str:
    text = clean_token(raw)
    if not text or any(ch.isspace() for ch in text):
        raise BlocklistError("Pass a channel name or numeric id, without spaces.")
    return text


def normalize_guild_id(raw: str | None) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    if not text.isdigit():
        raise BlocklistError("Guild id must be a numeric snowflake.")
    return text


def load_settings_dict(path: Path) -> dict:
    import tomllib

    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BlocklistError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BlocklistError("settings file must be a table")
    return data


def load_blocklist_file(path: Path) -> Blocklist:
    return blocklist_from_settings(load_settings_dict(path))


def format_blocklist(blocklist: Blocklist) -> str:
    if not blocklist:
        return "Blocklist is empty. No channel is excluded."
    lines = ["Global (every server):"]
    if blocklist.global_channels:
        for item in blocklist.global_channels:
            lines.append(f"  {item}")
    else:
        lines.append("  (none)")
    for gid in sorted(blocklist.by_guild):
        lines.append(f"Guild {gid}:")
        for item in blocklist.by_guild[gid]:
            lines.append(f"  {item}")
    return "\n".join(lines)


def _same(left: str, right: str) -> bool:
    return clean_token(left).casefold() == clean_token(right).casefold()


def _guild_map(data: dict) -> dict[str, list[str]]:
    block = data.get("blocklist")
    if not isinstance(block, dict):
        return {}
    raw = block.get("guilds")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for gid, channels in raw.items():
        items = string_items(channels)
        if items:
            out[str(gid)] = items
    return out


def add_channel(path: Path, channel: str, guild_id: str | None = None) -> tuple[str, Blocklist]:
    """Add a name or id. Returns a status line and the blocklist now on disk."""
    token = normalize_channel_token(channel)
    gid = normalize_guild_id(guild_id)
    data = load_settings_dict(path)
    if gid is None:
        current = string_items(data.get("excluded_channels"))
        if any(_same(item, token) for item in current):
            blocklist = blocklist_from_settings(data)
            return f"{token} is already on the global blocklist.", blocklist
        current.append(token)
        data["excluded_channels"] = string_items(current)
        scope = "in every guild"
    else:
        guilds = _guild_map(data)
        current = list(guilds.get(gid, []))
        if any(_same(item, token) for item in current):
            blocklist = blocklist_from_settings(data)
            return f"{token} is already blocked in guild {gid}.", blocklist
        current.append(token)
        guilds[gid] = string_items(current)
        data["blocklist"] = {"guilds": guilds}
        scope = f"in guild {gid}"
    blocklist = _write(path, data)
    return f"Blocked {token} {scope}.", blocklist


def remove_channel(path: Path, channel: str, guild_id: str | None = None) -> tuple[str, Blocklist]:
    token = normalize_channel_token(channel)
    gid = normalize_guild_id(guild_id)
    data = load_settings_dict(path)
    if gid is None:
        current = string_items(data.get("excluded_channels"))
        kept = [item for item in current if not _same(item, token)]
        if len(kept) == len(current):
            raise BlocklistError(
                f"{token} is not on the global blocklist. "
                "Use --guild GUILD_ID to remove a per-server entry, or blocklist list to see entries."
            )
        data["excluded_channels"] = kept
        scope = "the global blocklist"
    else:
        guilds = _guild_map(data)
        current = list(guilds.get(gid, []))
        kept = [item for item in current if not _same(item, token)]
        if gid not in guilds or len(kept) == len(current):
            raise BlocklistError(f"{token} is not blocked in guild {gid}.")
        if kept:
            guilds[gid] = kept
        else:
            guilds.pop(gid, None)
        if guilds:
            data["blocklist"] = {"guilds": guilds}
        elif isinstance(data.get("blocklist"), dict):
            data["blocklist"] = {"guilds": {}}
        scope = f"guild {gid}"
    blocklist = _write(path, data)
    return f"Removed {token} from {scope}. Logged history stays deleted.", blocklist


_EXCLUDED_LINE = re.compile(r"(?m)^excluded_channels\s*=\s*\[[^\]\n]*\][^\n]*$")
_GUILD_HEADER = re.compile(r"(?m)^\[blocklist\.guilds\][ \t]*$")


def toml_array(items) -> str:
    return "[" + ", ".join(json.dumps(str(item)) for item in items) + "]"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return toml_array(value)
    raise BlocklistError(f"cannot write settings value of type {type(value).__name__}")


def _render_guild_section(guilds: dict[str, list[str]]) -> str:
    lines = ["[blocklist.guilds]"]
    wrote = False
    for gid in sorted(guilds, key=str):
        items = string_items(guilds[gid])
        if not items:
            continue
        wrote = True
        lines.append(f"{json.dumps(str(gid))} = {toml_array(items)}")
    if not wrote:
        return ""
    return "\n".join(lines)


def _replace_guild_section(text: str, guilds: dict[str, list[str]]) -> str:
    body = _render_guild_section(guilds)
    match = _GUILD_HEADER.search(text)
    if match is None:
        if not body:
            return text
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        return text + body + "\n"
    rest = text[match.end() :]
    next_header = re.search(r"(?m)^\[", rest)
    end = match.end() + (next_header.start() if next_header else len(rest))
    if body:
        replacement = body + "\n"
        tail = text[end:]
        if tail.startswith("\n"):
            tail = tail[1:]
        if tail and not replacement.endswith("\n\n"):
            replacement += "\n"
        return text[: match.start()] + replacement + tail
    tail = text[end:]
    head = text[: match.start()]
    merged = head + tail
    merged = re.sub(r"\n{3,}", "\n\n", merged)
    return merged


_HEADER = """# Limits, branding, and channel rules. Edit, save, then ask again or run /admin bot reload.
# excluded_channels is the blocklist: names or numeric channel ids.
# An empty list excludes nothing. Nothing is excluded in code beyond this file.
# Per-guild entries live in [blocklist.guilds]. admin.py blocklist edits this file.
"""


def dump_settings(data: dict) -> str:
    """Full rewrite used when the excluded_channels line is not a single line."""
    lines = [_HEADER.rstrip("\n"), ""]
    emitted: set[str] = set()

    def emit(key: str, value=None) -> None:
        if key not in data and value is None:
            return
        if key in emitted:
            return
        emitted.add(key)
        lines.append(f"{key} = {_toml_value(data[key] if value is None else value)}")

    for key in ("display_title", "org_name"):
        emit(key)
    if lines[-1] != "":
        lines.append("")
    for key in ("max_answer_chars", "max_context_snippets"):
        emit(key)
    lines.append("")
    for key in ("excluded_channels", "allowed_channels", "public_channels"):
        if key not in data:
            continue
        value = string_items(data[key]) if key == "excluded_channels" else data[key]
        emit(key, value)
    guilds = _guild_map(data)
    emitted.add("blocklist")
    section = _render_guild_section(guilds)
    if section:
        lines.append("")
        lines.append(section)
    for key in sorted(data):
        if key in emitted:
            continue
        value = data[key]
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")
    return "\n".join(lines)


def render_settings_text(text: str, data: dict) -> str:
    """Update blocklist entries and keep the rest of the file, including comments."""
    data = dict(data)
    if "excluded_channels" not in data:
        data["excluded_channels"] = []
    items = string_items(data.get("excluded_channels"))
    data["excluded_channels"] = items
    line = "excluded_channels = " + toml_array(items)
    if not text.strip():
        return dump_settings(data)
    has_key = re.search(r"(?m)^excluded_channels\s*=", text)
    if has_key and not _EXCLUDED_LINE.search(text):
        # Multiline or otherwise not a single-line array. Rewrite the file.
        return dump_settings(data)
    if has_key:
        text, count = _EXCLUDED_LINE.subn(line, text, count=1)
        if count != 1:
            return dump_settings(data)
    else:
        allowed = re.compile(r"(?m)^allowed_channels\s*=")
        if allowed.search(text):
            text = allowed.sub(line + "\nallowed_channels =", text, count=1)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
    text = _replace_guild_section(text, _guild_map(data))
    if not text.endswith("\n"):
        text += "\n"
    return text


def set_local_string(path: Path, key: str, value: str) -> None:
    """Set one top-level string in a toml file and keep the other lines."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise BlocklistError("invalid settings key")
    line = f"{key} = {json.dumps(value)}"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write(path: Path, data: dict) -> Blocklist:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    _atomic_write(path, render_settings_text(existing, data))
    return blocklist_from_settings(load_settings_dict(path))
