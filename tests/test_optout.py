import pytest
import asyncio
import json

from db import (
    add_opt_out,
    is_opted_out,
    record_edit,
    record_member_event,
    record_member_snapshot,
    upsert_message,
)
from qa import handle_question
from retrieve import search_messages
from tests.conftest import add, committed_runtime
from voice import RuntimeConfig


def _message(**overrides):
    row = dict(
        id="m1",
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
        content="The launch code is purple",
        attachments=[],
        source="test",
    )
    row.update(overrides)
    return row


def test_shipped_settings_enable_optout_without_a_user_list(tmp_path):
    runtime = committed_runtime(tmp_path)
    assert runtime.optout_command is True
    text = runtime.settings_path.read_text(encoding="utf-8")
    assert "optout_command = true" in text
    assert "opt_outs" not in text


def test_optout_command_defaults_on_and_local_file_can_turn_it_off(tmp_path):
    root = tmp_path / "root"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "settings.toml").write_text('display_title = "Discord bot"\n', encoding="utf-8")
    runtime = RuntimeConfig(root)
    assert runtime.optout_command is True
    (config / "settings.local.toml").write_text("optout_command = false\n", encoding="utf-8")
    assert runtime.reload_if_changed() is True
    assert runtime.optout_command is False


def test_optout_add_purges_and_refuses_new_rows(db, tmp_path, capsys):
    from admin import main

    add(db, id="mine", author_id="1001", content="The launch code is purple")
    add(db, id="theirs", author_id="1002", author_name="avery", content="Meet at 3PM")
    db.execute(
        "INSERT INTO message_edits VALUES (?,?,?,?)",
        ("mine", "2026-10-18T16:00:00+00:00", "old", "The launch code is purple"),
    )
    assert record_member_snapshot(db, "2026-10-18T00:00:00+00:00", "1001", "ethan", "Ethan", "[]", None, 0)
    assert record_member_event(db, "1001", "ethan", "join", "", "2026-10-18T00:00:00+00:00")
    db.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES ('2026-10-20T00:00:00+00:00', 'pending', 'g', '10', 'general', '1002', 'Avery',
                   'quote', ?, '[]')""",
        (json.dumps([{"kind": "message", "text": "The launch code is purple", "author_id": "1001"}]),),
    )
    db.commit()
    db_path = tmp_path / "bot.db"
    assert main(["--db", str(db_path), "optout", "add", "not-an-id"]) == 1
    assert "numeric" in capsys.readouterr().err
    assert main(["--db", str(db_path), "optout", "add", "1001"]) == 0
    out = capsys.readouterr().out
    assert "Opted out 1001" in out
    assert "messages: 1" in out
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_edits").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM member_snapshots WHERE user_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM member_events WHERE user_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert db.execute("SELECT content FROM messages WHERE id='theirs'").fetchone()[0] == "Meet at 3PM"
    assert is_opted_out(db, "1001")
    assert upsert_message(db, _message()) is False
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    assert record_member_snapshot(db, "2026-10-18T01:00:00+00:00", "1001", "ethan", "Ethan", "[]", None, 0) is False
    assert record_member_event(db, "1001", "ethan", "leave", "", "2026-10-18T01:00:00+00:00") is False
    assert main(["--db", str(db_path), "optout", "list"]) == 0
    assert "1001" in capsys.readouterr().out


def test_optout_remove_allows_logging_again(db, tmp_path, capsys):
    from admin import main

    add_opt_out(db, "1001")
    db_path = tmp_path / "bot.db"
    assert main(["--db", str(db_path), "optout", "remove", "1001"]) == 0
    assert "Removed 1001" in capsys.readouterr().out
    assert is_opted_out(db, "1001") is False
    assert upsert_message(db, _message()) is True
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='m1'").fetchone()[0] == 1
    db.commit()
    assert main(["--db", str(db_path), "optout", "remove", "1001"]) == 1


def test_edit_of_an_opted_out_author_is_dropped(db):
    add(db, id="mine", author_id="1001", content="The launch code is purple")
    db.execute(
        "INSERT INTO opt_outs(user_id, created_at) VALUES (?, ?)",
        ("1001", "2026-10-21T00:00:00+00:00"),
    )
    db.commit()
    record_edit(
        db,
        "mine",
        "The launch code is now red",
        "2026-10-18T16:00:00+00:00",
        _message(content="The launch code is now red", edited_timestamp="2026-10-18T16:00:00+00:00"),
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='mine'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_edits").fetchone()[0] == 0


def test_search_skips_opted_out_authors_even_if_a_row_remains(db):
    add(db, id="mine", author_id="1001", content="The launch code is purple")
    add(db, id="theirs", author_id="1002", author_name="avery", content="Meet at 3PM in the hall")
    db.execute(
        "INSERT INTO opt_outs(user_id, created_at) VALUES (?, ?)",
        ("1001", "2026-10-21T00:00:00+00:00"),
    )
    db.commit()
    hidden = search_messages(
        db,
        "launch code purple",
        scope={"10", "general"},
        excluded=set(),
        asker_id="1002",
        author_mode="exclude",
    )
    assert hidden == []
    visible = search_messages(
        db,
        "Meet at 3PM",
        scope={"10", "general"},
        excluded=set(),
        asker_id="1001",
        author_mode="exclude",
    )
    assert [hit.author_id for hit in visible] == ["1002"]
    own = search_messages(
        db,
        "launch code purple",
        scope={"10", "general"},
        excluded=set(),
        asker_id="1001",
        author_mode="only",
    )
    assert own == []


@pytest.mark.messages
def test_opted_out_member_can_ask_without_their_old_messages(db, tmp_path):
    from voice import get_runtime

    add(db, id="mine", author_id="1001", content="The launch code is purple")
    add(db, id="theirs", author_id="1002", author_name="avery", display_name="Avery", content="Meet at 3PM in the hall")
    add_opt_out(db, "1001")
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime = get_runtime()
    held = handle_question(
        db,
        "When do we meet?",
        asker_id="1001",
        asker_name="Ethan",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=_Recording(),
        runtime=runtime,
    )
    assert held.kind == "answer"
    assert "3PM" in held.context_block
    assert "purple" not in held.context_block
    assert "Meet at 3PM in the hall" not in held.text
    empty = handle_question(
        db,
        "What is the launch code?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=_Recording(),
        runtime=runtime,
    )
    assert empty.kind == "empty"
    assert "purple" not in (empty.context_block or "")


def test_optout_reply_is_the_self_serve_path(db):
    from bot import optout_reply

    add(db, id="mine", author_id="1001", content="The launch code is purple")
    refused = optout_reply(db, "1001", enabled=False)
    assert "turned off" in refused
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 1
    accepted = optout_reply(db, 1001, enabled=True)
    assert "opted out" in accepted.casefold()
    assert "/ask" in accepted
    assert is_opted_out(db, "1001")
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0


def test_optout_slash_command_replies_ephemerally(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "bot.db"))
    from bot import QABot

    add(db, id="mine", author_id="1001", content="The launch code is purple")
    bot = QABot()

    class Response:
        def __init__(self):
            self.sent = []
            self.deferred = None

        async def defer(self, *, ephemeral=False, thinking=False):
            self.deferred = (ephemeral, thinking)

        async def send_message(self, text, *, ephemeral=False):
            self.sent.append((text, ephemeral))

    class Interaction:
        def __init__(self):
            self.guild = type("G", (), {"id": 1})()
            self.user = type("U", (), {"id": 1001})()
            self.response = Response()
            self.followup = type("F", (), {})()
            self.followup.sent = []

            async def send(text, *, ephemeral=False):
                self.followup.sent.append((text, ephemeral))

            self.followup.send = send

    interaction = Interaction()
    asyncio.run(bot.handle_optout(interaction))
    assert interaction.response.deferred == (True, False)
    assert interaction.response.sent == []
    assert len(interaction.followup.sent) == 1
    text, ephemeral = interaction.followup.sent[0]
    assert ephemeral is True
    assert "opted out" in text.casefold()
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    bot.db.close()


class _Recording:
    name = "recording"

    def answer(self, question, context_block):
        return "Recorded."
