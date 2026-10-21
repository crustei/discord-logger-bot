import shutil
from pathlib import Path

import pytest

import voice
from db import connect, upsert_message

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DB = ROOT / "sample" / "sample.db"


def committed_runtime(tmp_path):
    """Load the committed persona and settings, with no local override.

    Copies ``config/settings.toml`` and ``config/persona.md`` into a temp
    root. ``config/settings.local.toml`` is left behind, so a deployment
    file cannot change these assertions.
    """
    root = tmp_path / "committed-defaults"
    dest = root / "config"
    dest.mkdir(parents=True)
    source = ROOT / "config"
    for name in ("settings.toml", "persona.md"):
        shutil.copy(source / name, dest / name)
    runtime = voice.RuntimeConfig(root)
    voice.set_runtime(runtime)
    return runtime


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch, tmp_path):
    for key in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "DISCORD_BOT_TOKEN",
        "BOT_DB",
        "DOCS_DIR",
        "SNAPSHOT_HOURS",
        "GUILD_IDS",
        "GUILD_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_LOG", str(tmp_path / "bot.log"))
    voice.set_runtime(None)
    yield
    voice.set_runtime(None)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "bot.db")
    yield conn
    conn.close()


@pytest.fixture
def sample_db(tmp_path):
    dest = tmp_path / "sample.db"
    shutil.copy(SAMPLE_DB, dest)
    conn = connect(dest)
    yield conn
    conn.close()


def add(conn, **overrides):
    row = dict(
        id="1",
        guild_id="g",
        channel_id="10",
        channel_name="general",
        category="General",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        roles=[],
        timestamp="2026-10-18T15:00:00+00:00",
        edited_timestamp=None,
        content="hello from general",
        attachments=[],
        source="test",
    )
    row.update(overrides)
    assert upsert_message(conn, row) is True
    conn.commit()
    return row
