"""Officer-only answer verification (draft v2).

* When an officer uses /ask (or @mentions the bot), the public answer is posted
  unchanged and a SEPARATE ephemeral follow-up (only the asker sees it) offers
  ✅ Correct / ➖ Partly / ❌ Wrong. ➖ and ❌ open an optional correction modal.
  After voting, that ephemeral prompt is edited to "Thank you for your
  verification." with no buttons.
* Any officer can rate any bot answer on the fly with the message context menu
  Apps > "Verify answer" (no pings). Non-officers are turned away ephemerally.
* Votes live in ask_feedback (one changeable vote per officer per answer,
  voter stored as a salted hash, opted-out officers not stored), linked to
  ask_log.id. ask_message maps the public answer message to its ask_log row.
* When an answer reaches the threshold (settings verify_min_yes, default 2 ✅,
  and verify_max_no, default 0 ❌) the bot edits the public answer to drop the
  sources and show "✅ Verified by officers". That edit is not written to any
  log; a failed edit only logs an error.
Buttons use custom_id "qav:<ask_id>:<vote>" (DynamicItem), so they survive
restarts. No Gemini calls.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets

from db import get_meta, is_opted_out, set_meta

log = logging.getLogger("qa_bot")

VOTES = {"yes": ("✅", "Correct"), "partly": ("➖", "Partly"), "no": ("❌", "Wrong")}
CUSTOM_ID_RE = r"qav:(?P<ask_id>[0-9]+):(?P<vote>yes|partly|no)"
NOT_OFFICER = "Only officers can rate answers."
PROMPT = "Officers only: is this answer right?"
THANKS = "Thank you for your verification."
BADGE = "✅ Verified by officers"
DEFAULT_MIN_YES, DEFAULT_MAX_NO = 2, 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS ask_feedback(
  ask_id INTEGER NOT NULL REFERENCES ask_log(id),
  voter TEXT NOT NULL,
  vote TEXT NOT NULL CHECK(vote IN ('yes','partly','no')),
  note TEXT,
  ts TEXT NOT NULL,
  PRIMARY KEY(ask_id, voter));
CREATE INDEX IF NOT EXISTS ask_feedback_ts ON ask_feedback(ts);
CREATE TABLE IF NOT EXISTS ask_message(
  message_id TEXT PRIMARY KEY,
  ask_id INTEGER NOT NULL REFERENCES ask_log(id),
  channel_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ask_message_ask ON ask_message(ask_id);
CREATE VIEW IF NOT EXISTS ask_feedback_summary AS
  SELECT l.id AS ask_id, l.ts, l.question, l.decision, l.confidence,
         SUM(f.vote='yes') AS yes, SUM(f.vote='partly') AS partly, SUM(f.vote='no') AS no,
         COUNT(f.vote) AS votes,
         CASE WHEN COUNT(f.vote)=0 THEN NULL
              WHEN SUM(f.vote='no') > SUM(f.vote='yes') THEN 'wrong'
              WHEN SUM(f.vote='yes') > SUM(f.vote='no') + SUM(f.vote='partly') THEN 'verified'
              ELSE 'mixed' END AS verdict
  FROM ask_log l LEFT JOIN ask_feedback f ON f.ask_id = l.id GROUP BY l.id;
"""


def ensure_table(conn) -> None:
    """Additive only: new table, index and view. Never touches ask_log rows."""
    from asklog import ensure_table as ensure_ask_log
    ensure_ask_log(conn)
    conn.executescript(SCHEMA)


def _salt(conn) -> str:
    value = get_meta(conn, "feedback_salt")
    if not value:
        value = secrets.token_hex(16)
        set_meta(conn, "feedback_salt", value)
        conn.commit()
    return value


def voter_key(conn, user_id) -> str:
    return hashlib.sha256((_salt(conn) + ":" + str(user_id)).encode()).hexdigest()[:32]


def record_vote(conn, ask_id: int, user_id, tier: str, vote: str, note: str | None = None) -> str:
    """Returns 'saved', 'changed', 'not_officer', 'opted_out', 'bad_vote' or 'no_question'."""
    from tiers import is_officer
    if not is_officer(tier):
        return "not_officer"
    if vote not in VOTES:
        return "bad_vote"
    if is_opted_out(conn, str(user_id)):
        return "opted_out"
    ensure_table(conn)
    if conn.execute("SELECT 1 FROM ask_log WHERE id=?", (int(ask_id),)).fetchone() is None:
        return "no_question"
    key = voter_key(conn, user_id)
    old = conn.execute("SELECT vote FROM ask_feedback WHERE ask_id=? AND voter=?", (int(ask_id), key)).fetchone()
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO ask_feedback(ask_id, voter, vote, note, ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(ask_id, voter) DO UPDATE SET vote=excluded.vote, ts=excluded.ts, "
        "note=COALESCE(excluded.note, ask_feedback.note)",
        (int(ask_id), key, vote, (note or "").strip()[:500] or None, ts),
    )
    conn.commit()
    return "changed" if old else "saved"


