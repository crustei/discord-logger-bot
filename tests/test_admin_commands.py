"""Slash-command scope cleanup and /admin checks."""

import asyncio

import discord

from commandsync import sync_plan
from db import record_admin_action
from slashadmin import member_is_admin, mutate_blocklist
from tests.conftest import add, committed_runtime
from voice import set_runtime


def test_sync_plan_clears_the_other_scope():
    guild = sync_plan(
        numeric_guild_ids=["42", "nope"],
        previous_scope="global",
        previous_guilds=[],
        cached_guild_ids=[],
    )
    assert guild.mode == "guild"
    assert guild.sync_guilds == (42,)
    assert guild.clear_global is True
    assert guild.publish_global is False

    leftover = sync_plan(
        numeric_guild_ids=["7"],
        previous_scope="guild",
        previous_guilds=[42, 7],
        cached_guild_ids=[],
    )
    assert leftover.clear_guilds == (42,)
    assert leftover.clear_global is True

    first_global = sync_plan(
        numeric_guild_ids=[],
        previous_scope=None,
        previous_guilds=[],
        cached_guild_ids=[],
    )
    assert first_global.mode == "global"
    assert first_global.publish_global is True
    assert first_global.needs_cache is True

    with_cache = sync_plan(
        numeric_guild_ids=[],
        previous_scope=None,
        previous_guilds=[],
        cached_guild_ids=[9],
    )
    assert with_cache.clear_guilds == (9,)
    assert with_cache.needs_cache is False

    again = sync_plan(
        numeric_guild_ids=[],
        previous_scope="global",
        previous_guilds=[],
        cached_guild_ids=[9],
    )
    assert again.clear_guilds == ()
    assert again.needs_cache is False


def test_member_is_admin_requires_manage_server_and_optional_role():
    assert member_is_admin(True, ["5"], ["Officers"], []) is True
    assert member_is_admin(False, ["5"], ["Officers"], []) is False
    assert member_is_admin(True, ["5"], ["Officers"], ["officers"]) is True
    assert member_is_admin(True, ["5"], ["Members"], ["9"]) is False
    assert member_is_admin(True, ["9"], ["Members"], ["9"]) is True
    assert member_is_admin(True, ["9"], ["Members"], ["Officers"]) is False


def test_admin_group_replaces_top_level_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "bot.db"))
    set_runtime(committed_runtime(tmp_path))
    from bot import QABot, help_text

    bot = QABot()
    names = [command.name for command in bot.tree.get_commands()]
    assert "reload" not in names
    assert "admin" in names
    admin = bot.tree.get_command("admin")
    assert admin.default_permissions.manage_guild
    assert admin.guild_only is True
    children = {command.name: command for command in admin.commands}
    assert set(children) == {"blocklist", "approvals", "optout", "member", "bot"}
    assert all(isinstance(command, discord.app_commands.Group) for command in admin.commands)
    payload = admin.to_dict(bot.tree)
    assert payload["default_member_permissions"] == discord.Permissions(manage_guild=True).value
    assert payload["dm_permission"] is False
    assert {option["type"] for option in payload["options"]} == {2}
    from voice import get_runtime

    runtime = get_runtime()
    assert "/admin" not in help_text(runtime, admin=False)
    assert "/admin blocklist" in help_text(runtime, admin=True)
    bot.db.close()


def test_guild_sync_clears_globals_and_global_sync_clears_cached_guilds(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "bot.db"))
    monkeypatch.setenv("GUILD_IDS", "42")
    set_runtime(committed_runtime(tmp_path))
    from bot import QABot
    from db import get_meta

    bot = QABot()
    calls = []

    async def sync(guild=None):
        calls.append(("sync", None if guild is None else guild.id))
        return []

    def copy(*, guild):
        calls.append(("copy", guild.id))

    def clear(*, guild, type=None):
        calls.append(("clear", None if guild is None else guild.id))

    bot.tree.sync = sync
    bot.tree.copy_global_to = copy
    bot.tree.clear_commands = clear
    bot._cached_guild_ids = lambda: [42, 9]

    async def run():
        await bot.setup_hook()
        await bot._finish_command_sync()
        current = asyncio.current_task()
        for task in list(asyncio.all_tasks()):
            if task is not current:
                task.cancel()

    asyncio.run(run())
    assert ("copy", 42) in calls
    assert ("sync", 42) in calls
    assert ("clear", None) in calls
    assert calls.index(("copy", 42)) < calls.index(("clear", None))
    assert get_meta(bot.db, "command_scope") == "guild"

    calls.clear()
    bot.cfg = type(bot.cfg)(
        db_path=bot.cfg.db_path,
        docs_dir=bot.cfg.docs_dir,
        snapshot_hours=bot.cfg.snapshot_hours,
        guild_ids=(),
    )

    async def run_global():
        await bot.setup_hook()
        await bot._finish_command_sync()
        current = asyncio.current_task()
        for task in list(asyncio.all_tasks()):
            if task is not current:
                task.cancel()

    asyncio.run(run_global())
    assert ("sync", None) in calls
    assert ("clear", 42) in calls
    assert get_meta(bot.db, "command_scope") == "global"
    bot.db.close()


