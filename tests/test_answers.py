"""Synthesized answers, knowledge-file visibility, and doc-first ranking."""

from answerstyle import doc_is_public, redact_pii, synthesize, verbatim_overlap
from qa import handle_question
from retrieve import Hit, merge_hits
from tests.conftest import add
from voice import RuntimeConfig, set_runtime


class Recording:
    name = "recording"

    def answer(self, question, context_block):
        return "Recorded."


def _runtime(tmp_path, body: str) -> RuntimeConfig:
    root = tmp_path / "voice"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "settings.toml").write_text(body, encoding="utf-8")
    (config / "persona.md").write_text(
        "# Bot voice\n\n## System prompt\nAnswer in at most {max_answer_chars} characters.\nUse only the Context block.\n\n## Refusal\nI don't know that yet. Ask {officer_role}{officer_where}.\n",
        encoding="utf-8",
    )
    runtime = RuntimeConfig(root)
    set_runtime(runtime)
    return runtime


def test_redaction_and_verbatim_filter():
    raw = "Write to shop-lead@example.test or call 415-555-1212."
    cleaned = redact_pii(raw)
    assert "shop-lead@example.test" not in cleaned
    assert "415-555-1212" not in cleaned
    assert "[redacted]" in cleaned
    source = "Recruitment applications open on 1 November 2026."
    assert verbatim_overlap(source, [source])
    paraphrased = synthesize("When is recruitment?", [Hit(kind="doc", text=source, title="Key facts")])
    assert "1 November 2026" in paraphrased
    assert not verbatim_overlap(paraphrased, [source])


def test_facts_rank_ahead_of_the_askers_messages():
    facts = Hit(kind="doc", text="Recruitment applications open on 1 November 2026.", path="docs/facts.md", title="Key facts", score=-1)
    own = Hit(kind="message", text="I think recruitment is in December.", author_id="1001", score=-20, timestamp="2026-10-19")
    ranked = merge_hits([facts], [own], prefer_docs=True)
    assert ranked[0].kind == "doc"
    assert "November" in ranked[0].text
    assert ranked[1].text.startswith("I think")


def test_public_knowledge_answers_directly_and_beats_a_newer_message(db, tmp_path):
    runtime = _runtime(
        tmp_path,
        'docs_visibility = "public"\ncite_sources = true\nofficer_role = "an officer"\n',
    )
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\nmodified: 2026-10-21\nvisibility: public\n---\n\n"
        "Recruitment applications open on 1 November 2026.\n",
        encoding="utf-8",
    )
    add(
        db,
        id="guess",
        author_id="1001",
        content="I think recruitment is in December.",
        timestamp="2026-10-19T12:00:00+00:00",
    )
    result = handle_question(
        db,
        "When is recruitment?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
    )
    assert result.kind == "answer"
    assert result.context_block.find("1 November 2026") < result.context_block.find("December")
    assert result.snippets[0].kind == "doc"
    assert "December" not in result.text


