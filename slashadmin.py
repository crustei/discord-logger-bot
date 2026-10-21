"""In-server /admin commands.

Discord only allows a group to contain subcommands or subgroups, so every
child of /admin is a subgroup:

- /admin blocklist add|remove|list
- /admin approvals list|approve|deny
- /admin optout add|remove|list
- /admin member forget
- /admin bot status|reload|set-status

``default_permissions`` is Manage Server on the top group. Each callback also
checks Manage Server and, when ``admin_roles`` is non-empty, a role name or
role id from that list. Replies are ephemeral. Writes go to the gitignored
local settings file or the SQLite database.
"""
from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands

from admin import (
    AdminError,
    build_approval_answer,
    deny_notice,
    format_list,
    require_pending,
)
from blocklist import BlocklistError, add_channel, format_blocklist, remove_channel, set_local_string
from db import (
    add_opt_out,
    delete_user_data,
    list_approvals,
    list_opt_outs,
    purge_excluded,
    record_admin_action,
    remove_opt_out,
    resolve_approval,
)
from qa import utcnow
from voice import _one_line, get_runtime

log = logging.getLogger("qa_bot")


def member_is_admin(manage_guild: bool, role_ids, role_names, admin_roles) -> bool:
    """Manage Server is required. An empty role list means that permission is enough."""
    if not manage_guild:
        return False
    wanted = [str(item).strip() for item in (admin_roles or []) if str(item).strip()]
    if not wanted:
        return True
    ids = {str(item) for item in role_ids}
    names = {str(item).casefold() for item in role_names if str(item).strip()}
    for item in wanted:
        if item.isdigit() and item in ids:
            return True
        if item.casefold() in names:
            return True
    return False


def actor_is_admin(interaction, admin_roles) -> bool:
    perms = getattr(interaction, "permissions", None)
    manage = bool(getattr(perms, "manage_guild", False))
    user = getattr(interaction, "user", None)
    roles = list(getattr(user, "roles", []) or [])
    role_ids = [str(getattr(role, "id", "")) for role in roles]
    role_names = [str(getattr(role, "name", "")) for role in roles]
    return member_is_admin(manage, role_ids, role_names, admin_roles)


def format_uptime(started_at: dt.datetime, now: dt.datetime) -> str:
    seconds = int((now - started_at).total_seconds())
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def model_label(llm) -> str:
    name = str(getattr(llm, "name", "unknown"))
    model = getattr(llm, "model", None)
    if model:
        return f"{name} ({model})"
    return name


def status_report(
    *,
    started_at: dt.datetime,
    now: dt.datetime,
    message_count: int,
    last_at: str | None,
    last_channel: str | None,
    model: str,
    blocklist_count: int,
    pending_approvals: int,
) -> str:
    if last_at:
        where = last_channel or "unknown channel"
        last = f"{last_at} in {where}"
    else:
        last = "none"
    return "\n".join(
        [
            f"Uptime: {format_uptime(started_at, now)}",
            f"Messages stored: {message_count}",
            f"Last message logged: {last}",
            f"Model: {model}",
            f"Blocklisted channels: {blocklist_count}",
            f"Pending approvals: {pending_approvals}",
        ]
    )


