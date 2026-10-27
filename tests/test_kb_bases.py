"""Three source folders -> one generated base per role; one base per question."""
import pytest

import kb_bases
from qa import handle_question, is_gap_answer
from retrieve import search_docs
from tests.conftest import committed_runtime


def _write(root, rel, title, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\nmodified: 2026-10-20\n---\n# {title}\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def kb(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    monkeypatch.setenv("KB_BUILD_DIR", str(tmp_path / "built"))
    _write(root, "public/where.md", "Where ACME meets", "ACME meets in the lab building building on Tuesdays.")
    _write(root, "member/budget.md", "Rocket budget", "The rocket budget for propulsion is 4000 dollars this year.")
    _write(root, "officer/roster.md", "Propulsion roster", "Current members of propulsion rocket team: Ada, Bo.")
    _write(root, "public/rocket.md", "Rocket overview", "The rocket is called Comet and it is a liquid rocket.")
    return root


def _paths(hits):
    return [h.path for h in hits]


def test_each_role_searches_only_its_own_base(db, kb):
    pub = search_docs(db, "rocket", str(kb), asker_tier="non-member")
    mem = search_docs(db, "rocket", str(kb), asker_tier="member")
    off = search_docs(db, "rocket members", str(kb), asker_tier="officer")
    assert all("/public/" in p for p in _paths(pub))
    assert not any("/officer/" in p for p in _paths(mem)) and any("/member/" in p for p in _paths(mem))
    assert any("/officer/" in p for p in _paths(off))
    assert not (kb.parent / "built").exists()  # bases are assembled at load time, never copied


def test_own_tier_entries_rank_first(db, kb):
    assert "/member/" in search_docs(db, "rocket", str(kb), asker_tier="member")[0].path
    assert "/officer/" in search_docs(db, "rocket members", str(kb), asker_tier="officer")[0].path


def test_executive_is_officer_and_unknown_is_public():
    assert kb_bases.base_for("executive") == "kb_officer"
    assert kb_bases.base_for("non-member") == "kb_public"


def test_generated_base_holds_no_duplicates(kb):
    built = kb_bases.build_base(str(kb), "kb_officer")
    names = sorted(p.name for p in built.rglob("*.md"))
    assert names == sorted(set(names))


class Ungrounded:
    name = "ungrounded"

    def answer(self, question, context_block):
        return "ACME meets on Mars every Friday."

    def check_grounded(self, question, answer, block):
        return False


def test_ungrounded_answer_becomes_dont_know(db, kb, tmp_path):
    runtime = committed_runtime(tmp_path)
    result = handle_question(db, "Where does ACME meet?", asker_id="1", channel_id="1", channel_name="g",
                             visible={"1"}, here={"1"}, docs_dir=str(kb), llm=Ungrounded(),
                             runtime=runtime, asker_tier="member")
    assert result.kind == "empty"
    assert "Mars" not in result.text


def test_gap_phrases_are_detected():
    assert is_gap_answer("The provided documents do not state where Aurora is.")
    assert not is_gap_answer("ACME meets on Tuesdays.")


def test_rosters_stay_out_of_non_people_questions(db, kb):
    assert not any("roster" in h.path for h in search_docs(db, "rocket", str(kb), asker_tier="officer"))


def test_local_grounding_rejects_invented_names():
    from qa import is_grounded
    ctx = "ACME orders liquid oxygen from Airgas."
    assert is_grounded("Liquid oxygen comes from Airgas.", ctx)
    assert not is_grounded("Liquid oxygen comes from Praxair via Bob Smith.", ctx)


def test_rosters_members_and_officers_yes_public_no(db, tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "member/generated/roster/propulsion-roster.md", "Propulsion roster", "Current members list: Ada, Bo.")
    _write(root, "public/where.md", "Where ACME meets", "ACME meets in the lab building.")
    for tier, sees in (("member", True), ("officer", True), ("non-member", False)):
        hits = search_docs(db, "list current members", str(root), asker_tier=tier)
        assert any("Ada" in h.text for h in hits) is sees, tier


def test_how_do_i_is_not_about_self():
    from answerstyle import about_self
    assert not about_self("How do I join a team?")
    assert about_self("What did I say about the budget?")


def test_model_outage_answers_from_kb_in_backup_mode(db, tmp_path, monkeypatch):
    from llm import FALLBACK_TAG
    monkeypatch.setenv("LLM_BACKOFF_SCALE", "0")
    root = tmp_path / "knowledge"
    _write(root, "public/where.md", "Where ACME meets", "ACME meets in the lab building on Tuesdays.")
    from llm import LLMError

    class Down:
        def answer(self, q, c):
            raise LLMError("HTTP 500")
    r = handle_question(db, "Where does ACME meet?", asker_id="1", channel_id="0", channel_name="x",
                        visible={"0"}, here={"0"}, docs_dir=str(root), llm=Down(), asker_tier="member")
    assert r.kind == "answer" and "lab" in r.text and r.text.endswith("\n" + FALLBACK_TAG)


def test_model_outage_without_a_match_is_an_error(db, tmp_path, monkeypatch):
    from llm import LLMError
    from qa import UNAVAILABLE
    root = tmp_path / "knowledge"
    monkeypatch.setenv("LLM_BACKOFF_SCALE", "0")
    _write(root, "public/where.md", "Where ACME meets", "ACME meets in the lab building on Tuesdays.")

    class Down:
        def answer(self, q, c):
            raise LLMError("HTTP 429")
    import qa
    monkeypatch.setattr(qa, "backup_answer", lambda *a, **k: "")
    r = handle_question(db, "Where does ACME meet?", asker_id="1", channel_id="0", channel_name="x",
                        visible={"0"}, here={"0"}, docs_dir=str(root), llm=Down(), asker_tier="member")
    assert r.kind == "error" and r.text == UNAVAILABLE


def test_partial_answer_is_not_a_refusal():
    from qa import _is_refusal
    from voice import get_runtime
    rt = get_runtime()
    assert not _is_refusal("Pat Lee was a previous president, but the year isn't stated.", rt, 600)
    assert not _is_refusal("Storage is at Site X. Where the shop is isn't stated.", rt, 600)
    assert _is_refusal("I don't know.", rt, 600)
    assert _is_refusal("The RSO is not stated.", rt, 600)


def test_most_relevant_entry_first():
    from retrieve import Hit, most_relevant_first
    a = Hit(kind="doc", text="Dues: none.", path="a", title="FAQ")
    b = Hit(kind="doc", text="Storage: short-term storage is at Site X.", path="b", title="FAQ")
    assert most_relevant_first([a, b], "Where is storage?")[0] is b


def test_drive_source_beats_discord_on_tie():
    from retrieve import Hit, most_relevant_first
    disc = Hit(kind="doc", text="President: Alex (as of 2026-10-22; source: https://discord.com/channels/1/2/3)", title="t")
    drive = Hit(kind="doc", text="President: Sam (as of 2026-01-01; source: https://docs.google.com/document/d/x/edit)", title="t")
    assert most_relevant_first([disc, drive], "Who is president?")[0] is drive


def test_footer_is_one_line_of_whole_links():
    from types import SimpleNamespace as N
    from answerstyle import citation_line
    hits = [N(kind="doc", text="Alex is President. (as of 2026-10-22; source: https://docs.google.com/document/d/abc/edit)", source="", title="t"),
            N(kind="doc", text="Alex is President too.", source="Discord server role list (member snapshot)", title="x")]
    line = citation_line(hits, "Alex is President.")
    assert line == "Based on: https://docs.google.com/document/d/abc/edit"
    assert citation_line(hits, "Alex is President.", budget=20) == ""


def test_chatter_note_is_dropped():
    from retrieve import Hit, is_chatter
    note = Hit(kind="doc", path="member/generated/projects/x.md", title="#chan: hi", text="As of 2026-01-01, jamie is cool",
               source="https://discord.com/channels/1/2/3")
    assert is_chatter(note)


def test_concept_synonyms_expand_heavy_to_mass():
    from answerstyle import expand_terms
    assert {"mass", "lbs"} <= set(expand_terms("how heavy is Aurora"))
    assert "apogee" in expand_terms("how high will Aurora go")


def test_model_expansion_merges_and_failure_falls_back():
    from retrieve import expanded_question

    class Ok:
        def expand_query(self, q):
            assert len(q) <= 300
            return "rocket mass specs one two three four five"

    class Boom:
        def expand_query(self, q):
            raise RuntimeError("429")

    merged = expanded_question("how heavy is Aurora " + "x" * 400, Ok())
    assert "mass" in merged and "rocket" in merged and "five" not in merged
    assert "mass" in expanded_question("how heavy is Aurora", Boom())


def test_grounding_accepts_synonym_answer():
    from qa import is_grounded
    ctx = "Aurora: Dry Mass 131 lbs Wet Mass 173 lbs"
    assert is_grounded("Aurora weighs 131 lbs dry and 173 lbs wet.", ctx, "how heavy is Aurora")


def _hits():
    from retrieve import Hit
    return [Hit(kind="doc", path=f"public/docs/f{i}.md", title=f"Aurora fact {i}", text=f"Aurora fact {i}: value {i}.",
                source="", modified=f"2026-01-{i+1:02d}") for i in range(5)]


def test_rerank_uses_model_order_and_falls_back():
    from retrieve import rerank

    seen = {}

    class Ok:
        def rerank(self, q, listing):
            assert listing.count("\n") == 4 and "| kb |" in listing
            seen["lines"] = listing.splitlines()
            return "5, 2, 99, 2"

    class Boom:
        def rerank(self, q, listing):
            raise RuntimeError("429 Too Many Requests")

    hits = _hits()
    out = rerank(hits, "Aurora fact", Ok())
    want = [seen["lines"][4].split(" | ")[3], seen["lines"][1].split(" | ")[3]]
    assert [h.title for h in out] == want
    assert len(rerank(hits, "Aurora fact", Boom())) == 5
    assert len(rerank(hits, "Aurora fact", None)) == 5


def test_persona_folds_in_answer_check():
    from voice import get_runtime
    assert "check every claim" in get_runtime().system_prompt


def test_person_questions_never_use_raw_notes():
    from retrieve import Hit, authority_filter
    raw = Hit(kind="doc", path="member/generated/general/from-what-i-recall-jamie.md",
              title="From what I recall Jamie sent you a heater to base the board requirements off of?",
              text="As of 2026-02-19, From what I recall Jamie sent you a heater to base the board requirements off of and more words here today ok",
              source="https://discord.com/channels/1/2/3")
    board = Hit(kind="doc", path="public/docs/officer-boards.md", title="ACME officer boards",
                text="Jamie Taylor: Jamie Taylor is an ACME team lead for 2026-27.", source="")
    assert authority_filter([raw, board], "who is Jamie") == [board]
    assert authority_filter([raw], "who is Jamie") == []
