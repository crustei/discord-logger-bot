"""Turn retrieved notes into a short answer.

Posted text is one to three sentences. A model reply keeps the words it wrote.
If that reply copies a long stretch of a snippet, the model is asked once to
rephrase, and a second copy becomes the don't-know line. With no model, matching
notes are paraphrased locally. The reply does not name who said something, and
it does not leave emails, phone numbers, street addresses, or similar patterns.
A configurable citation line names the doc title or channel and the date, without
a link.

Knowledge files answer directly when ``docs_visibility`` is ``public`` (the
default) or a file's front matter says ``visibility: public``. ``approval``
holds those files for an officer. General questions rank ``facts.md`` and
other docs above loose messages.
"""
from __future__ import annotations

import re

WORD = re.compile(r"[0-9A-Za-z']+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\w)")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z0-9.'-]+\s+){0,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr)\b\.?",
    re.IGNORECASE,
)
GPA = re.compile(r"\bGPA\s*[:=]?\s*\d(?:\.\d+)?\b", re.IGNORECASE)

# Small, generic expansions so a natural question still hits BM25.
_EXPANSIONS = {
    "join": ("recruitment", "application", "apply"),
    "joining": ("recruitment", "application", "apply"),
    "recruitment": ("join", "application", "apply"),
    "apply": ("application", "recruitment"),
    "application": ("apply", "recruitment"),
    "meeting": ("session", "meet"),
    "session": ("meeting",),
    "meet": ("meeting", "session"),
    "schedule": ("date", "when"),
    "cost": ("price",),
    "price": ("cost",),
}


def expand_terms(question: str) -> list[str]:
    """Extra search words. The original question is still searched."""
    from retrieve import tokens_for

    found: list[str] = []
    seen = {token.casefold() for token in tokens_for(question)}
    for token in list(seen):
        if len(token) > 4 and token.endswith("s") and token[:-1] not in seen:
            found.append(token[:-1])
        for extra in _EXPANSIONS.get(token, ()):
            if extra not in seen and extra not in found:
                found.append(extra)
    return found[:12]


def doc_is_public(hit, default_visibility: str) -> bool:
    """Per-file front matter wins. Otherwise the ``docs_visibility`` setting."""
    choice = str(getattr(hit, "visibility", "") or "").strip().casefold()
    if choice not in {"public", "approval"}:
        choice = str(default_visibility or "public").strip().casefold()
    return choice != "approval"


def about_self(question: str) -> bool:
    """True when the asker is asking what they themselves said or wrote."""
    text = " ".join((question or "").casefold().split())
    return re.search(r"\b(did i|do i|have i|i say|i said|i write|i wrote|i mention|i mentioned)\b", text) is not None


def _topic_hit(question: str, topic: str) -> bool:
    token = " ".join(str(topic).casefold().split())
    if not token:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", question) is not None


def asks_for_private_detail(
    question: str,
    topics,
    hits,
    asker_id: str,
    docs_visibility: str = "public",
) -> bool:
    """Hold questions about a specific person's private information.

    A general date, event, how-to, or team question is not private, even when
    the supporting note is someone else's message or a knowledge file.
    """
    text = " ".join((question or "").casefold().split())
    if not text:
        return False
    for topic in topics or []:
        if _topic_hit(text, topic):
            return True
    if re.search(r"\bwhat did\b.+\b(say|said|tell|told|write|wrote|mention|mentioned)\b", text):
        return True
    if re.search(r"\b(vendor quote|how much did i|how much did they|how much did he|how much did she)\b", text):
        return True
    for hit in hits or []:
        if getattr(hit, "kind", "") != "message":
            continue
        if str(getattr(hit, "author_id", "") or "") == str(asker_id):
            continue
        for part in str(getattr(hit, "author", "") or "").split():
            if len(part) < 3:
                continue
            if part.casefold() in text and re.search(r"\b(say|said|email|phone|grade|address|quote)\b", text):
                return True
    if re.search(r"\bhow much\b", text):
        if not any(getattr(hit, "kind", "") == "doc" and doc_is_public(hit, docs_visibility) for hit in hits or []):
            return True
    return False


def word_runs(text: str) -> list[str]:
    return WORD.findall((text or "").lower())


def verbatim_overlap(answer: str, sources, window: int = 6) -> bool:
    """True when ``window`` source words appear in order in the answer."""
    answer_words = word_runs(answer)
    if window < 1 or len(answer_words) < window:
        return False
    sequences = {" ".join(answer_words[index : index + window]) for index in range(len(answer_words) - window + 1)}
    for source in sources:
        source_words = word_runs(source or "")
        for index in range(len(source_words) - window + 1):
            if " ".join(source_words[index : index + window]) in sequences:
                return True
    return False


