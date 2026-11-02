"""Pre-LLM routing and curated fact-page ranking.

Reply text, the bot name, the organization name, the domain-term list, boost
pages, and the typo map all come from settings. This module does not call a
model.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from retrieve import Hit, tokens_for

MATCH_MIN = float(os.environ.get("MATCH_MIN", "0.4"))

# Functional search synonyms (not organization-specific). Typo aliases from
# settings are merged on top, for example cheif -> chief.
SYNONYMS = {
    "heavy": "mass",
    "weigh": "mass",
    "weight": "mass",
    "big": "size",
    "tall": "length",
    "long": "length",
}

GENERIC_BASE = {
    "who", "what", "when", "where", "why", "how", "is", "the", "does", "do", "did", "tell",
    "about", "me", "can", "you", "current", "currently", "which", "are", "there", "any", "of", "in",
}
_FIND_WORDS_BASE = {
    "where", "how", "can", "do", "i", "find", "get", "learn", "look", "up", "info", "information",
    "details", "more", "about", "on", "the", "club", "team", "any", "some", "out", "a", "to", "we",
}
TIER_DIRS = {
    "non-member": ["public"],
    "public": ["public"],
    "member": ["public", "member"],
    "officer": ["public", "member", "officer"],
    "admin": ["public", "member", "officer"],
}

_FIXED_ROUTES = {
    "greeting", "identity", "sensitive", "find_info", "self", "whoami",
    "private", "offtopic", "optout_list",
}

OPTOUT_LIST = re.compile(r"\bopt.?out\s+list\b|\bwho\b.{0,20}\bopted?.?out\b", re.I)
_FIND_INFO_BASE = (
    r"\b(where|how)\b.{0,20}\b(find|get|learn|look up)\b.{0,30}\b(info|information|details|more|about)\b"
)
GREETING = re.compile(r"^\s*(hi|hello|hey|yo|sup|hola|good (morning|afternoon|evening))[\s!.?]*$", re.I)
IDENTITY = re.compile(
    r"\b(who|what)\s+(are|r)\s+(you|u)\b|\bwhat (ai|model|llm|bot) are you\b|\bwho (made|built|created|runs) you\b"
    r"|\bcorporate overlord\b|^\s*who is this\??\s*$|\bare you (chatgpt|gemini|an ai|a bot|gpt)\b",
    re.I,
)
SENSITIVE = re.compile(
    r"\b(phone( number)?|home address|address of|password|wi-?fi|e-?mail( address)?|grades?|gpa|sat score|weigh|"
    r"secret|girlfriend|boyfriend|bank|ssn|social security|private channel|officer channel|reviewer channel)\b",
    re.I,
)
SCHEDULE = re.compile(
    r"\b(when|date|time|schedule|meeting|gm|deadline|next|launch|symposium|call time|today|tomorrow)\b",
    re.I,
)
PEOPLE = re.compile(r"\b(who|lead|leads|president|vice|chief|officer|captain|member|contact|talk to)\b", re.I)
TECH = re.compile(
    r"\b(engine|battery|apogee|thrust|diameter|fuel|propellant|igniter|valve|pcb|daq|avionics|payload|recovery|"
    r"airframe|injector|cold ?flow|static fire|sop|cad|firmware|flight computer|rocket)\b",
    re.I,
)
_SELF_BASE = (
    r"\b(what'?s|what is|whats)\s+(your|ur)\s+(name|purpose|job)\b|\bwhat (can|do) (you|u) do\b|\bwhat are you( for)?\b"
    r"|\bwhere are (you|u)\b|\b(are|r) (you|u) (real|human|alive|sentient)\b"
    r"|\bdo (you|u) (like|love|hate|know)\b|\bwhere is your source code\b|\bsell me this bot\b"
    r"|\bwhy (are|r) (you|u)\b|\bwhat is your opinion\b|\bwho (made|built|created|coded) (you|u|this bot)\b"
)
WHOAMI = re.compile(
    r"^\W*(who am i|what is my name|whats my name|what's my name|what is my purpose|what is my role)\W*$",
    re.I,
)
PRIVATE = re.compile(
    r"\bwhere (does|do|did) \S+(\s\S+)?\s(live|stay|sleep)\b|\blinked ?in\b|\b(phone|cell|number|address|instagram|insta|snap(chat)?)\b.*\bof\b"
    r"|\bwhat car\b|\bdox+\b|\bhome town\b|\bwhere\b.*\b(lives?|house|apartment|dorm)\b|\b(most|least) attractive\b"
    r"|\bwhat internship\b|\bis \w+ (working for|a spy)\b|\bpersonal details\b",
    re.I,
)
OFFTOPIC = re.compile(
    r"^[\s\d+\-*/x^().=?]+$|\b\d+\s*[-+*/x]\s*\d+\b|\bcapital of\b|\bcolou?r is the sky\b|\bwhat day is it|today'?s (day|date)\b|\bignore (all )?(previous|prior)\b"
    r"|\bthis statement is false\b|\bdivide .* by (0|zero)\b|\bbest club\b|\bsushi or ramen\b|\bmillion trillion\b|\bpresident of\b"
    r"|\bwordle\b|\bmeaning of life\b|\btell (me )?a joke\b|\bbad bot\b|\bgood bot\b|^\s*(i )?don.?t know\b|\bfrom now on\b",
    re.I,
)
ALWAYS_OFF = re.compile(
    r"\bsushi or ramen\b|\bsquib or a whale\b|^\W*\S{25,}\W*$|\bhablas\b|\bwho is the dictator\b|\bget palantir\b|\bdestroy\b|\bbe destroyed\b",
    re.I,
)
MEETING_Q = re.compile(r"\b(meet|meets|meeting|meetings|gm|general meeting)\b", re.I)
ONE_DATE_Q = re.compile(r"\b(next|date|today|tomorrow|tonight|this week|cancel+ed|20\d\d)\b", re.I)


def _cfg():
    from voice import get_runtime

    return get_runtime()


def _name_pattern(bot_name: str) -> str:
    """Turn a display name into a flexible match, so "Example Bot" matches "example bot"."""
    parts = re.findall(r"[A-Za-z0-9]+", bot_name or "")
    if not parts:
        return ""
    return r"\s*".join(re.escape(part) for part in parts)


def mentions_self(question: str, bot_name: str) -> bool:
    if re.search(r"\b(you|u|bot)\b", question or "", re.I):
        return True
    pattern = _name_pattern(bot_name)
    return bool(pattern and re.search(rf"\b{pattern}\b", question or "", re.I))


def _org_short() -> str:
    try:
        return (_cfg().org_short_name or "").casefold().strip()
    except Exception:
        return ""


def generic_words() -> set[str]:
    extra = set()
    short = _org_short()
    if short:
        extra.add(short)
    return GENERIC_BASE | extra


def _find_words() -> set[str]:
    extra = set()
    short = _org_short()
    if short:
        extra.add(short)
    return _FIND_WORDS_BASE | extra


def _aliases() -> dict[str, str]:
    merged = dict(SYNONYMS)
    try:
        merged.update({str(k).casefold(): str(v).casefold() for k, v in (_cfg().typo_aliases or {}).items()})
    except Exception:
        pass
    return merged


def _domain_re() -> re.Pattern:
    terms: list[str] = []
    try:
        terms.extend(str(term).strip() for term in (_cfg().domain_terms or []) if str(term).strip())
    except Exception:
        terms = []
    short = _org_short()
    if short:
        terms.append(re.escape(short))
    parts = []
    for term in terms:
        try:
            re.compile(term)
            parts.append(term)
        except re.error:
            parts.append(re.escape(term))
    if not parts:
        return re.compile(r"(?!)")
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


def _find_info_re() -> re.Pattern:
    extra = ""
    short = _org_short()
    if short and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", short):
        extra = rf"|\binformation (on|about) {re.escape(short)}\b"
    return re.compile(_FIND_INFO_BASE + extra, re.I)


def _self_re() -> re.Pattern:
    extra = ""
    try:
        pattern = _name_pattern(_cfg().bot_name)
    except Exception:
        pattern = ""
    if pattern:
        extra = rf"|\bwho is {pattern}\b|\bwhat is {pattern}\b|\bwho (made|built|created|coded) {pattern}\b"
    return re.compile(_SELF_BASE + extra, re.I)


def _fill(route: str) -> str:
    rt = _cfg()
    template = ""
    replies = getattr(rt, "replies", {}) or {}
    template = str(replies.get(route) or "")
    if not template:
        from voice import DEFAULT_REPLIES

        template = DEFAULT_REPLIES.get(route, "")
    return (
        template.replace("{bot_name}", getattr(rt, "bot_name", "") or "Bot")
        .replace("{org_name}", getattr(rt, "org_name", "") or "this server")
        .replace("{maintainer}", getattr(rt, "maintainer", "") or "the maintainer")
        .replace("{info_channel_hint}", getattr(rt, "info_channel_hint", "") or "the announcements channel")
        .replace("{reviewer}", getattr(rt, "officer_role", "") or "a reviewer")
    )


def classify(question: str) -> str:
    """Route a question. Mention markup is removed before any rule runs."""
    q = (question or "").strip()
    q = re.sub(r"<@[!&]?\d+>|<#\d+>", " ", q).strip()
    if GREETING.match(q):
        return "greeting"
    if WHOAMI.match(q):
        return "whoami"
    if PRIVATE.search(q):
        return "private"
    if _self_re().search(q) or re.match(r"^\W*(what|who) (are|r) (you|u)\W*$", q, re.I):
        return "self"
    if not q or ALWAYS_OFF.search(q) or (OFFTOPIC.search(q) and not _domain_re().search(q)):
        return "offtopic"
    if OPTOUT_LIST.search(q):
        return "optout_list"
    if _find_info_re().search(q):
        rest = set(re.findall(r"[a-z0-9]+", q.lower())) - generic_words() - _find_words()
        if not rest:
            return "find_info"
    if IDENTITY.search(q):
        return "identity"
    if SENSITIVE.search(q):
        return "sensitive"
    if SCHEDULE.search(q):
        return "schedule"
    if PEOPLE.search(q):
        return "people"
    if TECH.search(q):
        return "technical"
    if "?" not in q and not re.match(r"^\s*(who|what|when|where|why|how|which|is|are|can|does|do|did|list|name|tell)\b", q, re.I):
        return "remark"
    return "other"


def fixed_reply(route: str) -> str | None:
    """Built-in reply for routes that should not call the model. None means keep going."""
    if route not in _FIXED_ROUTES:
        return None
    text = _fill(route).strip()
    return text or None


def remark_reply() -> str:
    return _fill("remark").strip()


def content_tokens(question: str) -> list[str]:
    aliases = _aliases()
    return [aliases.get(t, t) for t in tokens_for(question) if t not in generic_words()]


def _hits_word(tok: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(tok)}", text) is not None


def fact_lines(question: str, docs_dir: str, tier: str, keep: int = 6) -> list[Hit]:
    """Lines from hand-written fact pages for this tier, best overlap first.

    Boost pages and the dated meeting page rank higher. Lines marked OUTDATED rank lower.
    """
    toks = content_tokens(question)
    if not toks or not docs_dir:
        return []
    try:
        rt = _cfg()
        boost = {str(p).casefold() for p in (rt.boost_pages or [])}
        meeting = (rt.meeting_page or "meeting-times").casefold()
    except Exception:
        boost, meeting = set(), "meeting-times"
    scored = []
    for sub in TIER_DIRS.get(tier, ["public"]):
        folder = Path(docs_dir, sub, "docs")
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            body = path.read_text(errors="ignore")
            body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
            body = re.sub(r"^---.*?---", "", body, flags=re.S)
            for line in body.splitlines():
                s = line.strip(" -*#\t")
                if (len(s) < 25 and ":" not in s and "?" not in s) or len(s) > 400 or s.count("|") > 2:
                    continue
                low = s.casefold()
                n = sum(1 for t in toks if _hits_word(t, low))
                if not n:
                    continue
                qa_bonus = 0.25 if "?" in s else 0.0
                if MEETING_Q.search(question) and not ONE_DATE_Q.search(question):
                    if path.stem.casefold() == meeting:
                        qa_bonus += 1.0
                if n / len(toks) >= 0.5 and (
                    path.stem.casefold() in boost or (path.stem.casefold() == meeting and SCHEDULE.search(question))
                ):
                    qa_bonus += 0.5
                if "outdated" in low:
                    qa_bonus -= 0.5
                try:
                    rel = str(path.relative_to(docs_dir))
                except ValueError:
                    rel = str(path)
                scored.append((n / len(toks) + qa_bonus, min(len(s), 300), rel, s, sub))
    scored.sort(reverse=True)
    out, seen = [], set()
    for _frac, _, rel, s, sub in scored:
        if s in seen:
            continue
        seen.add(s)
        if len(out) >= keep:
            break
        out.append(Hit(
            kind="doc", text=s, path=rel, title=f"fact page {Path(rel).stem}", source="fact",
            visibility=sub, score=_frac,
        ))
    return out


def match_score(question: str, hits: list[Hit]) -> float:
    toks = content_tokens(question)
    if not toks:
        return 1.0 if re.search(r"[^\x00-\x7f]", question or "") else 0.0
    best = 0.0
    for hit in hits:
        low = ((hit.title or "") + " " + (hit.text or "")).casefold()
        best = max(best, sum(1 for t in toks if _hits_word(t, low)) / len(toks))
    return best


def label_chat(hits: list[Hit]) -> list[Hit]:
    """Chat lines carry a visible label so an answer can say where it came from."""
    from dataclasses import replace

    return [replace(h, text="[from Discord chat, unverified] " + (h.text or "")) if h.kind == "message" else h for h in hits]


def order_with_chat_last(pinned: list[Hit], hits: list[Hit], _question: str) -> list[Hit]:
    """Fact lines first, then knowledge docs, then chat.

    Chat stays in the candidate list so the snippet cap can still quote it.
    ``label_chat`` marks unverified chat when a caller wants that.
    """
    docs = [h for h in hits if h.kind != "message"]
    chat = [h for h in hits if h.kind == "message"]
    return list(pinned) + docs + chat


def log_gap(question: str, route: str, score: float, tier: str) -> None:
    path = os.environ.get("GAP_LOG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gap_log.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "q": (question or "")[:300],
                "route": route,
                "score": round(score, 2),
                "tier": tier,
            }) + "\n")
    except OSError:
        pass
