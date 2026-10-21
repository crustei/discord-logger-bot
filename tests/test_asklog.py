import json
import sqlite3

from asklog import confidence_for, record
from db import add_opt_out, connect
from qa import Result
from retrieve import Hit


def _conn(tmp_path):
    return connect(str(tmp_path / "t.db"))


def test_row_has_no_asker_or_channel(tmp_path):
    conn = _conn(tmp_path)
    hits = [Hit(kind="doc", text="GMs Fridays", title="facts", score=4.0),
            Hit(kind="message", text="GM friday", channel_name="general", timestamp="2026-10-01T00:00:00", author_id="42", author="bob", score=2.0)]
    rid = record(conn, "When are GMs?", Result(kind="answer", reason="", context_block="", snippets=hits, text="Fridays.", llm_called=True), asker_id="42")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ask_log)")]
    assert cols == ["id", "ts", "question", "answer", "decision", "confidence", "sources"]
    row = conn.execute("SELECT * FROM ask_log WHERE id=?", (rid,)).fetchone()
    blob = json.dumps(list(row))
    assert "42" not in blob and "bob" not in blob
    assert row[4] == "answered" and 0 < row[5] <= 1


def test_opted_out_not_logged(tmp_path):
    conn = _conn(tmp_path)
    add_opt_out(conn, "7")
    assert record(conn, "q", Result(kind="empty", reason="", context_block="", text="idk"), asker_id="7") is None


def test_dont_know_zero_confidence():
    assert confidence_for(Result(kind="empty", reason="", context_block="")) == 0.0
