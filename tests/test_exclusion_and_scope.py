import pytest

from blocklist import Blocklist
from db import purge_excluded, upsert_message
from permissions import (
    INVITE_PERMISSIONS,
    invocation_allowed,
    is_excluded,
    visible_channel_ids,
)
from qa import handle_question
from retrieve import search_messages
from tests.conftest import add, committed_runtime
from voice import RuntimeConfig, get_runtime, set_runtime


def test_invite_permissions_integer():
    assert INVITE_PERMISSIONS == (1 << 10) + (1 << 11) + (1 << 16) + (1 << 31)
    assert INVITE_PERMISSIONS == 2147552256


def test_snowflake_id_accepts_a_missing_guild():
    from permissions import snowflake_id

    assert snowflake_id(None) is None
    assert snowflake_id(type("G", (), {"id": 5})()) == "5"


def test_empty_blocklist_excludes_nothing():
    assert not is_excluded("50", "private-channel", set())
    assert not is_excluded("50", "#Private-Channel", Blocklist())
    assert not is_excluded(None, "private-channel", None)


def test_shipped_config_is_generic_with_an_empty_blocklist(tmp_path):
    runtime = committed_runtime(tmp_path)
    assert runtime.local_settings_path == tmp_path / "committed-defaults" / "config" / "settings.local.toml"
    assert not runtime.local_settings_path.is_file()
    assert runtime.display_title == "Discord bot"
    assert runtime.org_name == "My Org"
    assert "My Org" in runtime.system_prompt
    assert not runtime.blocklist
    assert not is_excluded("50", "private-channel", runtime.blocklist)
    assert not is_excluded(None, "#Private-Channel", runtime.blocklist)
    assert runtime.ask_description() == "Ask about My Org"
    assert len(runtime.ask_description()) <= 100
    assert runtime.tiers == ["non-member", "member", "officer", "executive"]
    assert runtime.tier_roles == {tier: [] for tier in runtime.tiers}
    assert runtime.channel_tiers == {}
    assert runtime.cite_sources is True
    assert runtime.admin_roles == []


def _block(tmp_path, *names):
    root = tmp_path / "voice"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "persona.md").write_text("## System prompt\nUse only the Context block.\n", encoding="utf-8")
    quoted = ", ".join(repr(name).replace("'", '"') for name in names)
    (config / "settings.toml").write_text(
        f'display_title = "Discord bot"\norg_name = "My Org"\nexcluded_channels = [{quoted}]\n',
        encoding="utf-8",
    )
    set_runtime(RuntimeConfig(root))
    return get_runtime()


def test_upsert_refuses_excluded_channel(db, tmp_path):
    _block(tmp_path, "private-channel")
    stored = upsert_message(
        db,
        dict(
            id="x",
            guild_id="g",
            channel_id="50",
            channel_name="private-channel",
            category="Private",
            author_id="1001",
            author_name="ethan",
            display_name="Ethan",
            roles=[],
            timestamp="2026-10-18T15:00:00+00:00",
            edited_timestamp=None,
            content="The budget PIN is 1234",
            attachments=[],
            source="test",
        ),
    )
    assert stored is False
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_search_drops_excluded_rows_even_if_indexed(db, tmp_path):
    _block(tmp_path, "private-channel")
    upsert_message(
        db,
        dict(
            id="secret",
            guild_id="g",
            channel_id="50",
            channel_name="private-channel",
            category="Private",
            author_id="1004",
            author_name="jordan",
            display_name="Jordan",
            roles=[],
            timestamp="2026-10-18T15:00:00+00:00",
            edited_timestamp=None,
            content="The budget PIN is 1234 and the fund is 99999",
            attachments=[],
            source="test",
        ),
        allow_excluded=True,
    )
    add(db, id="ok", content="general shop notes")
    db.commit()
    hits = search_messages(
        db,
        "budget PIN 1234",
        scope={"50", "private-channel", "10", "general"},
        excluded=get_runtime().excluded_channels,
        asker_id="1001",
        author_mode="exclude",
    )
    assert hits == []
    assert "1234" not in " ".join(hit.text for hit in hits)


