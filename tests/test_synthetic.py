import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_local"))
import synthetic_test as st  # noqa: E402
from db import connect, add_opt_out, is_opted_out, upsert_message  # noqa: E402


def _row(i, author):
    return dict(id=str(10**17 + i), guild_id="1", channel_id="5", channel_name="c", category=None,
                author_id=author, author_name="a", display_name="a", roles=[], timestamp="2026-01-01T00:00:00+00:00",
                edited_timestamp=None, content=f"message {i} about rockets", attachments=[], source="live")


def test_reply_format():
    assert st.reply_format_ok("I don't know.")[0]
    assert st.reply_format_ok("Aurora is 40 kg.\n\nBased on: <https://x/1>, <https://x/2>")[0]
    assert not st.reply_format_ok("x\n\nBased on: a\nb")[0]
    assert not st.reply_format_ok("")[0]


def test_optout_add_is_fast_and_persists(tmp_path):
    conn = connect(tmp_path / "b.db")
    for i in range(4000):
        upsert_message(conn, _row(i, "42"))
    conn.commit()
    t0 = time.monotonic()
    add_opt_out(conn, "42")
    assert time.monotonic() - t0 < 3
    conn.close()
    conn = connect(tmp_path / "b.db")
    assert is_opted_out(conn, "42")
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE author_id='42'").fetchone()[0] == 0
    upsert_message(conn, _row(99999, "42"))
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE author_id='42'").fetchone()[0] == 0


def test_harness_runs_every_command_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "guild_id_from_config", lambda: 111111111111111111)
    db = tmp_path / "src.db"
    c = connect(db)
    upsert_message(c, _row(1, "77"))
    c.commit(); c.close()
    s = st.run_once(use_llm=False, connectivity_checks=False, db_src=str(db))
    bad = [r for r in s["results"] if not r["ok"]]
    assert not bad, bad
    assert s["gemini_calls"] == 0
    names = {r["name"] for r in s["results"]}
    assert "every command exercised" in names and "admin optout add (persisted)" in names


def test_write_results_merges_status(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(st, "BOT_LOG", tmp_path / "bot.log")
    (tmp_path / "status.json").write_text('{"ok": true, "checks": {}}')
    line = st.write_results({"ok": False, "passed": 1, "total": 2, "gemini_calls": 0,
                             "timestamp": "2026-10-22T00:00:00+00:00",
                             "results": [{"name": "x", "ok": False, "detail": "boom"}]})
    assert "ERROR synthetic FAIL 1/2" in line and "x: boom" in line
    import json
    s = json.loads((tmp_path / "status.json").read_text())
    assert s["ok"] is True and s["synthetic"]["passed"] == 1
    assert (tmp_path / "bot.log").read_text().count("\n") == 1
