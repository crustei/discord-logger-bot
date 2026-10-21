"""Decide whether a question is answered, held, or empty, then build a short reply.

General questions (dates, events, how-tos, team structure) are answered from
the knowledge directory and from channels the asker can see. Knowledge files
answer directly unless ``docs_visibility`` or a file's ``visibility`` front
matter says ``approval``. Questions about a specific person's private
information wait for an officer. Blocklisted channels and opted-out members
are never retrieved.

The model only receives the string from ``select_context``. A reply that copies
those snippets is sent back once to be rephrased. Words are not deleted from
the model text. The reply is redacted and kept inside the character cap.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from db import connect, create_approval
from llm import ExtractiveProvider, LLMError
from permissions import is_excluded, message_in_scope
from retrieve import Hit, merge_hits, select_context
from voice import RuntimeConfig, get_runtime


@dataclass
class Result:
    kind: str  # answer, pending, empty
    reason: str
    context_block: str
    snippets: list[Hit] = field(default_factory=list)
    text: str = ""
    approval_id: int | None = None
    llm_called: bool = False
    dry_run: bool = False


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def neutralize_mentions(text: str) -> str:
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<@!?\d+>", "@user", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    return text


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def fit_answer(body: str, footer: str, limit: int | None = None) -> str:
    """Keep the source footer, and stay within the character cap from settings.toml."""
    if limit is None:
        limit = get_runtime().max_answer_chars
    body = neutralize_mentions(body or "")
    footer = neutralize_mentions(footer or "")
    if not footer:
        return clip(body, limit)
    separator = "\n\n"
    if len(footer) + len(separator) >= limit:
        return clip(footer, limit)
    room = limit - len(footer) - len(separator)
    if len(body) > room:
        body = clip(body, room)
    combined = f"{body}{separator}{footer}" if body else footer
    return clip(combined, limit)


def pending_message(approval_id: int | None, runtime: RuntimeConfig | None = None) -> str:
    runtime = runtime or get_runtime()
    return runtime.held(approval_id)


def _is_refusal(text: str, runtime: RuntimeConfig, limit: int) -> bool:
    from answerstyle import unknown_reply

    return (text or "").strip() == clip(unknown_reply(runtime), limit)


def explain(kind: str, asker_id: str, review: list[Hit]) -> str:
    if kind == "answer":
        return "Answered from the knowledge files and channels this member can see."
    if kind == "empty":
        return "No matching notes in the knowledge files or the channels you can see."
    return "Held for approval because the question asks about someone's private information."


def split_for_answer(
    found,
    *,
    asker_id: str,
    question: str,
    excluded,
    scope: set[str],
    runtime: RuntimeConfig | None = None,
    here: set[str] | None = None,
    public: set[str] | None = None,
) -> tuple[list[Hit], list[Hit]]:
    """Public knowledge and general notes answer now. Private questions wait.

    ``here`` and ``public`` are accepted so older callers keep working. Ranking
    no longer limits an automatic answer to the asker's own messages.
    """
    from answerstyle import about_self, asks_for_private_detail, doc_is_public
    from retrieve import order_messages

    runtime = runtime or get_runtime()
    messages: list[Hit] = []
    for hit in list(found.own) + list(found.others):
        if not message_in_scope(
            hit.channel_id,
            hit.channel_name,
            scope=scope,
            excluded=excluded,
            guild_id=hit.guild_id,
        ):
            continue
        if (hit.text or "").strip():
            messages.append(hit)
    public_docs: list[Hit] = []
    held_docs: list[Hit] = []
    for hit in found.docs:
        if not (hit.text or "").strip():
            continue
        if doc_is_public(hit, runtime.docs_visibility):
            public_docs.append(hit)
        else:
            held_docs.append(hit)
    # "What did I say" stays on that member's messages. General questions merge
    # knowledge files in front of chat, instead of preferring the asker's rows.
    if about_self(question):
        ranked = order_messages(messages, question)
    else:
        ranked = merge_hits(public_docs, messages, prefer_docs=True, question=question)
    private = asks_for_private_detail(
        question,
        runtime.sensitive_topics,
        ranked + held_docs,
        asker_id,
        runtime.docs_visibility,
    )
    if private:
        review = merge_hits(
            public_docs + held_docs,
            messages,
            prefer_docs=not about_self(question),
            question=question,
        )
        return [], review
    if ranked:
        return ranked, []
    return [], held_docs


def compose_answer(
    question: str,
    hits: list[Hit],
    llm,
    *,
    runtime: RuntimeConfig | None = None,
    limit: int | None = None,
) -> tuple[str, str, list[Hit], bool]:
    """Return posted text, the exact context block, the snippets in it, and whether the LLM ran."""
    runtime = runtime or get_runtime()
    if limit is None:
        limit = runtime.max_answer_chars
    kept, block = select_context(hits, limit=runtime.max_context_snippets)
    if not kept:
        return "", "", [], False
    from answerstyle import copies_source, present_answer, rephrase_request, unknown_reply

    def _ask(prompt: str) -> str:
        try:
            return llm.answer(prompt, block).strip()
        except LLMError:
            return ""

    def _offline(body: str) -> bool:
        return "here are the notes" in (body or "").casefold()

    def _too_close(body: str) -> bool:
        if not body or _offline(body):
            return False
        from answerstyle import _clean_model_text

        cleaned = _clean_model_text(body, kept)
        return copies_source(body, kept) or (bool(cleaned) and copies_source(cleaned, kept))

    body = _ask(question)
    called = bool(body)
    if body and _too_close(body):
        body = _ask(rephrase_request(question))
        called = True
        if not body or _offline(body) or _too_close(body):
            text = fit_answer(unknown_reply(runtime), "", limit)
            return text, block, kept, called
    if not body:
        text = fit_answer(unknown_reply(runtime), "", limit)
        return text, block, kept, False
    text = present_answer(question, body, kept, runtime, limit=limit)
    return text, block, kept, called


def handle_question(
    conn,
    question: str,
    *,
    asker_id: str,
    asker_name: str = "",
    guild_id: str | None = None,
    channel_id: str,
    channel_name: str | None,
    visible: set[str],
    public_channels: set[str] | None = None,
    here: set[str] | None = None,
    excluded=None,
    docs_dir: str,
    llm=None,
    dry_run: bool = False,
    runtime: RuntimeConfig | None = None,
) -> Result:
    from retrieve import retrieve

    runtime = runtime or get_runtime()
    runtime.reload_if_changed()
    limit = runtime.max_answer_chars
    question = (question or "").strip()[:500]
    if public_channels is None:
        public_channels = set(runtime.public_channels)
    excluded = runtime.blocklist if excluded is None else excluded
    if here is None:
        here = set()
        if channel_id:
            here.add(str(channel_id))
        if channel_name:
            here.add(channel_name)
            here.add(f"#{channel_name}")
    if is_excluded(channel_id, channel_name, excluded, guild_id):
        return Result(
            kind="empty",
            reason="This channel is excluded. Nothing was retrieved.",
            context_block="",
            text=clip(runtime.say("excluded_channel"), limit),
        )
    if not question:
        return Result(
            kind="empty",
            reason="No question text.",
            context_block="",
            text=clip(runtime.say("empty_question"), limit),
        )
    scope = set(visible) | set(here) | set(public_channels)
    found = retrieve(
        conn,
        question,
        asker_id=str(asker_id),
        scope=scope,
        excluded=excluded,
        docs_dir=docs_dir,
        llm=None if dry_run else llm,
    )
    auto, review = split_for_answer(
        found,
        asker_id=str(asker_id),
        question=question,
        excluded=excluded,
        scope=scope,
        runtime=runtime,
        here=set(here),
        public=set(public_channels),
    )
    if auto:
        kept, block = select_context(auto, limit=runtime.max_context_snippets)
        reason = explain("answer", str(asker_id), review)
        if dry_run:
            return Result(
                kind="answer",
                reason=reason,
                context_block=block,
                snippets=kept,
                text="",
                dry_run=True,
                llm_called=False,
            )
        if llm is None:
            llm = ExtractiveProvider()
        text, block, kept, called = compose_answer(question, auto, llm, runtime=runtime, limit=limit)
        if _is_refusal(text, runtime, limit):
            return Result(
                kind="empty",
                reason=explain("empty", str(asker_id), []),
                context_block=block,
                snippets=kept,
                text=text,
                llm_called=called,
            )
        return Result(
            kind="answer",
            reason=reason,
            context_block=block,
            snippets=kept,
            text=text,
            llm_called=called,
        )
    if review:
        kept, block = select_context(review, limit=runtime.max_context_snippets)
        reason = explain("pending", str(asker_id), kept)
        if dry_run:
            notice = clip(pending_message(None, runtime), limit)
            return Result(
                kind="pending",
                reason=reason,
                context_block=block,
                snippets=kept,
                text=notice,
                dry_run=True,
                llm_called=False,
            )
        approval_id = create_approval(
            conn,
            created_at=utcnow(),
            guild_id=guild_id,
            channel_id=str(channel_id),
            channel_name=channel_name,
            asker_id=str(asker_id),
            asker_name=asker_name,
            question=question,
            context=[hit.to_dict() for hit in kept],
            scope=scope,
            excluded=excluded,
        )
        if approval_id is None:
            return Result(
                kind="empty",
                reason=explain("empty", str(asker_id), []),
                context_block="",
                text=clip(runtime.say("refusal"), limit),
            )
        return Result(
            kind="pending",
            reason=reason,
            context_block=block,
            snippets=kept,
            text=clip(pending_message(approval_id, runtime), limit),
            approval_id=approval_id,
            llm_called=False,
        )
    return Result(
        kind="empty",
        reason=explain("empty", str(asker_id), []),
        context_block="",
        text=clip(runtime.say("refusal"), limit),
    )


def answer_in_new_connection(db_path: str, **kwargs) -> Result:
    conn = connect(db_path)
    try:
        return handle_question(conn, **kwargs)
    finally:
        conn.close()