def tally(conn, ask_id: int) -> dict:
    ensure_table(conn)
    row = conn.execute("SELECT yes, partly, no, votes, verdict FROM ask_feedback_summary WHERE ask_id=?",
                       (int(ask_id),)).fetchone()
    if row is None:
        return {"yes": 0, "partly": 0, "no": 0, "votes": 0, "verdict": None}
    return {"yes": row[0] or 0, "partly": row[1] or 0, "no": row[2] or 0, "votes": row[3] or 0, "verdict": row[4]}


def flagged_since(conn, since_ts: str) -> list[dict]:
    """Answers officers marked wrong/partly (with notes) since a time; for the nightly misses review."""
    ensure_table(conn)
    rows = conn.execute(
        "SELECT l.id, l.question, l.answer, f.vote, f.note FROM ask_feedback f JOIN ask_log l ON l.id=f.ask_id "
        "WHERE f.ts >= ? AND f.vote IN ('no','partly') ORDER BY f.ts", (since_ts,)).fetchall()
    return [dict(zip(("ask_id", "question", "answer", "vote", "note"), r)) for r in rows]


def reply_for(outcome: str, vote: str, counts: dict) -> str:
    if outcome == "not_officer":
        return NOT_OFFICER
    if outcome == "opted_out":
        return "You're opted out of logging, so your rating isn't stored."
    if outcome in ("no_question", "bad_vote"):
        return "That answer can't be rated anymore."
    emoji, label = VOTES[vote]
    verb = "Changed your rating to" if outcome == "changed" else "Rated"
    return (f"{verb} {emoji} {label}. Officer tally: ✅ {counts['yes']} · ➖ {counts['partly']} · ❌ {counts['no']}")




def link_message(conn, ask_id, message_id, channel_id) -> None:
    """Remember which public message carries this ask_log answer (for the context menu and badge)."""
    if ask_id is None or message_id is None:
        return
    ensure_table(conn)
    conn.execute("INSERT OR REPLACE INTO ask_message(message_id, ask_id, channel_id) VALUES(?,?,?)",
                 (str(message_id), int(ask_id), str(channel_id or "")))
    conn.commit()


def ask_id_for_message(conn, message_id) -> int | None:
    ensure_table(conn)
    row = conn.execute("SELECT ask_id FROM ask_message WHERE message_id=?", (str(message_id),)).fetchone()
    return None if row is None else int(row[0])


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def find_by_content(conn, content: str, created_at=None, window_s: int = 900) -> int | None:
    """Fallback for answers posted before message links were recorded: the ask_log row whose
    answer is exactly this message's text (as posted, clipped to 2000), logged within window_s
    of the message time. Returns an id only when exactly one row matches."""
    target = _norm(content)
    if not target:
        return None
    ensure_table(conn)
    rows = conn.execute("SELECT id, ts, answer FROM ask_log WHERE decision != 'error'").fetchall()
    hits = []
    for rid, ts, answer in rows:
        if _norm((answer or "")[:2000]) != target:
            continue
        if created_at is not None:
            try:
                logged = dt.datetime.fromisoformat(ts)
                if abs((logged - created_at).total_seconds()) > window_s:
                    continue
            except Exception:
                continue
        hits.append(int(rid))
    return hits[0] if len(hits) == 1 else None


def resolve_message(conn, message_id, content, created_at, channel_id) -> int | None:
    """Linked id, else an unambiguous content match (which then gets linked for next time)."""
    rid = ask_id_for_message(conn, message_id)
    if rid is None:
        rid = find_by_content(conn, content, created_at)
        if rid is not None:
            link_message(conn, rid, message_id, channel_id)
    return rid


def messages_for(conn, ask_id) -> list[tuple[str, str]]:
    ensure_table(conn)
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT message_id, channel_id FROM ask_message WHERE ask_id=?", (int(ask_id),))]