def test_approval_visibility_holds_a_doc_unless_the_file_says_public(db, tmp_path):
    runtime = _runtime(tmp_path, 'docs_visibility = "approval"\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    note = docs / "facts.md"
    note.write_text(
        "---\ntitle: Key facts\nvisibility: approval\n---\n\nRecruitment applications open on 1 November 2026.\n",
        encoding="utf-8",
    )
    held = handle_question(
        db,
        "When is recruitment?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
    )
    assert held.kind == "pending"
    assert held.llm_called is False
    assert doc_is_public(held.snippets[0], "approval") is False

    note.write_text(
        "---\ntitle: Key facts\nvisibility: public\n---\n\nRecruitment applications open on 1 November 2026.\n",
        encoding="utf-8",
    )
    direct = handle_question(
        db,
        "When is recruitment?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
    )
    assert direct.kind == "answer"
    assert "1 November 2026" in direct.context_block


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def answer(self, question, context_block):
        self.prompts.append(question)
        if not self.replies:
            raise AssertionError("no reply left")
        return self.replies.pop(0)


def _ask(db, runtime, docs, question, llm):
    return handle_question(
        db,
        question,
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=llm,
        runtime=runtime,
    )


def test_model_reply_keeps_every_word(db, tmp_path):
    runtime = _runtime(tmp_path, 'cite_sources = false\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\nvisibility: public\n---\n\n"
        "Compton Air and Space Fair: October 24, 2026 at Compton Airport in LA. "
        "Volunteers are needed for this family rocketry event.\n",
        encoding="utf-8",
    )
    llm = Scripted(["Volunteers are needed for that family rocketry event."])
    result = _ask(db, runtime, docs, "When is the Compton Air and Space Fair?", llm)
    assert result.kind == "answer"
    assert llm.prompts == ["When is the Compton Air and Space Fair?"]
    assert result.text == "Volunteers are needed for that family rocketry event."
    assert "happen on" not in result.text
    assert "iscussed" not in result.text


def test_copied_model_reply_is_reasked_once_then_kept(db, tmp_path):
    runtime = _runtime(tmp_path, 'cite_sources = false\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\nvisibility: public\n---\n\nGMs are Fridays at 3 PM.\n",
        encoding="utf-8",
    )
    llm = Scripted(
        [
            "GMs are Fridays at 3 PM.",
            "The weekly meeting is on Friday at 3 PM.",
        ]
    )
    result = _ask(db, runtime, docs, "When are GMs?", llm)
    assert result.kind == "answer"
    assert len(llm.prompts) == 2
    assert "Rephrase" in llm.prompts[1]
    assert result.text == "The weekly meeting is on Friday at 3 PM."
    assert "happen on" not in result.text


def test_second_copy_becomes_the_dont_know_line(db, tmp_path):
    runtime = _runtime(tmp_path, 'cite_sources = false\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\nvisibility: public\n---\n\nGMs are Fridays at 3 PM.\n",
        encoding="utf-8",
    )
    llm = Scripted(["GMs are Fridays at 3 PM.", "GMs are Fridays at 3 PM."])
    result = _ask(db, runtime, docs, "When are GMs?", llm)
    assert result.kind == "empty"
    assert len(llm.prompts) == 2
    assert "don't know" in result.text
    assert "happen on" not in result.text
    assert "Fridays at 3 PM" not in result.text


def test_snippet_keeps_whole_words():
    from retrieve import snippet

    note = (
        "Discussed the new schedule, FAR, and the TANGERInE launch with the avionics "
        "team after the design review on Monday morning in the shop."
    )
    text = snippet(note, "What is TANGERInE?")
    assert text.split()[0] == "Discussed"
    prefix = "Discussed the new schedule, FAR, and the "
    long = prefix + "TANGERInE " + ("detail " * 100)
    assert len(long) > 420
    assert len(prefix) > 40
    window = snippet(long, "TANGERInE", size=80)
    body = window.replace("…", " ")
    source_words = set(long.split())
    assert body.split()
    for word in body.split():
        assert word in source_words


def test_what_is_ranks_a_topic_prefix_ahead_of_mentions():
    from retrieve import _keep_best

    defined = Hit(
        kind="doc",
        text="TANGERInE: our high-altitude rocket.",
        title="Key facts",
        path="docs/facts.md",
        score=-2,
    )
    mention = Hit(
        kind="doc",
        text="Discussed the new schedule, FAR, and the TANGERInE flight.",
        title="TANGERInE flight",
        path="docs/launch-notes.md",
        score=-0.1,
    )
    ranked = merge_hits([mention, defined], [], prefer_docs=True, question="What is TANGERInE?")
    assert ranked[0].text.startswith("TANGERInE:")
    assert ranked[1].title == "TANGERInE flight"
    crowd = [
        Hit(
            kind="doc",
            text="Discussed the TANGERInE flight.",
            title="TANGERInE flight",
            path=f"docs/n{index}.md",
            score=-0.1,
        )
        for index in range(20)
    ]
    kept = _keep_best(crowd + [defined], "What is TANGERInE?")
    assert kept[0].text.startswith("TANGERInE:")
    who = Hit(kind="message", text="Asked Riley about the bay.", title="Riley")
    note = Hit(kind="doc", text="Riley sent the checklist.", title="Sunday session", path="docs/meetings.md")
    ordered = merge_hits([note], [who], prefer_docs=True, question="Who is Riley?")
    assert ordered[0] is who


def test_compton_fair_matches_the_topic_line_not_a_mention():
    from retrieve import partial_topic_match

    topic = Hit(
        kind="doc",
        text=(
            "Compton Air and Space Fair: Saturday, October 24, 2026 at "
            "Compton/Woodley Airport in LA; volunteers leave around 6:45 AM."
        ),
        title="Key facts",
        path="docs/facts.md",
    )
    booth = Hit(kind="doc", text="The fair booth needs a poster.", title="Notes", path="docs/notes.md")
    assert partial_topic_match("When is the Compton fair?", topic)
    assert not partial_topic_match("When is the Compton fair?", booth)
    ranked = merge_hits([booth, topic], [], prefer_docs=True, question="When is the Compton fair?")
    assert ranked[0] is topic
    assert "Woodley" in ranked[0].text


def test_a_dated_model_answer_is_not_thrown_away(db, tmp_path):
    runtime = _runtime(tmp_path, 'cite_sources = false\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\nvisibility: public\n---\n\n"
        "Compton Air and Space Fair: Saturday, October 24, 2026 at Compton/Woodley Airport in LA.\n",
        encoding="utf-8",
    )
    reply = "The Compton Air and Space Fair is on Saturday, October 24, 2026 at Compton/Woodley Airport in LA."
    llm = Scripted([reply])
    result = _ask(db, runtime, docs, "When is the Compton fair?", llm)
    assert result.kind == "answer"
    assert llm.prompts == ["When is the Compton fair?"]
    assert "October 24, 2026" in result.text
    assert "Woodley" in result.text
    assert "don't know" not in result.text
