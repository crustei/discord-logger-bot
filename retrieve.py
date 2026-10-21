"""Small, explicit retrieval. FTS5 top-k, then a hard snippet cap.

The model does not search. ``select_context`` builds the only text it is allowed
to see, and it keeps at most ``max_context_snippets()`` snippets.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from db import get_meta, is_opted_out, replace_docs, set_meta
from permissions import message_in_scope

# FTS window, then a re-rank. The context cap is what the model sees.
CANDIDATE_POOL = 24
CANDIDATE_LIMIT = 12
SNIPPET_CHARS = 420
# Room for about a dozen short fact lines, not three clipped windows.
CONTEXT_CHAR_BUDGET = 6000
# One exact keyword in the title or the line. A stemmed-only overlap stays below this.
RELEVANCE_MIN = 2
# Bump when chunking changes so an existing index is rebuilt.
CHUNK_VERSION = "line-facts-1"

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "when", "where", "who",
    "why", "how", "do", "does", "did", "we", "our", "to", "of", "for", "in", "on",
    "at", "and", "or", "it", "this", "that", "with", "about", "from", "be", "can",
    "i", "you", "me", "my", "please", "tell", "club", "any", "there",
}

DOC_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_DOC_BYTES = 200_000


@dataclass
class Hit:
    kind: str
    text: str
    channel_id: str | None = None
    channel_name: str | None = None
    guild_id: str | None = None
    author_id: str | None = None
    author: str | None = None
    timestamp: str | None = None
    message_id: str | None = None
    path: str | None = None
    title: str | None = None
    modified: str | None = None
    source: str | None = None
    visibility: str | None = None
    score: float = 0.0

    def label(self) -> str:
        if self.kind == "doc":
            title = self.title or "note"
            when = day_of(self.modified)
            if when != "unknown-date":
                return f"{title} · {when}"
            return title
        day = day_of(self.timestamp)
        who = f" · {self.author}" if self.author else ""
        name = self.channel_name or "unknown-channel"
        return f"#{name} · {day}{who}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "guild_id": self.guild_id,
            "author_id": self.author_id,
            "author": self.author,
            "timestamp": self.timestamp,
            "message_id": self.message_id,
            "path": self.path,
            "title": self.title,
            "modified": self.modified,
            "source": self.source,
            "visibility": self.visibility,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, item: dict) -> "Hit":
        return cls(
            kind=item.get("kind") or "message",
            text=item.get("text") or "",
            channel_id=item.get("channel_id"),
            channel_name=item.get("channel_name"),
            guild_id=item.get("guild_id"),
            author_id=item.get("author_id"),
            author=item.get("author"),
            timestamp=item.get("timestamp"),
            message_id=item.get("message_id"),
            path=item.get("path"),
            title=item.get("title"),
            modified=item.get("modified"),
            source=item.get("source"),
            visibility=item.get("visibility"),
            score=float(item.get("score") or 0),
        )


@dataclass
class Retrieval:
    own: list[Hit]
    others: list[Hit]
    docs: list[Hit]


def day_of(timestamp: str | None) -> str:
    if not timestamp:
        return "unknown-date"
    match = re.match(r"(\d{4}-\d{2}-\d{2})", timestamp)
    return match.group(1) if match else timestamp[:10]


def question_without_excluded_names(question: str, excluded) -> str:
    """Remove blocklisted channel names from the search text.

    A question that names a blocklisted channel must not treat pieces of that
    name as search terms. Porter stemming would otherwise match related words
    in some other channel. The blocklisted notes are already omitted.
    """
    text = question or ""
    for name in excluded:
        raw = (name or "").strip().lstrip("#")
        if len(raw) < 3 or raw.lower() not in text.lower():
            continue
        text = re.sub(re.escape(raw), " ", text, flags=re.IGNORECASE)
    return text


def tokens_for(question: str) -> list[str]:
    raw: list[str] = []
    for token in re.findall(r"[0-9A-Za-z]{2,}", question.lower()):
        if token not in raw:
            raw.append(token)
    useful = [token for token in raw if token not in STOPWORDS]
    return (useful or raw)[:12]


def _contains_word(token: str, text: str) -> bool:
    if not token or not text:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text.casefold()) is not None


def keyword_score(question: str, hit: Hit) -> int:
    """Exact words count more than a stem. A short line that says the word ranks higher."""
    tokens = tokens_for(question)
    if not tokens:
        return 0
    title = getattr(hit, "title", None) or ""
    text = getattr(hit, "text", None) or ""
    score = 0
    exact_text = False
    for token in tokens:
        stem = token[:-1] if len(token) > 3 and token.endswith("s") else ""
        longer = token + "s" if len(token) > 2 and not token.endswith("s") else ""
        if _contains_word(token, title):
            score += 4
        elif stem and _contains_word(stem, title):
            score += 1
        if _contains_word(token, text):
            score += 2
            exact_text = True
        elif stem and _contains_word(stem, text):
            score += 1
        elif longer and _contains_word(longer, text):
            score += 1
    if exact_text and len(text) <= 160:
        score += 2
    return score


_TOPIC_PREFIX = re.compile(r"^(?:[-*•+]\s+)?(.+?)\s*:\s+\S")


def topic_prefix(text: str) -> str:
    """The name before the first colon on a fact line, such as ``Name: details``."""
    match = _TOPIC_PREFIX.match((text or "").strip())
    if not match:
        return ""
    return " ".join(match.group(1).split())


def partial_topic_match(question: str, hit: Hit) -> bool:
    """True when the question is a partial name of a ``Topic:`` line.

    ``Compton fair`` matches ``Compton Air and Space Fair:`` because those
    words appear in that order, with other name words allowed in between.
    A note that only says one of the words does not. The file title is not
    the topic, so ``Key facts`` does not match every line in that file.
    """
    topic_words = tokens_for(topic_prefix(getattr(hit, "text", "") or ""))
    if len(topic_words) < 2:
        return False
    needed = tokens_for(question)
    if len(needed) < 2:
        return False
    index = 0
    for word in topic_words:
        if index < len(needed) and word == needed[index]:
            index += 1
    return index == len(needed)


def passes_relevance(question: str, hit: Hit) -> bool:
    """A partial topic name stays even when a single exact word would not."""
    if partial_topic_match(question, hit):
        return True
    return keyword_score(question, hit) >= RELEVANCE_MIN


def match_expr(question: str, columns: tuple[str, ...]) -> str | None:
    tokens = tokens_for(question)
    if not tokens:
        return None
    parts: list[str] = []
    for token in tokens:
        quoted = '"' + token.replace('"', "") + '"'
        for column in columns:
            parts.append(f"{{{column}}} : {quoted}")
    return " OR ".join(parts)


def _snap_span(content: str, start: int, end: int) -> tuple[int, int]:
    """Move a character window onto whitespace so it never keeps half a word."""
    length = len(content)
    start = max(0, min(start, length))
    end = max(start, min(end, length))
    if start > 0 and not content[start - 1].isspace():
        word_start = start
        while word_start > 0 and not content[word_start - 1].isspace():
            word_start -= 1
        if start - word_start <= 24:
            start = word_start
        else:
            while start < end and not content[start].isspace():
                start += 1
    while start < end and content[start].isspace():
        start += 1
    if end < length and end > start and not content[end - 1].isspace():
        while end > start and not content[end - 1].isspace():
            end -= 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def _clip_words(text: str, limit: int) -> str:
    """Shorten on a space. A cut that would leave a partial word is dropped."""
    if len(text) <= limit:
        return " ".join(text.split())
    clipped = text[:limit]
    if " " not in clipped:
        return ""
    clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return (" ".join(clipped.split()) + "…") if clipped else ""


def snippet(content: str, question: str, size: int = SNIPPET_CHARS) -> str:
    """A window of whole words. A note that already fits is returned intact."""
    content = content or ""
    if len(content) <= size:
        return " ".join(content.split())
    tokens = tokens_for(question)
    lower = content.lower()
    indexes = [lower.find(token) for token in tokens if token in lower]
    index = min(indexes) if indexes else 0
    start = max(0, index - 40)
    end = min(len(content), start + size)
    start, end = _snap_span(content, start, end)
    text = " ".join(content[start:end].split())
    if not text:
        return ""
    if start > 0:
        text = "…" + text
    if end < len(content):
        text = text + "…"
    return text


def allowed_channel_ids(conn, scope: set[str], excluded) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for row in conn.execute("SELECT DISTINCT channel_id, channel_name, guild_id FROM messages"):
        channel_id = row["channel_id"]
        if not channel_id or channel_id in seen:
            continue
        if message_in_scope(
            channel_id,
            row["channel_name"],
            scope=scope,
            excluded=excluded,
            guild_id=row["guild_id"],
        ):
            seen.add(channel_id)
            found.append(channel_id)
    return found


def _channel_guilds(conn) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for row in conn.execute("SELECT channel_id, guild_id FROM messages WHERE channel_id IS NOT NULL"):
        found[row["channel_id"]] = row["guild_id"]
    return found


def search_messages(
    conn,
    question: str,
    *,
    scope: set[str],
    excluded,
    asker_id: str,
    author_mode: str,
    limit: int = CANDIDATE_POOL,
    match_question: str | None = None,
    allow=None,
) -> list[Hit]:
    """FTS5 candidates inside the allowed channels. ``author_mode`` is ``only`` or ``exclude``.

    ``allow`` drops hits before the snippet cap. When it rejects part of the
    first pool, a wider pool is fetched so a higher tier cannot crowd out a
    note the asker is allowed to see.
    """

    def collect(pool: int) -> list[Hit]:
        if not asker_id:
            return []
        expr = match_expr(match_question or question, ("content",))
        if not expr:
            return []
        allowed = allowed_channel_ids(conn, scope, excluded)
        if not allowed:
            return []
        guilds = _channel_guilds(conn)
        placeholders = ",".join("?" * len(allowed))
        sql = (
            "SELECT message_id, channel_id, channel_name, author_id, author_name, display_name, "
            "timestamp, content, bm25(messages_fts) AS score "
            "FROM messages_fts WHERE messages_fts MATCH ? "
            f"AND channel_id IN ({placeholders}) "
        )
        params: list = [expr, *allowed]
        if author_mode == "only":
            sql += "AND author_id = ? "
            params.append(str(asker_id))
        else:
            sql += "AND author_id IS NOT NULL AND author_id != ? "
            params.append(str(asker_id))
        sql += "AND IFNULL(author_id, '') NOT IN (SELECT user_id FROM opt_outs) "
        sql += "ORDER BY score LIMIT ?"
        params.append(int(pool))
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        hits: list[Hit] = []
        for row in rows:
            # Defense in depth: the SQL channel list is already filtered.
            guild_id = guilds.get(row["channel_id"])
            if not message_in_scope(
                row["channel_id"],
                row["channel_name"],
                scope=scope,
                excluded=excluded,
                guild_id=guild_id,
            ):
                continue
            if author_mode == "only" and str(row["author_id"]) != str(asker_id):
                continue
            if author_mode == "exclude" and str(row["author_id"]) == str(asker_id):
                continue
            if is_opted_out(conn, row["author_id"]):
                continue
            hits.append(
                Hit(
                    kind="message",
                    text=row["content"] or "",
                    channel_id=row["channel_id"],
                    channel_name=row["channel_name"],
                    guild_id=guild_id,
                    author_id=row["author_id"],
                    author=row["display_name"] or row["author_name"],
                    timestamp=row["timestamp"],
                    message_id=row["message_id"],
                    score=float(row["score"] or 0),
                )
            )
        return hits

    hits = collect(limit)
    if allow is not None:
        kept = [hit for hit in hits if allow(hit)]
        if len(kept) != len(hits):
            wider = max(int(limit) * 4, int(limit))
            kept = [hit for hit in collect(wider) if allow(hit)]
        hits = kept
    return _show_snippets(_keep_best(hits, question), question)


def expanded_question(question: str, llm=None) -> str:
    """Original question plus light synonyms, and model keywords when configured."""
    from answerstyle import expand_terms

    pieces = [question or ""]
    pieces.extend(expand_terms(question or ""))
    if llm is not None and getattr(llm, "name", "") == "openai":
        try:
            extra = llm.expand_query(question or "") or ""
        except Exception:
            extra = ""
        if extra:
            pieces.append(extra)
    return " ".join(piece for piece in pieces if piece)


def relevance_of(hit: Hit) -> float:
    score = float(hit.score or 0)
    return -score if score < 0 else score


def recency_of(hit: Hit) -> str:
    return hit.modified or hit.timestamp or ""


_DEFINITION = re.compile(
    r"(?i)^(?:what|who)(?:\s+(?:is|are)|'s)\s+(?:(?:the|an|a)\s+)?(.+?)\s*\??$"
)


def definition_subject(question: str) -> str:
    """The X in ``What is X`` / ``Who is X``. Empty for every other question."""
    text = " ".join((question or "").strip().split())
    match = _DEFINITION.match(text)
    if not match:
        return ""
    subject = re.sub(r"\s+", " ", match.group(1).strip(" .?!\"'"))
    if len(subject) < 2:
        return ""
    return subject


def definition_rank(question: str, hit: Hit) -> int:
    """1 when the title is X, or the chunk starts with ``X:`` / ``X -``.

    A note that only mentions X stays at 0 so the definition sorts first.
    """
    subject = definition_subject(question)
    if not subject:
        return 0
    folded = subject.casefold()
    title = " ".join((hit.title or "").split()).casefold()
    if title == folded:
        return 1
    text = (hit.text or "").strip()
    text = re.sub(r"^[*_>\s]+", "", text)
    text = re.sub(r"^[-*•+]\s+", "", text)
    bold = re.match(r"^\*\*(.+?)\*\*\s*[:\-—–]", text)
    if bold and " ".join(bold.group(1).split()).casefold() == folded:
        return 1
    if re.match(rf"{re.escape(folded)}\s*[:\-—–](?:\s|$)", text.casefold()):
        return 1
    return 0


def is_facts_hit(hit: Hit) -> bool:
    path = (hit.path or "").replace("\\", "/").casefold()
    name = path.rsplit("/", 1)[-1]
    title = (hit.title or "").strip().casefold()
    return name in {"facts.md", "facts.txt", "facts.markdown"} or title in {"facts", "key facts"}


def _sort_key(question: str, hit: Hit, *, facts: bool) -> tuple:
    named = 1 if partial_topic_match(question, hit) else 0
    return (
        max(definition_rank(question, hit), named),
        1 if named and facts and is_facts_hit(hit) else 0,
        keyword_score(question, hit),
        1 if facts and is_facts_hit(hit) else 0,
        relevance_of(hit),
        recency_of(hit),
    )


def order_docs(docs: list[Hit], question: str = "") -> list[Hit]:
    """A partial topic name or a What/Who definition, then keyword matches, then facts.md."""
    return sorted(
        docs,
        key=lambda hit: _sort_key(question, hit, facts=True),
        reverse=True,
    )


def order_messages(messages: list[Hit], question: str = "") -> list[Hit]:
    """Keyword matches, then FTS strength and recency. Own rows are not placed first."""
    return sorted(
        messages,
        key=lambda hit: _sort_key(question, hit, facts=False),
        reverse=True,
    )


def merge_hits(docs: list[Hit], messages: list[Hit], *, prefer_docs: bool, question: str = "") -> list[Hit]:
    """Docs and messages in one list. General questions put knowledge files first.

    For ``What is X`` / ``Who is X``, a chunk whose title or topic prefix is X
    moves ahead of notes that only mention X. ``When is the Compton fair?``
    likewise prefers the line that starts ``Compton Air and Space Fair:``.
    """
    ordered_docs = order_docs([hit for hit in docs if (hit.text or "").strip()], question)
    ordered_messages = order_messages([hit for hit in messages if (hit.text or "").strip()], question)
    merged = ordered_docs + ordered_messages if prefer_docs else ordered_messages + ordered_docs
    merged = sorted(
        merged,
        key=lambda hit: max(
            definition_rank(question, hit),
            1 if partial_topic_match(question, hit) else 0,
        ),
        reverse=True,
    )
    return merged


def _keep_best(hits: list[Hit], question: str) -> list[Hit]:
    kept = [hit for hit in hits if passes_relevance(question, hit)]
    kept.sort(key=lambda hit: _sort_key(question, hit, facts=True), reverse=True)
    return kept[:CANDIDATE_LIMIT]


def _show_snippets(hits: list[Hit], question: str) -> list[Hit]:
    """Clip after ranking so a window cannot hide the topic name from the gate."""
    shown: list[Hit] = []
    for hit in hits:
        text = snippet(hit.text or "", question)
        if text:
            shown.append(replace(hit, text=text))
    return shown


def retrieve(
    conn,
    question: str,
    *,
    asker_id: str,
    scope: set[str],
    excluded,
    docs_dir: str,
    llm=None,
    allow=None,
) -> Retrieval:
    question = question_without_excluded_names(question, excluded)
    match_question = expanded_question(question, llm)
    own = search_messages(
        conn,
        question,
        scope=scope,
        excluded=excluded,
        asker_id=asker_id,
        author_mode="only",
        match_question=match_question,
        allow=allow,
    )
    others = search_messages(
        conn,
        question,
        scope=scope,
        excluded=excluded,
        asker_id=asker_id,
        author_mode="exclude",
        match_question=match_question,
        allow=allow,
    )
    docs = search_docs(conn, question, docs_dir, match_question=match_question, allow=allow)
    return Retrieval(own=own, others=others, docs=docs)


def format_chunk(index: int, hit: Hit) -> str:
    return f"[{index}] {hit.label()}\n{hit.text.strip()}"


def select_context(hits: list[Hit], *, limit: int | None = None) -> tuple[list[Hit], str]:
    """The exact block the model sees. Stops at the snippet cap and the char budget."""
    if limit is None:
        from voice import get_runtime

        limit = get_runtime().max_context_snippets
    cap = limit
    kept: list[Hit] = []
    chunks: list[str] = []
    used = 0
    for hit in hits:
        text = (hit.text or "").strip()
        if not text:
            continue
        if len(kept) >= cap:
            break
        chunk = format_chunk(len(kept) + 1, hit)
        if kept and used + len(chunk) + 1 > CONTEXT_CHAR_BUDGET:
            break
        if not kept and len(chunk) > CONTEXT_CHAR_BUDGET:
            text = _clip_words(text, max(40, CONTEXT_CHAR_BUDGET // 2))
            if not text:
                continue
            hit = replace(hit, text=text)
            chunk = format_chunk(1, hit)
        kept.append(hit)
        chunks.append(chunk)
        used += len(chunk) + 1
    return kept, "\n\n".join(chunks).strip()


def iter_doc_files(docs_dir: str) -> list[Path]:
    root = Path(docs_dir)
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    found: list[Path] = []
    for path in sorted(resolved_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in DOC_SUFFIXES:
            continue
        if path.name.lower() == "readme.md":
            continue
        if any(part.startswith(".") for part in path.relative_to(resolved_root).parts):
            continue
        try:
            if path.stat().st_size > MAX_DOC_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def docs_signature(docs_dir: str) -> str:
    root = Path(docs_dir)
    if not root.is_dir():
        return "missing"
    parts: list[str] = []
    resolved = root.resolve()
    for path in iter_doc_files(docs_dir):
        stat = path.stat()
        parts.append(f"{path.relative_to(resolved)}:{stat.st_mtime_ns}:{stat.st_size}")
    payload = CHUNK_VERSION + "\n" + "\n".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def _display_path(docs_dir: str, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        root = Path(docs_dir).resolve()
        return f"{root.name}/{path.resolve().relative_to(root).as_posix()}"


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Read a leading ``---`` block. Keys are lower-case. No nested tables."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text or ""
    meta: dict[str, str] = {}
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold()
        if key:
            meta[key] = value.strip().strip('"').strip("'")
    if end is None:
        return {}, text or ""
    return meta, "\n".join(lines[end + 1 :])


def _as_of_date(text: str) -> str:
    dates = re.findall(r"\bas of\s+(\d{4}-\d{2}-\d{2})\b", text or "", flags=re.IGNORECASE)
    return max(dates) if dates else ""


_BULLET = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+(.*\S)\s*$")


def _bullet_text(line: str) -> str:
    match = _BULLET.match(line.strip())
    return (match.group(1) if match else line).strip()


def _mostly_bullets(body: str) -> bool:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    bullets = sum(1 for line in lines if _BULLET.match(line))
    return bullets >= 2 and bullets * 2 >= len(lines)


def line_chunk_file(path: Path, title: str, body: str) -> bool:
    """facts.md and bullet lists are one searchable line per fact."""
    name = path.name.casefold()
    if name in {"facts.md", "facts.txt", "facts.markdown"}:
        return True
    if (title or "").strip().casefold() in {"facts", "key facts"}:
        return True
    return _mostly_bullets(body)


def _sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    title = ""
    buf: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("#"):
            sections.append((title, buf))
            title = line.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    sections.append((title, buf))
    return sections


def _chunks(text: str, *, per_line: bool = False) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    for heading, lines in _sections(text):
        if per_line:
            for line in lines:
                fact = _bullet_text(line)
                if fact:
                    chunks.append((heading, fact))
            continue
        body = "\n".join(lines).strip()
        if not body:
            continue
        paragraphs = re.split(r"\n\s*\n", body)
        current = ""
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            bullet_lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if len(bullet_lines) >= 2 and all(_BULLET.match(line) for line in bullet_lines):
                if current:
                    chunks.append((heading, current))
                    current = ""
                for line in bullet_lines:
                    chunks.append((heading, _bullet_text(line)))
                continue
            if current and len(current) + len(paragraph) + 2 > 800:
                chunks.append((heading, current))
                current = paragraph
            else:
                current = f"{current}\n\n{paragraph}".strip()
        if current:
            chunks.append((heading, current))
    return chunks


def refresh_docs(conn, docs_dir: str) -> None:
    signature = docs_signature(docs_dir)
    if get_meta(conn, "docs_signature") == signature:
        return
    rows: list[tuple] = []
    for path in iter_doc_files(docs_dir):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = split_front_matter(raw)
        display = _display_path(docs_dir, path)
        file_title = meta.get("title") or ""
        modified = meta.get("modified") or _as_of_date(body)
        source = meta.get("source") or ""
        # ``tier`` replaces the older visibility line. The column still holds
        # whichever string the file set, and the answer path resolves it.
        visibility = meta.get("tier") or meta.get("visibility") or ""
        for heading, chunk in _chunks(body, per_line=line_chunk_file(path, file_title, body)):
            title = file_title or heading or path.stem
            rows.append((display, title, chunk, modified, source, visibility))
    replace_docs(conn, rows)
    set_meta(conn, "docs_signature", signature)
    conn.commit()


def search_docs(
    conn,
    question: str,
    docs_dir: str,
    limit: int = CANDIDATE_POOL,
    match_question: str | None = None,
    allow=None,
) -> list[Hit]:
    """Knowledge-file hits. ``allow`` works the same way as in ``search_messages``."""

    def collect(pool: int) -> list[Hit]:
        if not docs_dir or not os.path.isdir(docs_dir):
            return []
        refresh_docs(conn, docs_dir)
        expr = match_expr(match_question or question, ("content", "title"))
        if not expr:
            return []
        try:
            rows = conn.execute(
                """SELECT path, title, content, modified, source, visibility,
                          bm25(docs_fts, 6.0, 1.0) AS score
                   FROM docs_fts WHERE docs_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (expr, int(pool)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        hits: list[Hit] = []
        for row in rows:
            hits.append(
                Hit(
                    kind="doc",
                    text=row["content"] or "",
                    path=row["path"],
                    title=row["title"],
                    modified=row["modified"] or None,
                    source=row["source"] or None,
                    visibility=row["visibility"] or None,
                    score=float(row["score"] or 0),
                )
            )
        return hits

    hits = collect(limit)
    if allow is not None:
        kept = [hit for hit in hits if allow(hit)]
        if len(kept) != len(hits):
            wider = max(int(limit) * 4, int(limit))
            kept = [hit for hit in collect(wider) if allow(hit)]
        hits = kept
    return _show_snippets(_keep_best(hits, question), question)