def status_from_db(conn, *, started_at, llm, blocklist_count, now=None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    message_count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    last = conn.execute(
        "SELECT timestamp, channel_name, channel_id FROM messages ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE status='pending'"
    ).fetchone()["n"]
    last_at = None
    last_channel = None
    if last is not None:
        last_at = last["timestamp"]
        last_channel = last["channel_name"] or last["channel_id"]
    return status_report(
        started_at=started_at,
        now=now,
        message_count=int(message_count or 0),
        last_at=last_at,
        last_channel=last_channel,
        model=model_label(llm),
        blocklist_count=int(blocklist_count),
        pending_approvals=int(pending or 0),
    )


def write_status_text(path, text: str) -> str:
    """Store presence text in the local settings file. Returns the clipped value."""
    stored = _one_line(text, "Always listening... | /help", 128)
    set_local_string(path, "status_text", stored)
    return stored


def mutate_blocklist(path, *, action: str, channel_id: str | None, name: str | None, guild_id: str) -> str:
    """Write channel id globally and an optional name for one guild. Local file only."""
    channel_id = str(channel_id).strip() if channel_id else ""
    name = (name or "").strip().lstrip("#")
    if channel_id and not channel_id.isdigit():
        raise BlocklistError("Channel id must be numeric.")
    if not channel_id and not name:
        raise BlocklistError("Pick a channel, type a name, or both.")
    if action == "add":
        edit = add_channel
    elif action == "remove":
        edit = remove_channel
    else:
        raise BlocklistError("Unknown blocklist action.")
    notes: list[str] = []
    problems: list[str] = []
    if channel_id:
        try:
            note, _block = edit(path, channel_id, None)
            notes.append(note)
        except BlocklistError as exc:
            problems.append(str(exc))
    if name:
        try:
            note, _block = edit(path, name, guild_id)
            notes.append(note)
        except BlocklistError as exc:
            problems.append(str(exc))
    if problems and not notes:
        raise BlocklistError(" ".join(problems))
    text = "\n".join(notes)
    if problems:
        text += "\n" + " ".join(problems)
    return text


def audit(bot, interaction, action: str, detail: str) -> None:
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    record_admin_action(
        bot.db,
        user_id=str(getattr(user, "id", "") or "0"),
        username=str(getattr(user, "name", "") or ""),
        guild_id=str(getattr(guild, "id", "") or ""),
        action=action,
        detail=detail,
    )


async def require_admin(bot, interaction, action: str) -> bool:
    runtime = get_runtime()
    runtime.reload_if_changed()
    guild = getattr(interaction, "guild", None)
    if guild is None:
        message = "Use this command in a server."
        reason = "denied: not in a server"
    elif not bot._watching(getattr(guild, "id", None)):
        message = "This bot is not watching this server."
        reason = "denied: server is not watched"
    elif not actor_is_admin(interaction, runtime.admin_roles):
        if runtime.admin_roles:
            message = "You need Manage Server and an admin role to use this command."
        else:
            message = "You need Manage Server to use this command."
        reason = "denied: missing Manage Server or admin role"
    else:
        return True
    audit(bot, interaction, action, reason)
    await interaction.response.send_message(message, ephemeral=True)
    return False


async def send(interaction, text: str, *, view=None) -> None:
    body = (text or "")[:2000]
    is_done = getattr(interaction.response, "is_done", None)
    done = bool(is_done()) if callable(is_done) else False
    if done:
        await interaction.followup.send(body, ephemeral=True)
        return
    kwargs = {"ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    await interaction.response.send_message(body, **kwargs)


async def deliver_channel_message(bot, channel_id: str, content: str) -> str:
    """Post in the original channel. The slash reply stays ephemeral."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        channel = await bot.fetch_channel(int(channel_id))
    message = await channel.send(content[:2000])
    return str(message.id)


class ForgetConfirm(discord.ui.View):
    def __init__(self, bot, actor_id: int, target_id: str, target_label: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.actor_id = int(actor_id)
        self.target_id = str(target_id)
        self.target_label = target_label
        self.done = False

    async def _reject_other(self, interaction) -> bool:
        if int(interaction.user.id) == self.actor_id:
            return False
        await interaction.response.send_message(
            "Only the admin who opened this can confirm it.",
            ephemeral=True,
        )
        audit(self.bot, interaction, "forget", f"denied: different member for user {self.target_id}")
        return True

    @discord.ui.button(label="Delete stored data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_other(interaction):
            return
        if self.done:
            await interaction.response.send_message("This confirmation is already finished.", ephemeral=True)
            return
        if not await require_admin(self.bot, interaction, "forget"):
            return
        self.done = True
        try:
            counts = delete_user_data(self.bot.db, self.target_id)
        except ValueError as exc:
            self.done = False
            await send(interaction, str(exc))
            return
        for child in self.children:
            child.disabled = True
        audit(
            self.bot,
            interaction,
            "forget",
            f"user {self.target_id} messages={counts['messages']} snapshots={counts['snapshots']} "
            f"events={counts['events']} approvals={counts['approvals']}",
        )
        await send(
            interaction,
            f"Deleted stored data for {self.target_label} ({self.target_id}). "
            f"Messages {counts['messages']}, snapshots {counts['snapshots']}, "
            f"events {counts['events']}, approvals {counts['approvals']}. "
            "Discord messages are unchanged. Audit rows stay.",
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_other(interaction):
            return
        if self.done:
            await interaction.response.send_message("This confirmation is already finished.", ephemeral=True)
            return
        self.done = True
        for child in self.children:
            child.disabled = True
        audit(self.bot, interaction, "forget.cancel", f"user {self.target_id}")
        await send(interaction, "Cancelled. Stored data was left in place.")
        self.stop()


async def blocklist_add(bot, interaction, channel, name: str | None):
    if not await require_admin(bot, interaction, "blocklist.add"):
        return
    await _blocklist_edit(bot, interaction, "add", channel, name)


async def blocklist_remove(bot, interaction, channel, name: str | None):
    if not await require_admin(bot, interaction, "blocklist.remove"):
        return
    await _blocklist_edit(bot, interaction, "remove", channel, name)


async def _blocklist_edit(bot, interaction, action: str, channel, name: str | None):
    runtime = get_runtime()
    channel_id = str(channel.id) if channel is not None else None
    try:
        text = mutate_blocklist(
            runtime.local_settings_path,
            action=action,
            channel_id=channel_id,
            name=name,
            guild_id=str(interaction.guild.id),
        )
    except BlocklistError as exc:
        audit(bot, interaction, f"blocklist.{action}", f"failed: {exc}")
        await send(interaction, str(exc))
        return
    runtime.reload()
    if action == "add":
        removed = purge_excluded(bot.db, runtime.blocklist)
        bot.db.commit()
        if removed:
            text += f"\nPurged {removed} logged message(s)."
    bot._block_fp = runtime.blocklist.fingerprint()
    audit(bot, interaction, f"blocklist.{action}", text)
    await send(interaction, text)


async def blocklist_list(bot, interaction):
    if not await require_admin(bot, interaction, "blocklist.list"):
        return
    runtime = get_runtime()
    runtime.reload()
    text = "Effective blocklist:\n" + format_blocklist(runtime.blocklist)
    audit(bot, interaction, "blocklist.list", f"{len(runtime.blocklist)} channel(s)")
    await send(interaction, text)


async def approvals_list(bot, interaction):
    if not await require_admin(bot, interaction, "approvals.list"):
        return
    rows = list_approvals(bot.db, "pending")
    text = format_list(rows)
    audit(bot, interaction, "approvals.list", f"{len(rows)} pending")
    await send(interaction, text)


async def approvals_approve(bot, interaction, request_id: int):
    if not await require_admin(bot, interaction, "approvals.approve"):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        row, text = build_approval_answer(bot.db, request_id, bot.llm)
        message_id = await deliver_channel_message(bot, str(row["channel_id"]), text)
        if not resolve_approval(
            bot.db,
            request_id,
            status="approved",
            answer=text,
            discord_message_id=message_id,
            resolved_by=str(interaction.user.id),
            resolved_at=utcnow(),
        ):
            raise AdminError(f"Request #{request_id} changed before it could be marked approved.")
    except AdminError as exc:
        audit(bot, interaction, "approvals.approve", f"failed #{request_id}: {exc}")
        await send(interaction, str(exc))
        return
    except Exception:
        log.exception("admin approve failed")
        audit(bot, interaction, "approvals.approve", f"failed #{request_id}: could not post")
        await send(interaction, "Could not post that answer. The request is still pending.")
        return
    audit(bot, interaction, "approvals.approve", f"#{request_id} message {message_id}")
    await send(interaction, f"Approved #{request_id}.")


async def approvals_deny(bot, interaction, request_id: int, reason: str | None):
    if not await require_admin(bot, interaction, "approvals.deny"):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        row = require_pending(bot.db, request_id)
        if not resolve_approval(
            bot.db,
            request_id,
            status="denied",
            deny_reason=(reason or "").strip() or None,
            resolved_by=str(interaction.user.id),
            resolved_at=utcnow(),
        ):
            raise AdminError(f"Request #{request_id} changed before it could be marked denied.")
        notice = ""
        try:
            await deliver_channel_message(bot, str(row["channel_id"]), deny_notice())
            notice = " The asker was notified."
        except Exception:
            log.exception("admin deny notify failed")
            notice = " The refusal could not be posted."
    except AdminError as exc:
        audit(bot, interaction, "approvals.deny", f"failed #{request_id}: {exc}")
        await send(interaction, str(exc))
        return
    audit(bot, interaction, "approvals.deny", f"#{request_id} {(reason or '').strip()}")
    await send(interaction, f"Denied #{request_id}.{notice}")


async def optout_add(bot, interaction, member: discord.User):
    if not await require_admin(bot, interaction, "optout.add"):
        return
    try:
        counts = add_opt_out(bot.db, str(member.id))
    except ValueError as exc:
        audit(bot, interaction, "optout.add", f"failed: {exc}")
        await send(interaction, str(exc))
        return
    audit(bot, interaction, "optout.add", f"user {member.id}")
    await send(
        interaction,
        f"Opted out {member.id}. Messages {counts['messages']}, snapshots {counts['snapshots']}, "
        f"events {counts['events']}, approvals {counts['approvals']}.",
    )


async def optout_remove(bot, interaction, member: discord.User):
    if not await require_admin(bot, interaction, "optout.remove"):
        return
    try:
        removed = remove_opt_out(bot.db, str(member.id))
    except ValueError as exc:
        audit(bot, interaction, "optout.remove", f"failed: {exc}")
        await send(interaction, str(exc))
        return
    if not removed:
        audit(bot, interaction, "optout.remove", f"failed: {member.id} is not opted out")
        await send(interaction, f"{member.id} is not on the opt-out list.")
        return
    audit(bot, interaction, "optout.remove", f"user {member.id}")
    await send(interaction, f"Removed {member.id} from the opt-out list. New messages can be stored again.")


async def optout_list(bot, interaction):
    if not await require_admin(bot, interaction, "optout.list"):
        return
    rows = list_opt_outs(bot.db)
    if not rows:
        text = "No one has opted out."
    else:
        lines = ["Opt-out list:"]
        lines.extend(f"{user_id}  {created}" for user_id, created in rows)
        text = "\n".join(lines)
    audit(bot, interaction, "optout.list", f"{len(rows)} member(s)")
    await send(interaction, text)


async def member_forget(bot, interaction, member: discord.User):
    if not await require_admin(bot, interaction, "forget"):
        return
    label = str(getattr(member, "name", "") or member.id)
    view = ForgetConfirm(bot, interaction.user.id, str(member.id), label)
    audit(bot, interaction, "forget.prompt", f"user {member.id}")
    await send(
        interaction,
        f"Delete stored data for {label} ({member.id})? "
        "This removes their messages, edits, snapshots, events, and approval rows from this bot's database. "
        "It does not delete their Discord messages. Press the button to confirm. It expires in 60 seconds.",
        view=view,
    )


async def bot_status(bot, interaction):
    if not await require_admin(bot, interaction, "status"):
        return
    runtime = get_runtime()
    runtime.reload_if_changed()
    text = status_from_db(
        bot.db,
        started_at=bot.started_at,
        llm=bot.llm,
        blocklist_count=len(runtime.blocklist),
    )
    audit(bot, interaction, "status", "ok")
    await send(interaction, text)


async def bot_reload(bot, interaction):
    await bot.handle_reload(interaction)


async def bot_set_status(bot, interaction, text: str):
    if not await require_admin(bot, interaction, "set-status"):
        return
    runtime = get_runtime()
    stored = write_status_text(runtime.local_settings_path, text)
    runtime.reload()
    await bot._apply_presence(runtime)
    audit(bot, interaction, "set-status", stored)
    await send(interaction, f"Status set to: {stored}")


def register_admin_commands(bot) -> None:
    admin = app_commands.Group(
        name="admin",
        description="Officer tools for the blocklist, approvals, and settings",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    class BlocklistGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="blocklist", description="Channels that are never logged or quoted")

        @app_commands.command(name="add", description="Block a channel and delete rows already stored")
        @app_commands.describe(channel="Channel to block by id", name="Channel name for this server")
        async def add(
            self,
            interaction: discord.Interaction,
            channel: discord.app_commands.AppCommandChannel | None = None,
            name: str | None = None,
        ):
            await blocklist_add(bot, interaction, channel, name)

        @app_commands.command(name="remove", description="Stop excluding a channel")
        @app_commands.describe(channel="Channel to unblock by id", name="Channel name for this server")
        async def remove(
            self,
            interaction: discord.Interaction,
            channel: discord.app_commands.AppCommandChannel | None = None,
            name: str | None = None,
        ):
            await blocklist_remove(bot, interaction, channel, name)

        @app_commands.command(name="list", description="Show the effective blocklist")
        async def list_channels(self, interaction: discord.Interaction):
            await blocklist_list(bot, interaction)

    class ApprovalsGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="approvals", description="Held questions waiting for an officer")

        @app_commands.command(name="list", description="List pending approval requests")
        async def list_pending(self, interaction: discord.Interaction):
            await approvals_list(bot, interaction)

        @app_commands.command(name="approve", description="Approve a request and post the answer")
        @app_commands.describe(request_id="Approval id")
        async def approve(self, interaction: discord.Interaction, request_id: int):
            await approvals_approve(bot, interaction, request_id)

        @app_commands.command(name="deny", description="Deny a request and post the refusal")
        @app_commands.describe(request_id="Approval id", reason="Stored on the request and not posted")
        async def deny(self, interaction: discord.Interaction, request_id: int, reason: str | None = None):
            await approvals_deny(bot, interaction, request_id, reason)

    class OptoutGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="optout", description="Stop or resume logging a member")

        @app_commands.command(name="add", description="Opt a member out and delete rows already stored")
        @app_commands.describe(member="Member to opt out")
        async def add(self, interaction: discord.Interaction, member: discord.User):
            await optout_add(bot, interaction, member)

        @app_commands.command(name="remove", description="Allow a member to be logged again")
        @app_commands.describe(member="Member to opt back in")
        async def remove(self, interaction: discord.Interaction, member: discord.User):
            await optout_remove(bot, interaction, member)

        @app_commands.command(name="list", description="List members who opted out")
        async def list_members(self, interaction: discord.Interaction):
            await optout_list(bot, interaction)

    class MemberGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="member", description="Member data stored by this bot")

        @app_commands.command(name="forget", description="Delete one member's stored data after confirmation")
        @app_commands.describe(member="Member whose stored rows should be deleted")
        async def forget(self, interaction: discord.Interaction, member: discord.User):
            await member_forget(bot, interaction, member)

    class BotGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="bot", description="Presence, status, and settings")

        @app_commands.command(name="status", description="Show uptime, the log, and the model")
        async def status(self, interaction: discord.Interaction):
            await bot_status(bot, interaction)

        @app_commands.command(name="reload", description="Re-read persona and settings files")
        async def reload_settings(self, interaction: discord.Interaction):
            await bot_reload(bot, interaction)

        @app_commands.command(name="set-status", description="Change the custom status text")
        @app_commands.describe(text="Status text, up to 128 characters")
        async def set_status(self, interaction: discord.Interaction, text: str):
            await bot_set_status(bot, interaction, text)

    admin.add_command(BlocklistGroup())
    admin.add_command(ApprovalsGroup())
    admin.add_command(OptoutGroup())
    admin.add_command(MemberGroup())
    admin.add_command(BotGroup())
    bot.tree.add_command(admin)