def test_purge_removes_excluded_messages_and_edits(db, tmp_path):
    _block(tmp_path, "private-channel")
    upsert_message(
        db,
        dict(
            id="secret",
            guild_id="g",
            channel_id="50",
            channel_name="private-channel",
            category="Private",
            author_id="1",
            author_name="a",
            display_name="A",
            roles=[],
            timestamp="2026-10-18T15:00:00+00:00",
            edited_timestamp=None,
            content="PIN 1234",
            attachments=[],
            source="test",
        ),
        allow_excluded=True,
    )
    db.execute(
        "INSERT INTO message_edits VALUES (?,?,?,?)",
        ("secret", "2026-10-18T16:00:00+00:00", "old", "PIN 1234"),
    )
    add(db, id="ok", content="visible note")
    removed = purge_excluded(db)
    db.commit()
    assert removed == 1
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='secret'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_edits").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages_fts WHERE message_id='secret'").fetchone()[0] == 0


def test_other_members_are_held_and_invisible_channels_are_dropped(db, tmp_path):
    add(db, id="mine", author_id="1001", content="I will bring the purchase order form on Friday.")
    add(
        db,
        id="theirs",
        author_id="1002",
        author_name="avery",
        display_name="Avery",
        content="Sunday at 3PM in the Solarium.",
    )
    add(
        db,
        id="hidden",
        channel_id="40",
        channel_name="business",
        author_id="1002",
        content="The hidden reimbursement code is ZX-HIDE.",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime = get_runtime()
    auto = handle_question(
        db,
        "When did I mention the purchase order form?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=_Recording(),
        runtime=runtime,
    )
    assert auto.kind == "answer"
    assert "purchase order form" in auto.context_block
    assert "Solarium" not in auto.context_block
    assert "ZX-HIDE" not in auto.context_block

    held = handle_question(
        db,
        "What did Avery say about the Solarium?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=_Recording(),
        runtime=runtime,
    )
    assert held.kind == "pending"
    assert held.llm_called is False
    assert "ZX-HIDE" not in held.context_block
    assert "Solarium" not in held.text

    direct = handle_question(
        db,
        "When is the Solarium?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=_Recording(),
        runtime=runtime,
    )
    assert direct.kind == "answer"
    assert "ZX-HIDE" not in direct.context_block
    assert "Sunday at 3PM in the Solarium." not in direct.text


def test_visible_channels_fail_closed():
    assert visible_channel_ids(None, object()) == set()

    class Perms:
        def __init__(self, view):
            self.view_channel = view

    class Channel:
        def __init__(self, id, view, threads=()):
            self.id = id
            self.threads = list(threads)
            self._view = view

        def permissions_for(self, member):
            if self._view is None:
                raise RuntimeError("uncached")
            return Perms(self._view)

    guild = type("G", (), {})()
    guild.channels = [Channel(1, True), Channel(2, False), Channel(3, None), Channel(4, True, threads=[Channel(5, True)])]
    guild.threads = []
    assert visible_channel_ids(guild, object()) == {"1", "4", "5"}


def test_invocation_allowlist_and_exclusion():
    excluded = {"private-channel"}

    class Ch:
        def __init__(self, id, name, parent=None):
            self.id = id
            self.name = name
            self.parent = parent

    general = Ch(10, "general")
    thread = Ch(11, "thread", parent=general)
    secret = Ch(50, "private-channel")
    assert invocation_allowed(general, set(), excluded)
    assert invocation_allowed(thread, {"general"}, excluded)
    assert not invocation_allowed(secret, set(), excluded)
    assert not invocation_allowed(secret, {"private-channel"}, excluded)
    assert not invocation_allowed(general, {"business"}, excluded)
    assert invocation_allowed(secret, set(), set())
    per_guild = Blocklist(by_guild={"1": ["leadership"]})
    leadership = Ch(8, "leadership")
    assert not invocation_allowed(leadership, set(), per_guild, "1")
    assert invocation_allowed(leadership, set(), per_guild, "2")


def _message(**overrides):
    row = dict(
        id="secret",
        guild_id="g",
        channel_id="50",
        channel_name="officers-only",
        category="Private",
        author_id="1004",
        author_name="jordan",
        display_name="Jordan",
        roles=[],
        timestamp="2026-10-18T15:00:00+00:00",
        edited_timestamp=None,
        content="The launch code is ALPHA-1",
        attachments=[],
        source="test",
    )
    row.update(overrides)
    return row


def test_user_blocklisted_channel_is_excluded(db, tmp_path):
    root = tmp_path / "root"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "persona.md").write_text("## System prompt\nUse only the Context block.\n", encoding="utf-8")
    (config / "settings.toml").write_text(
        "\n".join(
            [
                'display_title = "Club Bot"',
                'org_name = "Example Club"',
                "max_answer_chars = 600",
                "max_context_snippets = 4",
                'excluded_channels = ["officers-only", "42"]',
                "allowed_channels = []",
                "public_channels = []",
                "",
                "[blocklist.guilds]",
                '"200" = ["leadership"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime = RuntimeConfig(root)
    set_runtime(runtime)
    blocklist = runtime.blocklist
    assert is_excluded("7", "officers-only", blocklist)
    assert is_excluded("7", "#Officers-Only", blocklist)
    assert is_excluded("42", "general", blocklist)
    assert not is_excluded("9", "general", blocklist)
    assert is_excluded("8", "leadership", blocklist, "200")
    assert not is_excluded("8", "leadership", blocklist, "300")
    assert is_excluded("55", "general", Blocklist(by_guild={"200": ["55"]}), "300")

    assert upsert_message(db, _message()) is False
    assert upsert_message(db, _message(id="lead", guild_id="200", channel_id="60", channel_name="leadership")) is False
    assert (
        upsert_message(
            db,
            _message(
                id="other-server",
                guild_id="300",
                channel_id="61",
                channel_name="leadership",
                content="Visible meetup note",
            ),
        )
        is True
    )
    assert db.execute("SELECT COUNT(*) FROM messages WHERE channel_name='officers-only'").fetchone()[0] == 0

    upsert_message(db, _message(id="planted", content="The launch code is ALPHA-1"), allow_excluded=True)
    upsert_message(
        db,
        _message(
            id="planted-guild",
            guild_id="200",
            channel_id="60",
            channel_name="leadership",
            content="The launch code is ALPHA-1 secret",
        ),
        allow_excluded=True,
    )
    db.commit()
    hits = search_messages(
        db,
        "ALPHA-1",
        scope={"50", "60", "61", "officers-only", "leadership"},
        excluded=blocklist,
        asker_id="1001",
        author_mode="exclude",
    )
    assert [hit.channel_id for hit in hits] == []
    visible = search_messages(
        db,
        "meetup",
        scope={"61", "leadership"},
        excluded=blocklist,
        asker_id="1001",
        author_mode="exclude",
    )
    assert [hit.channel_id for hit in visible] == ["61"]
    assert "ALPHA-1" not in " ".join(hit.text for hit in visible)

    removed = purge_excluded(db, blocklist)
    db.commit()
    assert removed >= 2
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='planted'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='planted-guild'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='other-server'").fetchone()[0] == 1


def test_admin_blocklist_add_remove_list_and_purge(db, tmp_path, capsys):
    settings = tmp_path / "settings.toml"
    settings.write_text(
        "\n".join(
            [
                "# keep this comment",
                'display_title = "Club Bot"',
                "max_answer_chars = 600",
                'excluded_channels = ["private-channel"]',
                'allowed_channels = ["general"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    upsert_message(
        db,
        _message(id="room", channel_id="70", channel_name="secret-room", content="do not keep"),
        allow_excluded=True,
    )
    db.commit()
    from admin import main

    db_path = tmp_path / "bot.db"
    assert main(["--db", str(db_path), "--settings", str(settings), "blocklist", "add", "#secret-room"]) == 0
    listed = capsys.readouterr().out
    assert "secret-room" in listed
    assert "Purged 1" in listed
    text = settings.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert 'allowed_channels = ["general"]' in text
    assert 'display_title = "Club Bot"' in text
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='room'").fetchone()[0] == 0

    assert main(["--db", str(db_path), "--settings", str(settings), "blocklist", "add", "--guild", "15", "55"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db_path), "--settings", str(settings), "blocklist", "list"]) == 0
    listing = capsys.readouterr().out
    assert "private-channel" in listing
    assert "secret-room" in listing
    assert "Guild 15" in listing
    assert "55" in listing

    assert main(["--db", str(db_path), "--settings", str(settings), "blocklist", "remove", "secret-room"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db_path), "--settings", str(settings), "blocklist", "remove", "--guild", "15", "55"]) == 0
    text = settings.read_text(encoding="utf-8")
    assert "secret-room" not in text
    assert "private-channel" in text
    assert "[blocklist.guilds]" not in text
    assert "# keep this comment" in text


def test_blocklist_add_rewrites_a_multiline_array(tmp_path):
    import re

    from blocklist import add_channel, load_blocklist_file

    settings = tmp_path / "settings.toml"
    settings.write_text(
        'display_title = "Club Bot"\nexcluded_channels = [\n  "keep-me",\n]\nallowed_channels = ["general"]\n',
        encoding="utf-8",
    )
    status, _blocklist = add_channel(settings, "also")
    text = settings.read_text(encoding="utf-8")
    assert "Blocked also" in status
    assert len(re.findall(r"(?m)^excluded_channels\s*=", text)) == 1
    loaded = load_blocklist_file(settings)
    assert is_excluded("1", "keep-me", loaded)
    assert is_excluded("1", "also", loaded)
    assert "Club Bot" in text
    assert "general" in text


def test_env_names_load(monkeypatch, tmp_path):
    from config import load_config, watching_guild

    chosen = tmp_path / "bot.db"
    monkeypatch.setenv("BOT_DB", str(chosen))
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("SNAPSHOT_HOURS", "3")
    monkeypatch.setenv("GUILD_ID", "42")
    cfg = load_config()
    assert cfg.db_path == str(chosen)
    assert cfg.docs_dir == str(tmp_path / "notes")
    assert cfg.snapshot_hours == 3
    assert cfg.guild_ids == ("42",)
    assert watching_guild("42", cfg.guild_ids)
    assert not watching_guild("7", cfg.guild_ids)
    assert watching_guild("7", ())
    assert not watching_guild(None, ())

    monkeypatch.setenv("GUILD_IDS", "1, 2, 1")
    monkeypatch.setenv("BOT_DB", str(tmp_path / "new.db"))
    cfg = load_config()
    assert cfg.guild_ids == ("1", "2")
    assert cfg.db_path == str(tmp_path / "new.db")


def test_blocklist_default_file_is_the_local_override(tmp_path):
    from pathlib import Path

    from admin import settings_path

    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.toml").write_text('excluded_channels = []\n', encoding="utf-8")
    set_runtime(RuntimeConfig(root))
    assert settings_path(None) == root / "config" / "settings.local.toml"
    assert settings_path("custom.toml") == Path("custom.toml")


class _Recording:
    name = "recording"

    def __init__(self):
        self.blocks = []

    def answer(self, question, context_block):
        self.blocks.append(context_block)
        return "Recorded."