def thresholds() -> tuple[int, int]:
    try:
        from voice import get_runtime
        rt = get_runtime()
        return int(getattr(rt, "verify_min_yes", DEFAULT_MIN_YES)), int(getattr(rt, "verify_max_no", DEFAULT_MAX_NO))
    except Exception:
        return DEFAULT_MIN_YES, DEFAULT_MAX_NO


def is_verified(counts: dict, min_yes: int | None = None, max_no: int | None = None) -> bool:
    dy, dn = thresholds()
    min_yes = dy if min_yes is None else min_yes
    max_no = dn if max_no is None else max_no
    return counts.get("yes", 0) >= max(1, min_yes) and counts.get("no", 0) <= max_no


def badged_text(text: str) -> str:
    """Answer body without the 'Based on:' sources, plus the badge."""
    from llm import FALLBACK_TAG

    tagged = FALLBACK_TAG in (text or "")
    body = (text or "").replace(FALLBACK_TAG, "").split("\n\nBased on")[0].rstrip()
    tail = f"\n{FALLBACK_TAG}" if tagged else ""
    if BADGE not in body:
        body = f"{body}\n\n{BADGE}"
    return body[: 2000 - len(tail)] + tail


def vote_in_new_connection(db_path, ask_id, user_id, tier, vote, note):
    """Returns (outcome, reply_text, verified_now)."""
    from db import connect
    conn = connect(db_path)
    try:
        outcome = record_vote(conn, ask_id, user_id, tier, vote, note)
        if outcome not in ("saved", "changed"):
            return outcome, reply_for(outcome, vote, {}), []
        counts = tally(conn, ask_id)
        targets = messages_for(conn, ask_id) if is_verified(counts) else []
        return outcome, THANKS, targets
    except Exception:
        log.exception("feedback vote failed")
        return "error", "Couldn't save that rating. Try again in a minute.", []
    finally:
        conn.close()


async def apply_badge(client, targets) -> int:
    """Edit each public answer to drop sources and show the badge. Not logged; errors only."""
    done = 0
    for message_id, channel_id in targets:
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            msg = await channel.fetch_message(int(message_id))
            if BADGE in (msg.content or ""):
                continue
            await msg.edit(content=badged_text(msg.content))
            done += 1
        except Exception:
            log.error("verified badge edit failed for an answer message", exc_info=True)
    return done


async def finish_vote(interaction, ask_id, vote, note) -> None:
    """Shared by the buttons and the modal: save, swap the prompt for a thank-you, maybe badge."""
    import asyncio
    from tiers import member_tier
    outcome, text, targets = await asyncio.to_thread(
        vote_in_new_connection, interaction.client.cfg.db_path, ask_id,
        interaction.user.id, member_tier(interaction.user), vote, note)
    if outcome in ("saved", "changed"):
        await interaction.response.edit_message(content=THANKS, view=None)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    if targets:
        await apply_badge(interaction.client, targets)


try:
    import discord

    class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_RE):
        def __init__(self, ask_id: int, vote: str) -> None:
            emoji, label = VOTES[vote]
            super().__init__(discord.ui.Button(
                label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                custom_id=f"qav:{int(ask_id)}:{vote}"))
            self.ask_id, self.vote = int(ask_id), vote

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /):
            return cls(int(match["ask_id"]), match["vote"])

        async def callback(self, interaction: discord.Interaction) -> None:
            from tiers import is_officer, member_tier
            if not is_officer(member_tier(interaction.user)):
                await interaction.response.send_message(NOT_OFFICER, ephemeral=True)
                return
            if self.vote in ("no", "partly"):
                await interaction.response.send_modal(CorrectionModal(self.ask_id, self.vote))
                return
            await finish_vote(interaction, self.ask_id, self.vote, None)

    class CorrectionModal(discord.ui.Modal, title="What should the answer say?"):
        note = discord.ui.TextInput(label="Correction (optional)", style=discord.TextStyle.paragraph,
                                    required=False, max_length=500)

        def __init__(self, ask_id: int, vote: str) -> None:
            super().__init__(timeout=600)
            self.ask_id, self.vote = ask_id, vote

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await finish_vote(interaction, self.ask_id, self.vote, str(self.note.value or ""))

    def vote_view(ask_id: int) -> "discord.ui.View":
        view = discord.ui.View(timeout=None)
        for vote in VOTES:
            view.add_item(VoteButton(ask_id, vote))
        return view
except ImportError:  # pragma: no cover
    VoteButton = CorrectionModal = None

    def vote_view(ask_id):
        return None
