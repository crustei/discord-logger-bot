import pytest
from db import get_approval
from qa import handle_question
from tests.conftest import add
from voice import get_runtime


class Recording:
    def __init__(self):
        self.calls = 0

    def answer(self, question, context_block):
        self.calls += 1
        return "From the notes."


@pytest.mark.messages
def test_pending_stores_context_and_does_not_call_the_model(db, tmp_path):
    add(
        db,
        id="theirs",
        author_id="1002",
        author_name="avery",
        display_name="Avery",
        content="Sunday at 3PM in the Solarium.",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    llm = Recording()
    runtime = get_runtime()
    result = handle_question(
        db,
        "What did Avery say about the Solarium?",
        asker_id="1001",
        asker_name="Ethan",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=llm,
        runtime=runtime,
    )
    assert result.kind == "pending"
    assert result.approval_id is not None
    assert llm.calls == 0
    assert "Solarium" not in result.text
    row = get_approval(db, result.approval_id)
    assert row["status"] == "pending"
    assert "Solarium" in row["proposed_context"]
    assert row["question"].startswith("What did Avery say")


@pytest.mark.messages
def test_approve_posts_citation_and_deny_does_not_leak(db, tmp_path):
    from admin import approve_request, deny_request

    add(
        db,
        id="theirs",
        author_id="1002",
        display_name="Avery",
        content="Sunday at 3PM in the Solarium.",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime = get_runtime()
    result = handle_question(
        db,
        "What did Avery say about the Solarium?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
    )
    posted = []

    def poster(channel_id, content):
        posted.append((channel_id, content))
        return "999"

    text = approve_request(db, result.approval_id, Recording(), poster)
    assert posted[0][0] == "10"
    assert "https://discord.com/channels/" in text  # bare jump link footer
    assert "#general" not in text
    assert len(text) <= runtime.max_answer_chars
    assert get_approval(db, result.approval_id)["status"] == "approved"
    with pytest_raises_admin():
        approve_request(db, result.approval_id, Recording(), poster)

    held = handle_question(
        db,
        "What did Avery say about the Solarium?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10"},
        public_channels=set(),
        here={"10", "general"},
        excluded=runtime.excluded_channels,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
    )
    posted.clear()
    deny_request(db, held.approval_id, reason="The PIN is 1234", poster=poster)
    assert "1234" not in posted[0][1]
    assert "Solarium" not in posted[0][1]
    assert get_approval(db, held.approval_id)["status"] == "denied"


def test_approve_strips_tampered_private_context(db, tmp_path):
    import json

    from admin import AdminError, approve_request
    from tests.test_exclusion_and_scope import _block

    _block(tmp_path, "private-channel")
    db.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES ('2026-10-20T00:00:00+00:00', 'pending', 'g', '10', 'general', '1001', 'Ethan',
                   'What is the PIN?', ?, ?)""",
        (
            json.dumps(
                [
                    {
                        "kind": "message",
                        "text": "The budget PIN is 1234",
                        "channel_id": "50",
                        "channel_name": "private-channel",
                        "author_id": "1004",
                        "author": "Jordan",
                        "timestamp": "2026-10-18T15:00:00+00:00",
                    }
                ]
            ),
            json.dumps(["10", "50", "general", "private-channel"]),
        ),
    )
    db.commit()
    approval_id = db.execute("SELECT id FROM approvals").fetchone()[0]
    posted = []
    with pytest_raises_admin(AdminError):
        approve_request(db, approval_id, Recording(), lambda channel, content: posted.append(content) or "1")
    assert posted == []
    assert get_approval(db, approval_id)["status"] == "pending"


def test_forget_deletes_one_members_rows_and_leaves_others(db, tmp_path, capsys):
    import json

    from admin import main

    add(db, id="mine", author_id="1001", content="my note stays searchable")
    add(db, id="theirs", author_id="1002", author_name="avery", content="leave avery's note")
    db.execute(
        "INSERT INTO message_edits VALUES (?,?,?,?)",
        ("mine", "2026-10-18T16:00:00+00:00", "old", "my note stays searchable"),
    )
    db.execute(
        "INSERT INTO member_snapshots VALUES (?,?,?,?,?,?,?)",
        ("2026-10-18T00:00:00+00:00", "1001", "ethan", "Ethan", "[]", None, 0),
    )
    db.execute(
        "INSERT INTO member_snapshots VALUES (?,?,?,?,?,?,?)",
        ("2026-10-18T00:00:00+00:00", "1002", "avery", "Avery", "[]", None, 0),
    )
    db.execute(
        "INSERT INTO member_events VALUES (?,?,?,?,?)",
        ("2026-10-18T00:00:00+00:00", "1001", "ethan", "join", ""),
    )
    db.execute(
        "INSERT INTO role_snapshots VALUES (?,?,?,?,?)",
        ("2026-10-18T00:00:00+00:00", "9", "Member", 1, 2),
    )
    db.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES ('2026-10-20T00:00:00+00:00', 'pending', 'g', '10', 'general', '1001', 'Ethan',
                   'my question', '[]', '[]')"""
    )
    db.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES ('2026-10-20T00:00:00+00:00', 'pending', 'g', '10', 'general', '1002', 'Avery',
                   'quote them', ?, '[]')""",
        (
            json.dumps(
                [
                    {
                        "kind": "message",
                        "text": "my note stays searchable",
                        "author_id": "1001",
                        "channel_id": "10",
                        "channel_name": "general",
                    }
                ]
            ),
        ),
    )
    db.execute(
        """INSERT INTO approvals(
             created_at, status, guild_id, channel_id, channel_name, asker_id, asker_name,
             question, proposed_context, scope_json)
           VALUES ('2026-10-20T00:00:00+00:00', 'pending', 'g', '10', 'general', '1002', 'Avery',
                   'unrelated', ?, '[]')""",
        (json.dumps([{"kind": "message", "text": "leave avery's note", "author_id": "1002"}]),),
    )
    db.commit()
    db_path = tmp_path / "bot.db"
    assert main(["--db", str(db_path), "forget", "ethan"]) == 1
    assert "numeric" in capsys.readouterr().err
    assert main(["--db", str(db_path), "forget", "1001"]) == 0
    out = capsys.readouterr().out
    assert "messages: 1" in out
    assert "member snapshots: 1" in out
    assert "member events: 1" in out
    assert "approval rows: 2" in out
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages_fts WHERE author_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_edits").fetchone()[0] == 0
    assert db.execute("SELECT content FROM messages WHERE id='theirs'").fetchone()[0] == "leave avery's note"
    assert db.execute("SELECT COUNT(*) FROM member_snapshots WHERE user_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM member_snapshots WHERE user_id='1002'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM member_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM role_snapshots").fetchone()[0] == 1
    remaining = db.execute("SELECT question FROM approvals").fetchall()
    assert [row["question"] for row in remaining] == ["unrelated"]


def pytest_raises_admin(exc=Exception):
    import pytest

    return pytest.raises(exc)
