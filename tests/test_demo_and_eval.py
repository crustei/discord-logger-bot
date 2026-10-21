from demo import format_report
from eval import run_cases
from llm import ExtractiveProvider


class Boom:
    def answer(self, question, context_block):
        raise AssertionError("dry-run must not call the model")


def test_dry_run_prints_three_sections_without_calling_the_model(sample_db):
    from demo import ask

    result = ask(
        sample_db,
        asker="ethan",
        channels=["general", "private-channel"],
        here="general",
        question="When did I say I would bring the purchase order form?",
        docs_dir="sample/docs",
        public=set(),
        llm=Boom(),
        dry_run=True,
        apply_sample_blocklist=True,
    )
    report = format_report(result)
    assert "## Context" in report
    assert "## Decision" in report
    assert "## Answer" in report
    assert result.kind == "answer"
    assert "purchase order form" in result.context_block
    assert "1234" not in result.context_block
    assert "length: 0" in report
    assert "LLM: skipped (--dry-run)" in report


def test_extractive_answer_stays_within_the_cap(sample_db):
    from demo import ask
    from voice import get_runtime

    result = ask(
        sample_db,
        asker="ethan",
        channels=["general"],
        here="general",
        question="standupnote",
        docs_dir="sample/docs",
        public=set(),
        llm=ExtractiveProvider(),
        dry_run=False,
    )
    assert result.kind == "answer"
    assert len(result.snippets) <= get_runtime().max_context_snippets
    assert len(result.text) <= get_runtime().max_answer_chars
    assert result.llm_called is True


def test_sample_blocklist_is_opt_in(sample_db):
    from demo import ask

    hidden = ask(
        sample_db,
        asker="ethan",
        channels=["general", "private-channel"],
        here="general",
        question="What is the budget PIN?",
        docs_dir="sample/docs",
        public=set(),
        llm=Boom(),
        dry_run=True,
        apply_sample_blocklist=True,
    )
    assert "1234" not in (hidden.context_block or "")
    visible = ask(
        sample_db,
        asker="ethan",
        channels=["general", "private-channel"],
        here="general",
        question="What is the budget PIN?",
        docs_dir="sample/docs",
        public=set(),
        llm=Boom(),
        dry_run=True,
        apply_sample_blocklist=False,
    )
    assert "1234" in (visible.context_block or "")


def test_eval_cases_pass(sample_db):
    results = run_cases(sample_db)
    failed = [item for item in results if not item.ok]
    assert not failed, "\n".join(
        f"{item.name}: {', '.join(item.problems)}" for item in failed
    )