def redact_pii(text: str) -> str:
    """Replace contact-detail patterns. Dates and ordinary numbers stay."""
    cleaned = EMAIL.sub("[redacted]", text or "")
    cleaned = PHONE.sub("[redacted]", cleaned)
    cleaned = SSN.sub("[redacted]", cleaned)
    cleaned = ADDRESS.sub("[redacted]", cleaned)
    cleaned = GPA.sub("[redacted]", cleaned)
    return " ".join(cleaned.split())


def strip_author_names(text: str, hits) -> str:
    cleaned = text or ""
    for hit in hits or []:
        for part in str(getattr(hit, "author", "") or "").split():
            if len(part) < 3:
                continue
            cleaned = re.sub(rf"\b{re.escape(part)}\b", "a member", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def limit_sentences(text: str, count: int = 3) -> str:
    return " ".join(_sentences(text)[:count])


def _best_sentence(text: str, question: str) -> str:
    from retrieve import tokens_for

    sentences = _sentences(text) or [" ".join((text or "").split())]
    wanted = set(tokens_for(question))

    def score(sentence: str) -> int:
        return len(set(tokens_for(sentence)) & wanted)

    return max(sentences, key=score)


def _paraphrase(sentence: str) -> str:
    text = (sentence or "").strip().strip('"').strip()
    swaps = (
        (r"\bI will\b", "The plan is to"),
        (r"\bI'll\b", "The plan is to"),
        (r"\b[Mm]y\b", "the"),
        (r"\bis at\b", "happens at"),
        (r"\bis in\b", "meets in"),
        (r"\bgo in\b", "belong in"),
        (r"\bstarting\b", "from"),
        (r"\bopen on\b", "begin"),
        (r"\bcan be sent to\b", "go to"),
        (r"\bI think\b", "One note says"),
        (r"\bare\b", "happen on"),
    )
    for pattern, replacement in swaps:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\bI\b", "", text)
    text = " ".join(text.split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def _drop_overlapping_words(text: str, sources, window: int = 6) -> str:
    """Offline paraphrase helper. Never call this on text the model wrote."""
    kept: list[str] = []
    source_grams = []
    for source in sources:
        words = word_runs(source)
        source_grams.append(
            {" ".join(words[index : index + window]) for index in range(max(0, len(words) - window + 1))}
        )
    for word in (text or "").split():
        trial = kept + [word]
        runs = word_runs(" ".join(trial))
        if len(runs) >= window:
            gram = " ".join(runs[-window:])
            if any(gram in grams for grams in source_grams):
                continue
        kept.append(word)
    return " ".join(kept)


def _question_overlap(text: str, question: str) -> int:
    from retrieve import tokens_for

    return len(set(tokens_for(text)) & set(tokens_for(question)))


def synthesize(question: str, hits) -> str:
    """One plain sentence from the note that shares the most question words.

    Docs win ties, so a knowledge file is used when it matches as well as chat.
    """
    if not hits:
        return ""
    from retrieve import is_facts_hit, partial_topic_match

    hit = max(
        hits,
        key=lambda item: (
            1 if partial_topic_match(question, item) else 0,
            _question_overlap(getattr(item, "text", "") or "", question),
            1 if getattr(item, "kind", "") == "doc" else 0,
            1 if is_facts_hit(item) else 0,
            -len(getattr(item, "text", "") or ""),
        ),
    )
    sources = [getattr(item, "text", "") or "" for item in hits]
    sentence = _paraphrase(_best_sentence(getattr(hit, "text", "") or "", question))
    sentence = redact_pii(strip_author_names(sentence, hits))
    if verbatim_overlap(sentence, sources):
        sentence = _drop_overlapping_words(sentence, sources)
        sentence = redact_pii(sentence)
    if sentence and sentence[-1] not in ".!?":
        sentence += "."
    sentence = strip_source_labels(sentence, hits)
    if verbatim_overlap(sentence, sources) or _question_overlap(sentence, question) < 1:
        return ""
    return sentence


def citation_line(hits) -> str:
    """Doc title or channel, plus a date. No paths and no links."""
    from retrieve import day_of

    parts: list[str] = []
    seen: set[str] = set()
    for hit in hits or []:
        if getattr(hit, "kind", "") == "doc":
            title = (getattr(hit, "title", "") or "note").strip() or "note"
            when = day_of(getattr(hit, "modified", None) or getattr(hit, "timestamp", None))
            label = f"{title}, {when}" if when != "unknown-date" else title
        else:
            channel = getattr(hit, "channel_name", None) or "channel"
            when = day_of(getattr(hit, "timestamp", None))
            label = f"#{channel}, {when}" if when != "unknown-date" else f"#{channel}"
        if label in seen:
            continue
        seen.add(label)
        parts.append(label)
        if len(parts) >= 3:
            break
    if not parts:
        return ""
    return "Based on: " + "; ".join(parts)


def unknown_reply(runtime) -> str:
    role = getattr(runtime, "officer_role", "") or "an officer"
    channel = str(getattr(runtime, "officer_channel", "") or "").strip().lstrip("#")
    where = f" in #{channel}" if channel else ""
    template = runtime.say("refusal")
    return (
        template.replace("{officer_role}", role)
        .replace("{officer_channel}", f"#{channel}" if channel else "")
        .replace("{officer_where}", where)
    )


def _sources_of(hits) -> list[str]:
    return [getattr(hit, "text", "") or "" for hit in hits or []]


_LABEL_TAIL = re.compile(r"(?:\(|\[)?[A-Z][^.;\n]{0,48};\s*[A-Z][^)\n]{0,48}\)")
_INDEXED = re.compile(r"\[\d+\]")
_BASED_ON = re.compile(r"(?im)^based on:.*$")


def strip_source_labels(text: str, hits=None) -> str:
    """Drop index markers, citation lines, and 'Title; Other title)' fragments.

    A document title is left alone when it is also the subject of the sentence.
    """
    cleaned = text or ""
    cleaned = cleaned.replace("…", " ")
    cleaned = _INDEXED.sub(" ", cleaned)
    cleaned = _BASED_ON.sub(" ", cleaned)
    cleaned = _LABEL_TAIL.sub(" ", cleaned)
    for hit in hits or []:
        label = str(getattr(hit, "label", lambda: "")() or "").strip()
        if len(label) >= 4:
            cleaned = cleaned.replace(label, " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip(" -–—:;,)(")


def copies_source(text: str, hits) -> bool:
    """True when a model reply pastes a snippet instead of answering.

    Stating a date and a place shares several words with the fact line. That
    is still an answer. A long run of the source, or the whole snippet, is a paste.
    """
    sources = _sources_of(hits)
    if verbatim_overlap(text, sources, window=12):
        return True
    folded = " ".join((text or "").split())
    if folded.startswith("…"):
        return True
    runs = word_runs(folded)
    for source in sources:
        src = " ".join((source or "").split())
        if not src or not folded:
            continue
        if folded.casefold() == src.casefold():
            return True
        if len(runs) >= 12 and folded.casefold() in src.casefold():
            return True
    return False


def rephrase_request(question: str) -> str:
    """One follow-up when the first reply copied the notes."""
    base = (question or "").strip()
    follow = (
        "Rephrase your previous reply in your own words. "
        "Do not quote the context. If you cannot, say you don't know."
    )
    return f"{base}\n\n{follow}" if base else follow


def _raw_snippet(text: str, sources) -> bool:
    if verbatim_overlap(text, sources):
        return True
    folded = " ".join((text or "").split())
    if folded.startswith("…"):
        return True
    for source in sources:
        src = " ".join((source or "").split())
        if not src:
            continue
        if folded and (
            folded.casefold() == src.casefold()
            or (len(word_runs(folded)) >= 4 and folded.casefold() in src.casefold())
        ):
            return True
        if len(src) > 40 and src[:40] in (text or ""):
            return True
    return False


def _hits_relevant(question: str, hits) -> bool:
    from retrieve import passes_relevance

    return any(passes_relevance(question, hit) for hit in hits or [])


def _acceptable(text: str, question: str, hits, sources) -> bool:
    if not text or _raw_snippet(text, sources):
        return False
    if _question_overlap(text, question) >= 1:
        return True
    from retrieve import keyword_score, passes_relevance, tokens_for

    words = set(tokens_for(text))
    for hit in hits or []:
        if not passes_relevance(question, hit):
            continue
        if len(words & set(tokens_for(getattr(hit, "text", "") or ""))) >= 2:
            return True
        if keyword_score(question, hit) >= 4 and len(words & set(tokens_for(getattr(hit, "text", "") or ""))) >= 1:
            return True
    return False


def _clean_model_text(body: str, hits) -> str:
    """Redact and trim a model reply. Words the model wrote stay in order."""
    return limit_sentences(strip_source_labels(redact_pii(strip_author_names(body or "", hits)), hits), 3)


def present_answer(question: str, body: str, hits, runtime, *, limit: int | None = None) -> str:
    """Filter a model reply. An irrelevant or failed reply becomes the refusal.

    Model words are not deleted or swapped. A reply that still copies a snippet
    becomes the don't-know line; the caller re-asks the model once before that.
    The offline note dump is paraphrased only when a snippet matches.
    """
    sources = _sources_of(hits)
    raw = body or ""
    offline = "here are the notes" in raw.casefold()
    if offline:
        text = synthesize(question, hits) if _hits_relevant(question, hits) else ""
        text = strip_source_labels(text, hits)
        if not _acceptable(text, question, hits, sources):
            text = unknown_reply(runtime)
            footer = ""
        else:
            footer = citation_line(hits) if getattr(runtime, "cite_sources", True) else ""
    else:
        text = _clean_model_text(raw, hits)
        if not text or copies_source(text, hits) or copies_source(raw, hits):
            text = unknown_reply(runtime)
            footer = ""
        else:
            footer = citation_line(hits) if getattr(runtime, "cite_sources", True) else ""
    from qa import fit_answer

    return fit_answer(text, footer, limit)
