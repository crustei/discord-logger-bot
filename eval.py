"""Canned questions against the fictional sample log and knowledge files.

Checks scope, synthesized answers, the knowledge directory, and the length cap.
No API key. Exit status is 1 when any case fails.

    python eval.py
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from answerstyle import verbatim_overlap
from db import connect
from demo import SAMPLE_DB, SAMPLE_DOCS, ask
from llm import ExtractiveProvider, LLMError
from voice import get_runtime

class _FailingLLM:
    name = "openai"

    def answer(self, question, context_block):
        raise LLMError("HTTP 500")


class _ScriptedLLM:
    """Returns canned replies, in order. Used to prove a copied reply is re-asked."""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def answer(self, question, context_block):
        if not self._replies:
            raise LLMError("no reply left")
        return self._replies.pop(0)


ETHAN_CHANNELS = [
    "general",
    "business",
    "projects",
    "internal-announcements",
    "private-channel",
]


@dataclass
class Case:
    name: str
    asker: str
    channels: list[str]
    here: str
    question: str
    expect: str
    forbid_context: tuple[str, ...] = ()
    forbid_posted: tuple[str, ...] = ()
    require_context: tuple[str, ...] = ()
    require_posted: tuple[str, ...] = ()
    require_before: tuple[tuple[str, str], ...] = ()
    exact_snippets: int | None = None
    forbid_verbatim: bool = False
    fail_llm: bool = False
    replies: tuple[str, ...] = ()


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str
    problems: list[str] = field(default_factory=list)


CASES = [
    Case(
        name="own-form",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When did I say I would bring the purchase order form?",
        expect="answer",
        require_context=("purchase order form",),
        require_posted=("#general", "2026-10-18"),
        forbid_context=("1234", "99999", "submits the purchase order", "Static fire"),
        forbid_posted=("1234", "99999", "I will bring the purchase order form on Friday"),
        forbid_verbatim=True,
    ),
    Case(
        name="solarium-other-member",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When is the Solarium?",
        expect="answer",
        require_posted=("Solarium", "Sunday session"),
        forbid_context=("1234", "99999"),
        forbid_posted=("1234", "99999", "The Sunday session is in the Solarium"),
        forbid_verbatim=True,
    ),
    Case(
        name="private-pin",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="What is the budget PIN in private-channel?",
        expect="empty",
        forbid_context=("1234", "99999", "sponsor shortfall", "discretionary"),
        forbid_posted=("1234", "99999", "sponsor shortfall", "discretionary"),
    ),
    Case(
        name="discretionary-fund",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="How much is the discretionary fund?",
        expect="empty",
        forbid_context=("99999", "1234"),
        forbid_posted=("99999", "1234"),
    ),
    Case(
        name="riley-static-fire",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="What did Riley say about the static fire?",
        expect="pending",
        forbid_posted=("Static fire", "Saturday"),
        forbid_context=("1234", "99999"),
    ),
    Case(
        name="own-quote-other-channel",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="What vendor quote did I write for the nozzle blanks?",
        expect="pending",
        forbid_posted=("500",),
        forbid_context=("1234", "99999"),
    ),
    Case(
        name="riley-cannot-see-business",
        asker="riley",
        channels=["projects", "general"],
        here="projects",
        question="What is the vendor quote for nozzle blanks?",
        expect="empty",
        forbid_context=("500", "nozzle"),
        forbid_posted=("500", "nozzle"),
    ),
    Case(
        name="reimbursement-other-member",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="How are reimbursement receipts filed?",
        expect="answer",
        require_posted=("receipts",),
        forbid_context=("1234", "99999"),
        forbid_posted=("1234", "99999", "Reimbursement receipts go in the shared drive"),
        forbid_verbatim=True,
    ),
    Case(
        name="unknown-topic",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="What is the launch azimuth for the moonshot vehicle?",
        expect="empty",
        require_posted=("don't know",),
        forbid_context=("1234", "99999", "Static fire"),
        forbid_posted=("1234", "99999"),
    ),
    Case(
        name="snippet-cap",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="standupnote",
        expect="answer",
        exact_snippets=12,
        forbid_context=("1234", "99999", "sponsor shortfall"),
        forbid_posted=("1234", "99999"),
    ),
    Case(
        name="recruitment-from-knowledge",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When is recruitment?",
        expect="answer",
        require_context=("1 November 2026",),
        require_posted=("1 November 2026", "Key facts"),
        require_before=(("1 November 2026", "December"),),
        forbid_posted=("December", "I think recruitment is in December"),
        forbid_verbatim=True,
    ),
    Case(
        name="pii-redaction",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="How are shop questions sent?",
        expect="answer",
        require_posted=("[redacted]",),
        forbid_posted=("shop-lead@example.test",),
        forbid_verbatim=True,
    ),
    Case(
        name="gm-fact-line",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When are GMs?",
        expect="answer",
        require_context=("GMs are Fridays at 3 PM",),
        require_posted=("Friday", "3"),
        forbid_posted=("Meeting Notes", "GM Kickoff", "equipment lead"),
        forbid_verbatim=True,
    ),
    Case(
        name="no-raw-fallback",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When are the general meetings?",
        expect="empty",
        require_posted=("don't know",),
        forbid_posted=("Meeting Notes", "GM Kickoff", "equipment lead", "Solarium", "Riley is"),
        forbid_context=("Meeting Notes", "Solarium"),
    ),
    Case(
        name="llm-failure-unknown",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When are GMs?",
        expect="empty",
        require_posted=("don't know",),
        forbid_posted=("Fridays at 3 PM", "Meeting Notes", "here are the notes"),
        fail_llm=True,
    ),
    Case(
        name="garbled-output-guard",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When is the Compton Air and Space Fair?",
        expect="answer",
        require_posted=("are needed", "Compton Airport", "24 October"),
        forbid_posted=("happen on", "iscussed", "October 24, 2026"),
        forbid_verbatim=True,
        replies=(
            "Compton Air and Space Fair: October 24, 2026 at Compton Airport in LA. "
            "Volunteers are needed for this family rocketry event.",
            "Volunteers are needed for that family rocketry event. "
            "It is at Compton Airport in LA on 24 October 2026.",
        ),
    ),
    Case(
        name="compton-fair",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="When is the Compton fair?",
        expect="answer",
        require_context=("Compton Air and Space Fair", "October 24, 2026", "Woodley"),
        require_posted=("Compton", "Woodley", "24", "2026"),
        forbid_posted=("don't know",),
    ),
    Case(
        name="what-is-x-ranking",
        asker="ethan",
        channels=ETHAN_CHANNELS,
        here="general",
        question="What is TANGERInE?",
        expect="answer",
        require_context=("TANGERInE: our high-altitude rocket.", "Discussed the new schedule"),
        require_before=(("TANGERInE: our high-altitude rocket.", "TANGERInE flight"),),
        require_posted=("club high-altitude rocket",),
        forbid_posted=("happen on", "October 24"),
        forbid_verbatim=True,
        replies=("TANGERInE is the club high-altitude rocket.",),
    ),
]


def _problems(case: Case, result, cap_chars: int, cap_snippets: int) -> list[str]:
    problems = []
    if result.kind != case.expect:
        problems.append(f"decision {result.kind}, expected {case.expect}")
    if len(result.snippets) > cap_snippets:
        problems.append(f"{len(result.snippets)} snippets, cap is {cap_snippets}")
    if case.exact_snippets is not None and len(result.snippets) != case.exact_snippets:
        problems.append(f"{len(result.snippets)} snippets, expected {case.exact_snippets}")
    posted_len = 0 if result.dry_run and result.kind == "answer" else len(result.text)
    if posted_len > cap_chars:
        problems.append(f"answer length {posted_len} exceeds {cap_chars}")
    blob_context = result.context_block or ""
    blob_posted = result.text or ""
    for needle in case.forbid_context:
        if needle in blob_context:
            problems.append(f"context leaked {needle!r}")
    for needle in case.forbid_posted:
        if needle in blob_posted:
            problems.append(f"posted text leaked {needle!r}")
    for needle in case.require_context:
        if needle not in blob_context:
            problems.append(f"context missing {needle!r}")
    for needle in case.require_posted:
        if needle not in blob_posted:
            problems.append(f"answer missing {needle!r}")
    for earlier, later in case.require_before:
        left = blob_context.find(earlier)
        right = blob_context.find(later)
        if left < 0 or right < 0 or left > right:
            problems.append(f"context should mention {earlier!r} before {later!r}")
    if case.forbid_verbatim:
        sources = [hit.text for hit in result.snippets]
        if verbatim_overlap(blob_posted, sources):
            problems.append("answer repeats a long stretch of a source")
    return problems


def run_cases(conn=None) -> list[CaseResult]:
    runtime = get_runtime()
    runtime.reload_if_changed()
    own_conn = conn is None
    if conn is None:
        if not SAMPLE_DB.is_file():
            raise SystemExit("Missing sample/sample.db. Run: python sample/build_sample.py")
        temp_dir = tempfile.mkdtemp(prefix="qa-eval-")
        db_path = Path(temp_dir) / "eval.db"
        shutil.copy(SAMPLE_DB, db_path)
        conn = connect(db_path)
    llm = ExtractiveProvider()
    results: list[CaseResult] = []
    try:
        for case in CASES:
            if case.fail_llm:
                model = _FailingLLM()
            elif case.replies:
                model = _ScriptedLLM(case.replies)
            else:
                model = llm
            result = ask(
                conn,
                asker=case.asker,
                channels=case.channels,
                here=case.here,
                question=case.question,
                docs_dir=str(SAMPLE_DOCS),
                public=set(),
                llm=model,
                dry_run=False,
                apply_sample_blocklist=True,
            )
            problems = _problems(case, result, runtime.max_answer_chars, runtime.max_context_snippets)
            detail = f"{result.kind} snippets={len(result.snippets)} len={len(result.text)}"
            results.append(CaseResult(case.name, not problems, detail, problems))
        from tests.test_latency import ask_defer_problems

        defer_problems = ask_defer_problems()
        results.append(
            CaseResult(
                "ask-defers-before-db",
                not defer_problems,
                "defer" if not defer_problems else "out of order",
                defer_problems,
            )
        )
    finally:
        if own_conn:
            conn.close()
    return results


def main() -> int:
    results = run_cases()
    failed = 0
    for item in results:
        mark = "PASS" if item.ok else "FAIL"
        print(f"{mark}  {item.name:<28} {item.detail}")
        for problem in item.problems:
            print(f"      {problem}")
        if not item.ok:
            failed += 1
    passed = len(results) - failed
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
