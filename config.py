"""Environment configuration. Nothing here reads secrets from source files.

Call ``load_dotenv()`` once from a command entry point (``bot.py``, ``admin.py``).
Library code reads ``os.environ`` so tests can set variables without a .env file.

Channel rules live in ``config/settings.toml``. An optional gitignored
``config/settings.local.toml`` overrides those keys. An empty blocklist excludes nothing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def load_dotenv(path: str | None = None) -> None:
    """Load KEY=value lines. Existing environment variables win."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def excluded_channels():
    """Effective blocklist from settings plus the local override.

    Re-read when either file changes. Empty unless a file lists channels.
    Names in ``excluded_channels`` apply to every guild. Per-guild names live
    under ``[blocklist.guilds]``.
    """
    from voice import get_runtime

    return get_runtime().blocklist


def default_db_path() -> str:
    """SQLite path from ``BOT_DB``, or ``bot.db`` in this directory."""
    chosen = _first_env("BOT_DB")
    if chosen:
        return chosen
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


def default_docs_dir() -> str:
    chosen = _first_env("DOCS_DIR")
    if chosen:
        return chosen
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


def snapshot_hours() -> float:
    raw = _first_env("SNAPSHOT_HOURS") or "6"
    try:
        value = float(raw)
    except ValueError:
        return 6.0
    if value <= 0:
        return 6.0
    return value


def configured_guild_ids() -> tuple[str, ...]:
    """Guilds to watch. Empty means every guild the bot is invited to.

    ``GUILD_IDS`` is a comma-separated list. ``GUILD_ID`` is a single-id alias
    used when ``GUILD_IDS`` is unset.
    """
    raw = _first_env("GUILD_IDS", "GUILD_ID") or ""
    found: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part or part in seen:
            continue
        seen.add(part)
        found.append(part)
    return tuple(found)


def watching_guild(guild_id, configured: tuple[str, ...] | None = None) -> bool:
    """True when this guild should be logged. DMs (no guild id) are not watched.

    An empty configured list watches every guild.
    """
    if configured is None:
        configured = configured_guild_ids()
    if guild_id is None or str(guild_id).strip() == "":
        return False
    if not configured:
        return True
    return str(guild_id) in set(configured)


@dataclass(frozen=True)
class Config:
    """Paths and secrets. Channel rules, title, and org name live in settings files."""

    db_path: str
    docs_dir: str
    snapshot_hours: float
    guild_ids: tuple[str, ...]


def load_config() -> Config:
    return Config(
        db_path=default_db_path(),
        docs_dir=default_docs_dir(),
        snapshot_hours=snapshot_hours(),
        guild_ids=configured_guild_ids(),
    )
