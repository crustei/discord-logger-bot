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
import os
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


def with_fallback_tag(text: str, limit: int | None = None) -> str:
    """Append the tiny Discord subtext line that marks a non-AI (backup mode) answer."""
    from llm import FALLBACK_TAG

    if not text or FALLBACK_TAG in text:
        return text
    if limit is None:
        limit = get_runtime().max_answer_chars
    room = max(0, limit - len(FALLBACK_TAG) - 1)
    return f"{clip(text, room)}\n{FALLBACK_TAG}"


def backup_answer(question: str, kept, runtime, limit: int, why: str) -> str:
    """Model down: answer from the matching KB snippets without AI. Don't-know when none fits."""
    import logging

    from answerstyle import present_answer, unknown_reply

    text = present_answer(question, "Here are the notes", kept, runtime, limit=limit)
    if not text or text == fit_answer(unknown_reply(runtime), "", limit):
        return text
    logging.getLogger("llm").warning("llm fallback extractive answer (%s)", why[:80])
    try:
        import counters

        counters.bump("llm_fallback_extractive", quiet=True)
    except Exception:
        pass
    return with_fallback_tag(text, limit)


def pending_message(approval_id: int | None, runtime: RuntimeConfig | None = None) -> str:
    runtime = runtime or get_runtime()
    return runtime.held(approval_id)


_GAP = re.compile(
    r"(?i)\b(?:i\s+(?:do\s*n[o']?t|don't|dont)\s+know|i'?m\s+not\s+sure|not\s+sure|no\s+information|"
    r"(?:does|do|did)\s*(?:n[o']?t|n't)\s+(?:state|say|mention|specify|include|contain|provide|list|cover|indicate)|"
    r"(?:is|are)\s+not\s+(?:stated|mentioned|specified|listed|provided|included|available|known|clear)|"
    r"(?:isn't|aren't)\s+(?:stated|mentioned|specified|listed|provided|included|available|known|clear)|"
    r"not\s+in\s+the\s+(?:context|notes|documents?|sources?)|"
    r"(?:the\s+)?(?:provided|available|given)\s+(?:context|notes|documents?|sources?|information)|"
    r"\bcontext\s+(?:does|block)|unable\s+to\s+(?:find|determine|answer)|can(?:no|')t\s+(?:find|determine|answer|tell))"
)


def is_gap_answer(text: str) -> bool:
    """True when a reply says it does not know, or hedges about what the notes leave out."""
    return bool(_GAP.search(text or ""))


_CLAIM = re.compile(r"\b(?:\d[\d:/.,-]*\d|\d|[A-Z][A-Za-z0-9&'-]{2,}(?:\s+[A-Z][A-Za-z0-9&'-]{2,})*)")
_COMMON = {
    "the", "this", "that", "these", "they", "there", "it", "its", "you", "your", "yes", "no", "as", "of",
    "and", "but", "for", "see", "source", "sources", "note", "also", "per", "based", "currently", "today",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "discord", "i", "we", "our", "if", "when", "where", "who", "what", "how", "here", "please", "ask",
}


def is_grounded(answer: str, context_block: str, question: str = "") -> bool:
    """Local grounding check (no model call): names, numbers and dates in the answer
    must appear in the retrieved entries or in the question."""
    body = re.sub(r"https?://\S+|<[^>]+>|\[\d+\]", " ", (answer or "").split("\n\nSource")[0])
    haystack = ((context_block or "") + " " + (question or "")).casefold()
    claims = []
    for match in _CLAIM.finditer(body):
        token = match.group(0)
        before = body[: match.start()].rstrip()
        if " " not in token and not token[0].isdigit() and (not before or before[-1] in ".!?:-*\n"):
            continue  # an ordinary word capitalized at the start of a sentence
        words = [w for w in token.split() if w.casefold() not in _COMMON]
        if words:
            claims.append(" ".join(words))
    if not claims:
        return True
    missing = [c for c in claims if all(w.casefold().strip(".,") not in haystack for w in c.split())]
    return len(missing) <= max(0, len(claims) // 5)


UNAVAILABLE = "I'm temporarily unavailable. Please try again shortly."


def _refusal(runtime) -> str:
    from answerstyle import unknown_reply

    return unknown_reply(runtime)


def _is_refusal(text: str, runtime: RuntimeConfig, limit: int) -> bool:
    from answerstyle import unknown_reply

    body = (text or "").strip()
    if body == clip(unknown_reply(runtime), limit) or body == clip(_refusal(runtime), limit):
        return True
    # A partial answer states a fact first and may then note what isn't stated; only a reply
    # whose first clause is itself a gap counts as a refusal.
    first = re.split(r"(?<=[.!?;])\s+|;\s*|\s+[-\u2014]\s+|,\s+(?:but|while|though|although|however)\s+", body.split("\n\n")[0].strip(), maxsplit=1)[0]
    return is_gap_answer(first)


def explain(kind: str, asker_id: str, review: list[Hit]) -> str:
    if kind == "answer":
        return "Answered from the knowledge base entries this member may see."
    if kind == "empty":
        return "No matching knowledge base entry for this member."
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
    from retrieve import rerank

    ranked = rerank(hits, question, llm) if question else hits
    kept, block = select_context(ranked, limit=runtime.max_context_snippets, question=question, ranked=bool(question))
    if not kept:
        return "", "", [], False
    from answerstyle import copies_source, present_answer, rephrase_request, unknown_reply

    failed = []

    def _ask(prompt: str) -> str:
        import time

        from llm import primary_exhausted

        for wait in (0, 2, 5):
            if primary_exhausted():
                failed.append("primary exhausted")
                break
            if wait:
                time.sleep(wait * float(os.environ.get("LLM_BACKOFF_SCALE", "1")))
            try:
                return llm.answer(prompt, block).strip()
            except LLMError as exc:  # 429 or outage: back off and retry twice
                failed.append(str(exc))
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
    if not body and failed:
        text = backup_answer(question, kept, runtime, limit, failed[-1])
        return (text or UNAVAILABLE), block, kept, False
    called = bool(body)
    if body and _too_close(body):
        n_failed = len(failed)
        body = _ask(rephrase_request(question))
        called = True
        if not body and len(failed) > n_failed:
            text = backup_answer(question, kept, runtime, limit, failed[-1])
            if text:
                return text, block, kept, False
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
    asker_tier: str = "non-member",
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
        asker_tier=asker_tier,
        include_messages=getattr(runtime, "answer_from", "knowledge") == "knowledge_and_messages",
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
        kept, block = select_context(auto, limit=runtime.max_context_snippets, question=question)
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
        if text == UNAVAILABLE:
            return Result(kind="error", reason="The model was unavailable after retries.",
                          context_block=block, snippets=kept, text=UNAVAILABLE, llm_called=False)
        if called and not _is_refusal(text, runtime, limit):
            if not is_grounded(text, block, question):
                from answerstyle import unknown_reply

                text = fit_answer(unknown_reply(runtime), "", limit)  # not supported by the entries
        if _is_refusal(text, runtime, limit):
            from answerstyle import unknown_reply

            text = fit_answer(unknown_reply(runtime), "", limit)
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
        kept, block = select_context(review, limit=runtime.max_context_snippets, question=question)
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
                text=clip(_refusal(runtime), limit),
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
        text=clip(_refusal(runtime), limit),
    )


def answer_in_new_connection(db_path: str, **kwargs) -> Result:
    conn = connect(db_path)
    try:
        return handle_question(conn, **kwargs)
    finally:
        conn.close()
