"""Discord handlers acknowledge first and do SQLite work off the event loop."""

import asyncio
import datetime as dt
import os
import tempfile
from pathlib import Path

from tests.conftest import add


def ask_defer_problems() -> list[str]:
    """`/ask` defers before settings reload or the blocklist purge."""
    from bot import QABot
    from voice import get_runtime

    problems = []
    order = []
    previous = os.environ.get("BOT_DB")
    tmp = tempfile.mkdtemp(prefix="qa-defer-")
    os.environ["BOT_DB"] = str(Path(tmp) / "bot.db")
    bot = None
    runtime = get_runtime()
    original_reload = runtime.reload_if_changed

    def reload():
        order.append("reload")
        return original_reload()

    runtime.reload_if_changed = reload
    try:
        bot = QABot()
        original_purge = bot._purge_if_needed

        def purge():
            order.append("purge")
            return original_purge()

        bot._purge_if_needed = purge
        # A deployment with GUILD_IDS set does not watch guild 1. Use one it does watch,
        # or the handler returns before defer and the check only sees "send".
        watched = next((gid for gid in bot.cfg.guild_ids if str(gid).strip()), None)

        class Response:
            async def defer(self, *, thinking=False, ephemeral=False):
                order.append("defer")

            async def send_message(self, *args, **kwargs):
                order.append("send")

        class Followup:
            async def send(self, *args, **kwargs):
                order.append("followup")

        interaction = type("I", (), {})()
        interaction.guild = type("G", (), {"id": watched or 1, "channels": [], "threads": []})()
        interaction.channel = type("C", (), {"id": 10, "name": "general", "guild": interaction.guild})()
        interaction.user = type("U", (), {"id": 1001, "name": "ada", "display_name": "Ada"})()
        interaction.response = Response()
        interaction.followup = Followup()

        async def answer(*args, **kwargs):
            order.append("answer")
            return "ok"

        bot._answer = answer
        order.clear()
        asyncio.run(bot.handle_ask(interaction, "When are GMs?"))
        if not order or order[0] != "defer" or "reload" not in order or "purge" not in order:
            problems.append(f"missing steps: {order}")
        elif order.index("reload") < order.index("defer") or order.index("purge") < order.index("defer"):
            problems.append(f"defer was not first: {order}")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        runtime.reload_if_changed = original_reload
        if bot is not None:
            bot.db.close()
        if previous is None:
            os.environ.pop("BOT_DB", None)
        else:
            os.environ["BOT_DB"] = previous
    return problems


def test_ask_defers_before_reload_and_purge():
    assert ask_defer_problems() == []


def test_ask_defers_when_guild_ids_limits_the_bot(monkeypatch):
    monkeypatch.setenv("GUILD_IDS", "424242")
    assert ask_defer_problems() == []


def test_message_log_snapshot_and_optout_leave_the_event_loop(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "bot.db"))
    from bot import QABot

    calls = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", ""))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    bot = QABot()

    class Author:
        id = 1001
        bot = False
        name = "ethan"
        display_name = "Ethan"
        roles = []

    class Channel:
        id = 10
        name = "general"
        category = None

    class Guild:
        id = 1

        def __init__(self):
            self.members = []
            self.roles = []

    message = type("M", (), {})()
    message.id = 99
    message.guild = Guild()
    message.author = Author()
    message.channel = Channel()
    message.content = "hello from the loop test"
    message.mentions = []
    message.attachments = []
    message.created_at = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)
    message.edited_at = None

    asyncio.run(bot.on_message(message))
    assert "_record_message" in calls
    assert db.execute("SELECT content FROM messages WHERE id='99'").fetchone()[0] == "hello from the loop test"

    calls.clear()

    async def one_snapshot():
        async def stop(_seconds):
            bot._closed = True

        bot.is_closed = lambda: getattr(bot, "_closed", False)
        bot.wait_until_ready = lambda: _ready()
        monkeypatch.setattr(asyncio, "sleep", stop)
        await bot.snapshot_loop()

    async def _ready():
        return None

    asyncio.run(one_snapshot())
    assert "snapshot" in calls

    calls.clear()
    add(db, id="mine", author_id="1001", content="stored before opt-out")

    class Response:
        def __init__(self):
            self.order = []

        async def defer(self, *, ephemeral=False, thinking=False):
            self.order.append("defer")

        async def send_message(self, *args, **kwargs):
            self.order.append("send")

    class Followup:
        def __init__(self, response):
            self.response = response
            self.sent = []

        async def send(self, text, *, ephemeral=False):
            self.response.order.append("followup")
            self.sent.append(text)

    interaction = type("I", (), {})()
    interaction.guild = type("G", (), {"id": 1})()
    interaction.user = type("U", (), {"id": 1001})()
    interaction.response = Response()
    interaction.followup = Followup(interaction.response)
    asyncio.run(bot.handle_optout(interaction))
    assert interaction.response.order[0] == "defer"
    assert "_opt_out_member" in calls
    assert calls.index("_opt_out_member") >= 0
    assert "opted out" in interaction.followup.sent[0].casefold()
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    bot.db.close()
