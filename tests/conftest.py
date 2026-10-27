import shutil
import os
os.environ.setdefault("LLM_BACKOFF_SCALE", "0")
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
    if not SAMPLE_DB.exists():
        import subprocess, sys
        subprocess.run([sys.executable, str(ROOT / "sample" / "build_sample.py")], check=True)
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


@pytest.fixture(autouse=True)
def legacy_message_answers(request, monkeypatch):
    """Tests marked ``messages`` exercise the opt-in answer_from = "knowledge_and_messages" path."""
    if request.node.get_closest_marker("messages") is not None:
        original = voice.RuntimeConfig.__init__

        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            self.answer_from = "knowledge_and_messages"

        monkeypatch.setattr(voice.RuntimeConfig, "__init__", init)
    yield


def pytest_configure(config):
    config.addinivalue_line("markers", "messages: raw-message answers turned on (legacy opt-in mode)")


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolate_counters(tmp_path, monkeypatch):
    """Tests must never touch the live diag/counters.json (they once logged fake bot restarts)."""
    import counters
    monkeypatch.setattr(counters, "PATH", tmp_path / "counters.json")
    monkeypatch.setattr(counters.bump, "__defaults__", ("", tmp_path / "counters.json", False))
    monkeypatch.setattr(counters.on_start, "__defaults__", (tmp_path / "counters.json", None))


@pytest.fixture(autouse=True)
def _isolated_role_store(tmp_path, monkeypatch):
    """Never read or write the real data/role_tiers.json in tests."""
    import tiers
    monkeypatch.setattr(tiers, "ROLE_FILE", tmp_path / "role_tiers.json")
    tiers._store_cache.update(key=None, data={})
