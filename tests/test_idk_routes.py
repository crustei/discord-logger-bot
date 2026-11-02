import pytest

import router
from qa import Result, _nonempty


class _RT:
    max_answer_chars = 500
    officer_role = "an officer"
    officer_channel = ""
    bot_name = "Example Bot"

    def say(self, key):
        return "I don't know."


@pytest.mark.parametrize("q,route", [
    ("whats your name?", "self"),
    ("what can you do", "self"),
    ("what are you?", "self"),
    ("who is example bot?", "self"),
    ("<@111111111111111111> who are you", "self"),
    ("who am I", "whoami"),
    ("Where does Alex live?", "private"),
    ("what is sam's linkedin", "private"),
    ("what's 2 + 2", "offtopic"),
    ("what's the capital of France", "offtopic"),
    ("what is the best club on campus", "offtopic"),
    ("when are payload meetings?", "schedule"),
    ("who is the chief engineer", "people"),
])
def test_routes(q, route):
    assert router.classify(q) == route
    assert router.fixed_reply(route) or route in ("schedule", "people")


def test_domain_math_not_offtopic():
    assert router.classify("what is the 2 + 2 config on the payload board?") != "offtopic"


@pytest.mark.parametrize("q", ["who are you", "who is jims", "", "random"])
def test_reply_never_empty(q):
    res = _nonempty(Result(kind="answer", reason="", context_block="", text="  "), q, _RT())
    assert res.text.strip()


def test_blank_self_question_gets_self_description():
    res = _nonempty(Result(kind="answer", reason="", context_block="", text=""), "who are you", _RT())
    assert "Example Bot" in res.text


def test_typo_alias_expands_to_chief():
    from answerstyle import expand_terms

    assert "chief" in expand_terms("who is the cheif engineer")


def test_fact_lines_boost_outdated_and_meeting_page(tmp_path):
    root = tmp_path / "knowledge"
    docs = root / "public" / "docs"
    docs.mkdir(parents=True)
    (docs / "lore.md").write_text(
        "Lore: the club rocket is called Aurora and it flies high above the field.\n",
        encoding="utf-8",
    )
    (docs / "other.md").write_text(
        "OUTDATED: Aurora was cancelled years ago and is not the current rocket at all.\n",
        encoding="utf-8",
    )
    (docs / "meeting-times.md").write_text(
        "General meeting: Fridays at 6 PM in the lab classroom for all members.\n",
        encoding="utf-8",
    )
    (docs / "old-gm.md").write_text(
        "General meeting used to be on Mondays at noon in a different room entirely.\n",
        encoding="utf-8",
    )
    hits = router.fact_lines("what is the aurora rocket", str(root), "non-member")
    assert hits and "OUTDATED" not in hits[0].text
    assert hits[0].path.endswith("lore.md")
    meet = router.fact_lines("when is the general meeting", str(root), "non-member")
    assert meet and "meeting-times" in meet[0].path
