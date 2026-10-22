import asyncio
import types

import discord

import catchup
from db import connect, upsert_message


def _row(mid, ch="10", name="general"):
    return dict(id=str(mid), guild_id="1", channel_id=ch, channel_name=name, category=None,
                author_id="5", author_name="a", display_name="A", roles=[],
                timestamp="2026-10-22T00:00:00+00:00", edited_timestamp=None,
                content=f"m{mid}", attachments=[], source="live")


class FakeChan:
    def __init__(self, cid, name, msgs, forbid=False):
        self.id, self.name, self.msgs, self.forbid, self.parent = cid, name, msgs, forbid, None
        self.seen_after = None

    def permissions_for(self, _):
        return types.SimpleNamespace(read_message_history=True)

    async def archived_threads(self, limit=None):
        if False:
            yield None

    async def history(self, after=None, limit=None, oldest_first=True):
        self.seen_after = after.id
        if self.forbid:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no")
        for m in [m for m in self.msgs if m > after.id][:limit]:
            yield m


def test_catchup_fetches_after_newest_and_skips(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    for r in (_row(100), _row(200, "20", "other"), _row(300, "30", "secret")):
        upsert_message(conn, r, allow_excluded=True)
    conn.commit(); conn.close()
    a = FakeChan(10, "general", [100, 101, 102])
    b = FakeChan(20, "other", [201], forbid=True)
    c = FakeChan(30, "secret", [301])
    d = FakeChan(40, "empty", [401])
    guild = types.SimpleNamespace(id=1, me=None, text_channels=[a, b, c, d], threads=[])
    client = types.SimpleNamespace(guilds=[guild])
    msg_row = lambda m: _row(m, str(10), "general")
    stats = asyncio.run(catchup.run_catchup(client, db, msg_row, ["secret"]))
    assert a.seen_after == 100 and c.seen_after is None and d.seen_after is None
    assert stats["saved"] == 2 and stats["errors"] == 1 and stats["skipped"] == 1
    conn = connect(db)
    assert conn.execute("select count(*) from messages where channel_id='10'").fetchone()[0] == 3
