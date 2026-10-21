"""Discord logger and Q&A assistant.

Logging is passive: messages are stored and the bot does not talk. It posts only
when someone uses /ask, /help, /optout, mentions it, or an officer uses /admin.
Channels on the blocklist in config/settings.toml are not stored.
``allowed_channels`` limits where questions are answered. An empty blocklist
excludes nothing.

``GUILD_IDS`` (optional) limits logging and slash commands to those servers.
Leave it unset to watch every server the bot is invited to. Guild-scoped sync
clears leftover global commands, and global sync clears leftover guild commands,
so an earlier scope does not leave a duplicate set in the picker.

Invite bits: View Channels, Send Messages, Read Message History, Use Application
Commands. The integer is permissions.INVITE_PERMISSIONS (2147552256).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sys
import threading

import discord
from discord import app_commands

from commandsync import SyncPlan, parse_id_list, sync_plan, unique_ids
from config import load_config, load_dotenv, watching_guild
from db import (
    add_opt_out,
    connect,
    delete_message_everywhere,
    get_meta,
    mark_deleted,
    purge_excluded,
    record_edit,
    record_member_event,
    record_member_snapshot,
    set_meta,
    upsert_message,
)
from llm import provider_from_env
from asklog import record_in_new_connection
from permissions import (
    channel_guild_id,
    guild_channel_viewers,
    here_keys,
    invocation_allowed,
    is_excluded,
    role_tokens,
    snowflake_id,
    visible_channel_ids,
)
from qa import answer_in_new_connection
from slashadmin import actor_is_admin, audit, register_admin_commands, require_admin
from voice import get_runtime

log = logging.getLogger("qa_bot")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def roles_of(member) -> list[str]:
    return [role.name for role in getattr(member, "roles", []) if role.name != "@everyone"]


def msg_row(message: discord.Message) -> dict:
    channel = message.channel
    category = getattr(channel, "category", None)
    return dict(
        id=str(message.id),
        guild_id=str(message.guild.id) if message.guild else None,
        channel_id=str(channel.id),
        channel_name=getattr(channel, "name", str(channel)),
        category=getattr(category, "name", None),
        author_id=str(message.author.id),
        author_name=message.author.name,
        display_name=message.author.display_name,
        roles=roles_of(message.author),
        timestamp=message.created_at.isoformat(),
        edited_timestamp=message.edited_at.isoformat() if message.edited_at else None,
        content=message.content,
        attachments=[attachment.url for attachment in message.attachments],
        source="live",
    )


def presence_activity(status_text: str) -> discord.CustomActivity:
    """Custom status so the settings text is shown as written.

    A Listening activity would prefix the same sentence with "Listening to".
    """
    text = " ".join((status_text or "").split()) or "Always listening... | /help"
    if len(text) > 128:
        text = text[:128]
    return discord.CustomActivity(name=text)


def help_text(runtime, *, admin: bool = False) -> str:
    """Ephemeral /help body. Terms and privacy links appear only when configured."""
    lines = [
        f"{runtime.display_title} answers questions for {runtime.org_name}.",
        "",
        "How to ask",
        "Use /ask and type your question. Or @mention the bot and put the question in the same message.",
        "The bot only replies when you do one of those. It does not post on its own.",
        "",
        "What it can see",
        "It searches the knowledge files and messages in channels you can view, and only notes at or below your tier. Channels on the blocklist are not logged and are not used.",
        "Messages from members who opted out are not stored and are not used.",
        "If an allowlist is set, it only answers in those channels. An empty allowlist means every channel that is not blocklisted.",
        "Answers are short paraphrases. A line at the end can name the doc title or channel and the date.",
        "",
        "Approval",
        "Dates, events, how-tos, and team questions are answered directly from public knowledge files and from channels you can see.",
        "A question about a specific person's private information waits for an officer. The waiting note does not include those excerpts.",
        "",
        "Opt out",
    ]
    if runtime.optout_command:
        lines.append(
            "/optout stops logging your messages, edits, member snapshots, and events, and deletes copies already stored. "
            "Only you see the confirmation. You can still use /ask. An operator has to opt you back in."
        )
    else:
        lines.append("The /optout command is turned off. Ask an operator to opt you out. You can still use /ask.")
    links = []
    if runtime.terms_url:
        links.append(f"Terms: {runtime.terms_url}")
    if runtime.privacy_url:
        links.append(f"Privacy: {runtime.privacy_url}")
    if links:
        lines.extend(["", "Terms and privacy", *links])
    if admin:
        lines.extend(
            [
                "",
                "Admin commands",
                "Only you can see this section. Discord also limits /admin to members with Manage Server.",
                "/admin blocklist add, remove, and list take a channel and an optional name. Add deletes stored rows for that channel.",
                "/admin approvals list, approve, and deny use the same queue as admin.py.",
                "/admin optout add, remove, and list take a member.",
                "/admin member forget deletes one member's stored data after you confirm.",
                "/admin bot status shows uptime, messages, the model, the blocklist, and pending approvals.",
                "/admin bot reload re-reads the persona and settings. /admin bot set-status changes the presence text.",
                "/admin tier set-channel, set-role, and show map channels and roles onto the tier list.",
                "When admin_roles lists role names or role ids, you also need one of those roles.",
            ]
        )
    return "\n".join(lines)


def question_from_mention(message: discord.Message, bot_id: int) -> str:
    import re

    return re.sub(rf"<@!?{bot_id}>", "", message.content).strip()


class QABot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.cfg = load_config()
        self.started_at = dt.datetime.now(dt.timezone.utc)
        self.tree = app_commands.CommandTree(self)
        self.db = connect(self.cfg.db_path)
        self._block_fp = None
        self._pending_command_sync = None
        self._pending_previous_scope = None
        self._pending_previous_guilds = ()
        self._db_lock = threading.Lock()
        self._purge_if_needed()
        self.llm = provider_from_env()
        self._register_commands()

    def _watching(self, guild_id) -> bool:
        return watching_guild(guild_id, self.cfg.guild_ids)

    def _purge_if_needed(self) -> None:
        with self._db_lock:
            runtime = get_runtime()
            fingerprint = runtime.blocklist.fingerprint()
            if fingerprint == self._block_fp:
                return
            conn = connect(self.cfg.db_path)
            try:
                removed = purge_excluded(conn, runtime.blocklist)
                conn.commit()
            finally:
                conn.close()
            self._block_fp = fingerprint
            if removed:
                log.info("purged %s message(s) from blocklisted channels", removed)

    def _record_message(self, row: dict | None) -> None:
        """Purge, then store one message. Runs off the event loop."""
        self._purge_if_needed()
        if not row:
            return
        conn = connect(self.cfg.db_path)
        try:
            upsert_message(conn, row)
            conn.commit()
        finally:
            conn.close()

    def _opt_out_member(self, user_id, enabled: bool) -> str:
        conn = connect(self.cfg.db_path)
        try:
            return optout_reply(conn, user_id, enabled=enabled)
        finally:
            conn.close()

    def _register_commands(self) -> None:
        runtime = get_runtime()

        @self.tree.command(name="ask", description=runtime.ask_description())
        @app_commands.describe(question="Question to answer from messages and notes you can see")
        async def ask(interaction: discord.Interaction, question: str):
            await self.handle_ask(interaction, question)

        @self.tree.command(
            name="optout",
            description="Stop storing your messages and delete copies already stored",
        )
        async def optout(interaction: discord.Interaction):
            await self.handle_optout(interaction)

        @self.tree.command(name="help", description="How to ask, what is stored, and how to opt out")
        async def help_command(interaction: discord.Interaction):
            await self.handle_help(interaction)

        register_admin_commands(self)

    async def setup_hook(self) -> None:
        asyncio.create_task(self.snapshot_loop())
        for gid in self.cfg.guild_ids:
            if not str(gid).strip().isdigit():
                log.warning("GUILD_IDS entry %r is not a numeric snowflake; ignoring it", gid)
        if self.cfg.guild_ids and not unique_ids(self.cfg.guild_ids):
            log.warning("No numeric guild ids in GUILD_IDS; syncing slash commands globally")
        await self._sync_slash_commands(cached_guild_ids=())

    def _cached_guild_ids(self) -> list[int]:
        return [int(guild.id) for guild in self.guilds]

    def _store_sync_state(self, plan: SyncPlan) -> None:
        set_meta(self.db, "command_scope", plan.next_scope)
        set_meta(self.db, "command_guilds", ",".join(str(gid) for gid in plan.next_guilds))
        self.db.commit()

    async def _apply_command_sync(self, plan: SyncPlan) -> None:
        for gid in plan.sync_guilds:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %s slash command(s) to guild %s", len(synced), gid)
        if plan.publish_global:
            synced = await self.tree.sync()
            log.info("synced %s global slash command(s)", len(synced))
        if plan.clear_global:
            self.tree.clear_commands(guild=None)
            synced = await self.tree.sync()
            log.info("cleared global slash commands (%s now registered globally)", len(synced))
        for gid in plan.clear_guilds:
            guild = discord.Object(id=gid)
            self.tree.clear_commands(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("cleared guild slash commands for %s (%s left)", gid, len(synced))

    async def _sync_slash_commands(self, cached_guild_ids) -> None:
        scope = get_meta(self.db, "command_scope")
        previous = parse_id_list(get_meta(self.db, "command_guilds"))
        plan = sync_plan(
            numeric_guild_ids=self.cfg.guild_ids,
            previous_scope=scope,
            previous_guilds=previous,
            cached_guild_ids=cached_guild_ids,
        )
        log.info(
            "command sync mode=%s publish_global=%s clear_global=%s sync_guilds=%s clear_guilds=%s",
            plan.mode,
            plan.publish_global,
            plan.clear_global,
            ",".join(str(gid) for gid in plan.sync_guilds),
            ",".join(str(gid) for gid in plan.clear_guilds),
        )
        try:
            await self._apply_command_sync(plan)
        except Exception:
            log.exception("slash command sync failed")
            return
        if plan.needs_cache and not cached_guild_ids:
            self._pending_command_sync = plan
            self._pending_previous_scope = scope if scope in {"guild", "global"} else None
            self._pending_previous_guilds = previous
            return
        self._store_sync_state(plan)
        self._pending_command_sync = None

    async def _finish_command_sync(self) -> None:
        pending = self._pending_command_sync
        if pending is None:
            return
        fresh = sync_plan(
            numeric_guild_ids=self.cfg.guild_ids,
            previous_scope=self._pending_previous_scope,
            previous_guilds=self._pending_previous_guilds,
            cached_guild_ids=self._cached_guild_ids(),
        )
        done = set(pending.clear_guilds)
        keep = set(pending.sync_guilds)
        extra = tuple(gid for gid in fresh.clear_guilds if gid not in done and gid not in keep)
        follow = SyncPlan(
            mode=pending.mode,
            sync_guilds=(),
            publish_global=False,
            clear_global=False,
            clear_guilds=extra,
            next_scope=pending.next_scope,
            next_guilds=pending.next_guilds,
            needs_cache=False,
        )
        try:
            await self._apply_command_sync(follow)
        except Exception:
            log.exception("slash command sync follow-up failed")
            return
        self._store_sync_state(pending)
        self._pending_command_sync = None

    async def _apply_presence(self, runtime) -> None:
        try:
            await self.change_presence(activity=presence_activity(runtime.status_text))
        except Exception:
            log.exception("could not set presence")

    async def on_ready(self) -> None:
        await self._finish_command_sync()
        runtime = get_runtime()
        await self._apply_presence(runtime)
        watched = "every guild" if not self.cfg.guild_ids else ",".join(self.cfg.guild_ids)
        log.info(
            "Logged in as %s (%s) in %s guild(s). Watching %s. Voice cap %s chars, provider %s.",
            self.user,
            runtime.display_title,
            len(self.guilds),
            watched,
            runtime.max_answer_chars,
            self.llm.name,
        )

    async def snapshot_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.snapshot)
            except Exception:
                log.exception("snapshot failed")
            await asyncio.sleep(self.cfg.snapshot_hours * 3600)

    def snapshot(self) -> None:
        stamp = now()
        member_rows = []
        role_rows = []
        for guild in list(self.guilds):
            if not self._watching(guild.id):
                continue
            for member in list(guild.members):
                member_rows.append(
                    (
                        stamp,
                        str(member.id),
                        member.name,
                        member.display_name,
                        json.dumps(roles_of(member)),
                        member.joined_at.isoformat() if member.joined_at else None,
                        int(member.bot),
                    )
                )
            for role in list(guild.roles):
                role_rows.append((stamp, str(role.id), role.name, role.position, len(role.members)))
        conn = connect(self.cfg.db_path)
        try:
            for row in member_rows:
                record_member_snapshot(conn, *row)
            if role_rows:
                conn.executemany("INSERT INTO role_snapshots VALUES(?,?,?,?,?)", role_rows)
            conn.commit()
        finally:
            conn.close()
        log.info("snapshot written")

    def _excluded_channel(self, channel, guild=None) -> bool:
        runtime = get_runtime()
        guild_id = channel_guild_id(channel, snowflake_id(guild))
        return is_excluded(getattr(channel, "id", None), getattr(channel, "name", None), runtime.blocklist, guild_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None and not self._watching(message.guild.id):
            return
        if self.user and message.author.id == self.user.id:
            return
        row = None
        if message.guild and not self._excluded_channel(message.channel, message.guild):
            row = msg_row(message)
        await asyncio.to_thread(self._record_message, row)
        if message.author.bot or self.user is None or self.user not in message.mentions:
            return
        await self.answer_mention(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is not None and not self._watching(payload.guild_id):
            return
        data = payload.data
        if "content" not in data:
            return
        await asyncio.to_thread(self._purge_if_needed)
        cached = payload.cached_message
        if cached is not None and self._excluded_channel(cached.channel, cached.guild):
            delete_message_everywhere(self.db, str(payload.message_id))
            self.db.commit()
            return
        cached_row = msg_row(cached) if cached else None
        record_edit(self.db, str(payload.message_id), data["content"], data.get("edited_timestamp") or now(), cached_row)
        self.db.commit()

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None and not self._watching(payload.guild_id):
            return
        mark_deleted(self.db, str(payload.message_id))
        self.db.commit()

    def event(self, user, ev: str, detail: str = "") -> None:
        if record_member_event(self.db, str(user.id), user.name, ev, detail, now()):
            self.db.commit()

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild is None or not self._watching(member.guild.id):
            return
        self.event(member, "join", json.dumps(roles_of(member)))

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None or not self._watching(member.guild.id):
            return
        self.event(member, "leave", json.dumps(roles_of(member)))

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild is None or not self._watching(after.guild.id):
            return
        added = set(roles_of(after)) - set(roles_of(before))
        removed = set(roles_of(before)) - set(roles_of(after))
        if added or removed:
            self.event(after, "roles", json.dumps({"added": sorted(added), "removed": sorted(removed)}))
        if before.display_name != after.display_name:
            self.event(after, "nick", json.dumps({"from": before.display_name, "to": after.display_name}))

    async def handle_reload(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self, interaction, "reload"):
            return
        runtime = get_runtime()
        runtime.reload()
        await asyncio.to_thread(self._purge_if_needed)
        await self._apply_presence(runtime)
        log.info("voice reloaded: %s", runtime.summary())
        audit(self, interaction, "reload", runtime.summary())
        await interaction.response.send_message(runtime.summary(), ephemeral=True)

    async def handle_help(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if not self._watching(interaction.guild.id):
            await interaction.response.send_message("This bot is not watching this server.", ephemeral=True)
            return
        runtime = get_runtime()
        runtime.reload_if_changed()
        admin = actor_is_admin(interaction, runtime.admin_roles)
        await interaction.response.send_message(help_text(runtime, admin=admin)[:2000], ephemeral=True)

    async def handle_optout(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if not self._watching(interaction.guild.id):
            await interaction.response.send_message("This bot is not watching this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        runtime = get_runtime()
        text = await asyncio.to_thread(self._opt_out_member, interaction.user.id, runtime.optout_command)
        await interaction.followup.send(text[:2000], ephemeral=True)

    async def handle_ask(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild is not None and not self._watching(interaction.guild.id):
            await interaction.response.send_message("This bot is not watching this server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        runtime = get_runtime()
        await asyncio.to_thread(self._purge_if_needed)
        if self._excluded_channel(interaction.channel, interaction.guild):
            await interaction.followup.send(runtime.say("excluded_channel"))
            return
        if not invocation_allowed(
            interaction.channel,
            runtime.allowed_channels,
            runtime.blocklist,
            channel_guild_id(interaction.channel, snowflake_id(interaction.guild)),
        ):
            await interaction.followup.send(
                "I only answer in the channels listed under allowed_channels in config/settings.toml.",
            )
            return
        question = (question or "").strip()
        if not question:
            await interaction.followup.send(runtime.say("empty_question"))
            return
        text = await self._answer(question, interaction.guild, interaction.user, interaction.channel)
        await interaction.followup.send(text[:2000])

    async def answer_mention(self, message: discord.Message) -> None:
        runtime = get_runtime()
        runtime.reload_if_changed()
        await asyncio.to_thread(self._purge_if_needed)
        if self._excluded_channel(message.channel, message.guild):
            return
        if not invocation_allowed(
            message.channel,
            runtime.allowed_channels,
            runtime.blocklist,
            channel_guild_id(message.channel, snowflake_id(message.guild)),
        ):
            log.info("ignoring mention outside allowed_channels in %s", getattr(message.channel, "id", None))
            return
        question = question_from_mention(message, self.user.id)
        if not question:
            await message.reply(runtime.say("empty_question"), mention_author=False)
            return
        async with message.channel.typing():
            text = await self._answer(question, message.guild, message.author, message.channel)
        await message.reply(text[:2000], mention_author=False)

    async def _answer(self, question: str, guild, user, channel) -> str:
        runtime = get_runtime()
        visible = visible_channel_ids(guild, user) | here_keys(channel) | set(runtime.public_channels)
        here = here_keys(channel)
        try:
            result = await asyncio.to_thread(
                answer_in_new_connection,
                self.cfg.db_path,
                question=question,
                asker_id=str(user.id),
                asker_name=getattr(user, "display_name", None) or getattr(user, "name", ""),
                guild_id=str(guild.id) if guild else None,
                channel_id=str(getattr(channel, "id", "")),
                channel_name=getattr(channel, "name", None),
                visible=visible,
                public_channels=set(runtime.public_channels),
                here=here,
                excluded=runtime.blocklist,
                docs_dir=self.cfg.docs_dir,
                llm=self.llm,
                asker_roles=role_tokens(user),
                channel_viewers=guild_channel_viewers(guild) if guild is not None else None,
            )
        except Exception:
            log.exception("ask failed")
            return "Something went wrong while looking that up. Try again in a minute."
        try:
            await asyncio.to_thread(
                record_in_new_connection, self.cfg.db_path, question, result, asker_id=str(user.id)
            )
        except Exception:
            log.exception("ask log write failed")
        log.info("ask kind=%s channel=%s", result.kind, getattr(channel, "id", None))
        return result.text or runtime.say("refusal")


def optout_reply(conn, user_id, *, enabled: bool) -> str:
    """Ephemeral text for /optout. Adds the member and purges stored rows when enabled."""
    if not enabled:
        return "Opting out from Discord is turned off. Ask an operator to opt you out."
    add_opt_out(conn, str(user_id))
    return (
        "You're opted out. This bot will not store your messages, edits, member snapshots, or events, "
        "and it deleted copies it already had. You can still use /ask. Your older messages will not be used in answers. "
        "To opt back in, ask an operator."
    )


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set")
    QABot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