def test_blocklist_add_writes_the_local_file_and_purges(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(db.execute("PRAGMA database_list").fetchone()[2]))
    runtime = committed_runtime(tmp_path)
    before = runtime.settings_path.read_text(encoding="utf-8")
    add(db, id="secret", channel_id="50", channel_name="private-channel", content="hidden note", guild_id="1")
    from bot import QABot

    bot = QABot()
    bot.db.close()
    bot.db = db
    bot._block_fp = runtime.blocklist.fingerprint()

    class Response:
        def __init__(self):
            self.sent = []

        def is_done(self):
            return bool(self.sent)

        async def send_message(self, text, *, ephemeral=False, view=None):
            self.sent.append((text, ephemeral, view))

    class Interaction:
        def __init__(self):
            self.guild = type("G", (), {"id": 1})()
            self.user = type("U", (), {"id": 9, "name": "ada", "roles": []})()
            self.permissions = type("P", (), {"manage_guild": True})()
            self.response = Response()
            self.channel = type("C", (), {"id": 10, "name": "general"})()

    interaction = Interaction()
    channel = type("Chan", (), {"id": 50, "name": "private-channel"})()

    async def run():
        from slashadmin import blocklist_add

        await blocklist_add(bot, interaction, channel, "private-channel")

    asyncio.run(run())
    text, ephemeral, _view = interaction.response.sent[0]
    assert ephemeral is True
    assert "50" in text
    assert runtime.settings_path.read_text(encoding="utf-8") == before
    local = runtime.local_settings_path.read_text(encoding="utf-8")
    assert "50" in local
    assert "private-channel" in local
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='secret'").fetchone()[0] == 0
    row = db.execute("SELECT action, user_id FROM admin_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "blocklist.add"
    assert row["user_id"] == "9"
    assert "blocklist.add" in (tmp_path / "bot.log").read_text(encoding="utf-8")


def test_set_status_and_forget_confirm(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "unused.db"))
    runtime = committed_runtime(tmp_path)
    before = runtime.settings_path.read_text(encoding="utf-8")
    add(db, id="mine", author_id="1001", content="hello from general")
    from bot import QABot
    from slashadmin import ForgetConfirm, bot_set_status

    bot = QABot()
    bot.db.close()
    bot.db = db
    seen = {}

    async def change_presence(*, activity=None, status=None):
        seen["state"] = activity.to_dict()["state"]

    bot.change_presence = change_presence

    class Response:
        def __init__(self):
            self.sent = []

        def is_done(self):
            return bool(self.sent)

        async def send_message(self, text, *, ephemeral=False, view=None):
            self.sent.append({"text": text, "ephemeral": ephemeral, "view": view})

    def interaction(user_id=9, manage=True):
        item = type("I", (), {})()
        item.guild = type("G", (), {"id": 1})()
        item.user = type("U", (), {"id": user_id, "name": "ada", "roles": []})()
        item.permissions = type("P", (), {"manage_guild": manage})()
        item.response = Response()
        item.channel = type("C", (), {"id": 10, "name": "general"})()
        return item

    status_interaction = interaction()

    async def run_status():
        await bot_set_status(bot, status_interaction, "Office hours")

    asyncio.run(run_status())
    assert seen["state"] == "Office hours"
    assert 'status_text = "Office hours"' in runtime.local_settings_path.read_text(encoding="utf-8")
    assert runtime.settings_path.read_text(encoding="utf-8") == before

    prompt = interaction()

    async def run_prompt():
        from slashadmin import member_forget

        member = type("M", (), {"id": 1001, "name": "ethan"})()
        await member_forget(bot, prompt, member)

    asyncio.run(run_prompt())
    view = prompt.response.sent[0]["view"]
    assert isinstance(view, ForgetConfirm)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 1

    stranger = interaction(user_id=8)
    confirm = next(child for child in view.children if child.label == "Delete stored data")

    async def run_stranger():
        await confirm.callback(stranger)

    asyncio.run(run_stranger())
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 1

    owner = interaction()

    async def run_owner():
        await confirm.callback(owner)

    asyncio.run(run_owner())
    assert db.execute("SELECT COUNT(*) FROM messages WHERE author_id='1001'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0] >= 1
    record_admin_action(db, user_id="1001", username="ethan", guild_id="1", action="status", detail="kept")
    assert db.execute("SELECT COUNT(*) FROM admin_audit WHERE user_id='1001'").fetchone()[0] == 1


def test_mutate_blocklist_needs_a_channel_or_a_name(tmp_path):
    path = tmp_path / "settings.local.toml"
    try:
        mutate_blocklist(path, action="add", channel_id=None, name=None, guild_id="1")
    except Exception as exc:
        assert "Pick a channel" in str(exc)
    else:
        raise AssertionError("expected a blocklist error")
