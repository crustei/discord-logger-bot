import asyncio
import json

import feedback
from asklog import record
from db import add_opt_out, connect
from feedback import flagged_since, record_vote, tally, vote_in_new_connection
from qa import Result


def _ask(conn, q="When are GMs?"):
    return record(conn, q, Result(kind="answer", reason="", context_block="", text="Fridays.", llm_called=True), asker_id="1")


def test_non_officer_rejected_and_nothing_stored(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn)
    for tier in ("member", "non-member"):
        assert record_vote(conn, rid, 5, tier, "yes") == "not_officer"
    assert tally(conn, rid)["votes"] == 0


def test_one_vote_per_officer_changeable(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn)
    assert record_vote(conn, rid, 10, "officer", "yes") == "saved"
    assert record_vote(conn, rid, 10, "officer", "no", "It's Thursdays") == "changed"
    assert record_vote(conn, rid, 11, "officer", "partly") == "saved"
    t = tally(conn, rid)
    assert (t["yes"], t["partly"], t["no"], t["votes"]) == (0, 1, 1, 2)
    flagged = flagged_since(conn, "2000-01-01")
    assert {f["vote"] for f in flagged} == {"no", "partly"}
    assert any(f["note"] == "It's Thursdays" for f in flagged)


def test_linked_to_ask_log_and_no_raw_ids(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn)
    assert record_vote(conn, 9999, 10, "officer", "yes") == "no_question"
    record_vote(conn, rid, 123456789012345678, "officer", "yes")
    row = conn.execute("SELECT * FROM ask_feedback").fetchone()
    assert row[0] == rid and "123456789012345678" not in json.dumps(list(row))
    s = conn.execute("SELECT verdict FROM ask_feedback_summary WHERE ask_id=?", (rid,)).fetchone()
    assert s[0] == "verified"
    # ask_log itself is unchanged in shape (additive migration)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ask_log)")]
    assert cols == ["id", "ts", "question", "answer", "decision", "confidence", "sources"]


def test_opted_out_officer_not_stored(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn)
    add_opt_out(conn, "77")
    assert record_vote(conn, rid, 77, "officer", "yes") == "opted_out"
    assert tally(conn, rid)["votes"] == 0


def test_persists_across_restart(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    rid = _ask(conn)
    record_vote(conn, rid, 10, "officer", "yes")
    conn.close()
    # new process: same salt -> same officer still counts once, vote changes
    conn2 = connect(path)
    assert record_vote(conn2, rid, 10, "officer", "partly") == "changed"
    assert tally(conn2, rid)["votes"] == 1
    # buttons are rebuilt from custom_id alone (DynamicItem), no in-memory state
    import re
    m = re.fullmatch(feedback.CUSTOM_ID_RE, f"qav:{rid}:no")
    item = asyncio.run(feedback.VoteButton.from_custom_id(None, None, m))
    assert item.ask_id == rid and item.vote == "no" and item.item.custom_id == f"qav:{rid}:no"
    assert len(item.item.custom_id) <= 100


def test_vote_view_has_three_buttons():
    async def build():
        return feedback.vote_view(42)
    view = asyncio.run(build())
    ids = [c.custom_id for c in view.children]
    assert ids == ["qav:42:yes", "qav:42:partly", "qav:42:no"] and view.timeout is None


def test_wrapper_returns_thanks_and_badge_targets(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    rid = _ask(conn)
    feedback.link_message(conn, rid, 555, 66)
    conn.close()
    assert vote_in_new_connection(path, rid, 3, "member", "yes", None)[1] == feedback.NOT_OFFICER
    out, text, targets = vote_in_new_connection(path, rid, 3, "officer", "yes", None)
    assert (out, text, targets) == ("saved", feedback.THANKS, [])  # 1 yes < default 2
    out, text, targets = vote_in_new_connection(path, rid, 4, "officer", "yes", None)
    assert targets == [("555", "66")]


def test_message_link_and_lookup(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn)
    feedback.link_message(conn, rid, 123, 9)
    assert feedback.ask_id_for_message(conn, 123) == rid and feedback.ask_id_for_message(conn, 1) is None


def test_threshold_and_badge_text():
    assert feedback.is_verified({"yes": 2, "no": 0}, 2, 0)
    assert not feedback.is_verified({"yes": 1, "no": 0}, 2, 0)
    assert not feedback.is_verified({"yes": 5, "no": 1}, 2, 0)
    t = feedback.badged_text("Fridays at 6.\n\nBased on: <https://x/1>")
    assert t == "Fridays at 6.\n\n" + feedback.BADGE
    assert feedback.badged_text(t) == t


class _Msg:
    def __init__(self, content, fail=False):
        self.content, self.fail, self.edits = content, fail, []

    async def edit(self, content):
        if self.fail:
            raise RuntimeError("cannot edit")
        self.edits.append(content)
        self.content = content


class _Chan:
    def __init__(self, msg):
        self.msg = msg

    async def fetch_message(self, mid):
        return self.msg


class _Client:
    def __init__(self, msg):
        self.chan = _Chan(msg)

    def get_channel(self, cid):
        return self.chan


def test_apply_badge_edits_once_and_survives_failure(caplog):
    m = _Msg("Answer.\n\nBased on: <https://x>")
    assert asyncio.run(feedback.apply_badge(_Client(m), [("1", "2")])) == 1
    assert m.content.endswith(feedback.BADGE) and "Based on" not in m.content
    assert asyncio.run(feedback.apply_badge(_Client(m), [("1", "2")])) == 0  # already badged
    bad = _Msg("x", fail=True)
    assert asyncio.run(feedback.apply_badge(_Client(bad), [("1", "2")])) == 0
    assert any("badge edit failed" in r.message for r in caplog.records)


def test_finish_vote_replaces_prompt_with_thanks(tmp_path):
    from types import SimpleNamespace as N
    path = str(tmp_path / "t.db")
    conn = connect(path)
    rid = _ask(conn)
    conn.close()
    calls = []

    class Resp:
        async def edit_message(self, **kw):
            calls.append(("edit", kw))

        async def send_message(self, text, **kw):
            calls.append(("send", text))

    officer = N(id=8, roles=[])
    inter = N(client=N(cfg=N(db_path=path)), user=officer, response=Resp())
    import tiers
    orig = tiers.member_tier
    tiers.member_tier = lambda u, m=None: "officer"
    try:
        asyncio.run(feedback.finish_vote(inter, rid, "yes", None))
    finally:
        tiers.member_tier = orig
    assert calls == [("edit", {"content": feedback.THANKS, "view": None})]


def test_fallback_matches_pre_link_answer_once(tmp_path):
    import datetime as dt
    conn = connect(str(tmp_path / "t.db"))
    rid = _ask(conn, "Who was chief engineer?")
    other = record(conn, "x", Result(kind="answer", reason="", context_block="", text="Different.", llm_called=True), asker_id="1")
    now = dt.datetime.now(dt.timezone.utc)
    assert feedback.resolve_message(conn, 900, "Fridays.", now, 5) == rid
    assert feedback.ask_id_for_message(conn, 900) == rid  # linked for next time
    assert feedback.find_by_content(conn, "Fridays.", now - dt.timedelta(hours=2)) is None  # outside window
    assert feedback.find_by_content(conn, "nope", now) is None
    _ask(conn, "dup")  # second identical answer -> ambiguous
    assert feedback.find_by_content(conn, "Fridays.", now) is None
    assert other
