"""Access tiers: config, doc front matter, channel derivation, and /admin writers."""

from pathlib import Path

from qa import handle_question
from retrieve import Hit
from tests.conftest import add
from tiers import (
    asker_tier_index,
    assign_role_tier,
    channel_tier_name,
    format_tiers,
    resolve_doc_tier,
    set_channel_tier,
)
from voice import RuntimeConfig, set_runtime


OFFICER_FACT = "The zelkova latch code is cedar-42."
MEMBER_FACT = "The quillon drawer mark is maple-7."
PUBLIC_FACT = "Recruitment applications open on 1 November 2026."


def _runtime(tmp_path, body: str) -> RuntimeConfig:
    root = tmp_path / "voice"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "settings.toml").write_text(body, encoding="utf-8")
    (config / "persona.md").write_text(
        "## Refusal\nI don't know that yet. Ask {officer_role}{officer_where}.\n",
        encoding="utf-8",
    )
    runtime = RuntimeConfig(root)
    set_runtime(runtime)
    return runtime


def _settings() -> str:
    return """
tiers = ["non-member", "member", "officer", "executive"]
cite_sources = false

[tier_roles]
member = ["member"]
officer = ["officer"]
executive = ["executive"]
"""


def _docs(tmp_path) -> Path:
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "facts.md").write_text(
        "---\ntitle: Key facts\ntier: non-member\n---\n\n" + PUBLIC_FACT + "\n",
        encoding="utf-8",
    )
    (docs / "member-notes.md").write_text(
        "---\ntitle: Member board\ntier: member\n---\n\n" + MEMBER_FACT + "\n",
        encoding="utf-8",
    )
    (docs / "officer-notes.md").write_text(
        "---\ntitle: Officer board\ntier: officer\n---\n\n" + OFFICER_FACT + "\n",
        encoding="utf-8",
    )
    return docs


class Recording:
    name = "recording"

    def __init__(self):
        self.contexts = []

    def answer(self, question, context_block):
        self.contexts.append(context_block or "")
        return "Recorded."


def _ask(db, runtime, docs, question, roles):
    llm = Recording()
    result = handle_question(
        db,
        question,
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general", "77", "officers"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=llm,
        runtime=runtime,
        asker_roles=list(roles),
    )
    return result, llm


def test_defaults_and_asker_tier():
    tiers = ["non-member", "member", "officer", "executive"]
    roles = {"non-member": [], "member": ["Members"], "officer": ["9"], "executive": []}
    assert asker_tier_index([], tiers, roles) == 0
    assert asker_tier_index(["members"], tiers, roles) == 1
    assert asker_tier_index(["9", "Members"], tiers, roles) == 2
    assert resolve_doc_tier("public", tiers) == "non-member"
    assert resolve_doc_tier("", tiers) == "non-member"
    assert resolve_doc_tier("approval", tiers) == "officer"
    assert resolve_doc_tier("member", tiers) == "member"
    assert resolve_doc_tier("not-a-tier", tiers) == "executive"


def test_channel_tier_from_viewers_and_explicit_map():
    tiers = ["non-member", "member", "officer", "executive"]
    roles = {"member": ["member"], "officer": ["officer"], "non-member": [], "executive": []}
    viewers = [("1", "@everyone", False), ("2", "member", True), ("3", "officer", True)]
    assert (
        channel_tier_name("77", "officers", tiers=tiers, channel_tiers={}, tier_roles=roles, viewers=viewers)
        == "member"
    )
    officer_only = [("1", "@everyone", False), ("2", "member", False), ("3", "officer", True)]
    assert (
        channel_tier_name("77", "officers", tiers=tiers, channel_tiers={}, tier_roles=roles, viewers=officer_only)
        == "officer"
    )
    everyone = [("1", "@everyone", True), ("3", "officer", True)]
    assert (
        channel_tier_name("77", "officers", tiers=tiers, channel_tiers={}, tier_roles=roles, viewers=everyone)
        == "non-member"
    )
    assert (
        channel_tier_name(
            "77",
            "officers",
            tiers=tiers,
            channel_tiers={"77": "officer"},
            tier_roles=roles,
            viewers=everyone,
        )
        == "officer"
    )
    assert (
        channel_tier_name("10", "general", tiers=tiers, channel_tiers={}, tier_roles=roles, viewers=None)
        == "non-member"
    )


def test_member_cannot_read_officer_facts_and_officer_can(db, tmp_path):
    runtime = _runtime(tmp_path, _settings())
    docs = _docs(tmp_path)
    member, member_llm = _ask(db, runtime, docs, "What is the zelkova latch code?", ["member"])
    assert member.kind == "empty"
    assert member.llm_called is False
    assert member_llm.contexts == []
    assert "cedar" not in (member.context_block or "")
    assert "cedar" not in member.text
    assert "don't know" in member.text.casefold()
    assert "restricted" not in member.text.casefold()
    assert "hidden" not in member.text.casefold()

    officer, officer_llm = _ask(db, runtime, docs, "What is the zelkova latch code?", ["officer"])
    assert officer.kind == "answer"
    assert "cedar-42" in officer.context_block
    assert officer_llm.contexts and "cedar-42" in officer_llm.contexts[0]
    assert "maple-7" not in officer.context_block
    lower, _lower_llm = _ask(db, runtime, docs, "What is the quillon drawer mark?", ["officer"])
    assert lower.kind == "answer"
    assert "maple-7" in lower.context_block


def test_non_member_only_gets_public_facts(db, tmp_path):
    runtime = _runtime(tmp_path, _settings())
    docs = _docs(tmp_path)
    hidden, _llm = _ask(db, runtime, docs, "What is the quillon drawer mark?", [])
    assert hidden.kind == "empty"
    assert "maple" not in (hidden.context_block or "")
    assert "cedar" not in (hidden.context_block or "")
    assert "maple" not in hidden.text
    assert "cedar" not in hidden.text
    assert "don't know" in hidden.text.casefold()

    public, public_llm = _ask(db, runtime, docs, "When is recruitment?", [])
    assert public.kind == "answer"
    assert "1 November 2026" in public.context_block
    assert "maple-7" not in public.context_block
    assert "cedar-42" not in public.context_block
    assert public_llm.contexts
    assert "maple-7" not in public_llm.contexts[0]
    assert "cedar-42" not in public_llm.contexts[0]


def test_legacy_visibility_and_tier_front_matter(db, tmp_path):
    runtime = _runtime(tmp_path, _settings())
    docs = tmp_path / "notes"
    docs.mkdir()
    note = docs / "facts.md"
    note.write_text(
        "---\ntitle: Key facts\nvisibility: approval\n---\n\n" + OFFICER_FACT + "\n",
        encoding="utf-8",
    )
    held, _llm = _ask(db, runtime, docs, "What is the zelkova latch code?", ["member"])
    assert "cedar" not in (held.context_block or "")
    assert held.kind == "empty"
    seen, _llm = _ask(db, runtime, docs, "What is the zelkova latch code?", ["officer"])
    assert "cedar-42" in seen.context_block

    note.write_text(
        "---\ntitle: Key facts\ntier: member\nvisibility: approval\n---\n\n" + OFFICER_FACT + "\n",
        encoding="utf-8",
    )
    member, _llm = _ask(db, runtime, docs, "What is the zelkova latch code?", ["member"])
    assert member.kind == "answer"
    assert "cedar-42" in member.context_block


def test_explicit_channel_tier_hides_a_visible_channel(db, tmp_path):
    runtime = _runtime(tmp_path, _settings() + '\n[channel_tiers]\n"77" = "officer"\n"officers" = "officer"\n')
    docs = tmp_path / "notes"
    docs.mkdir()
    add(
        db,
        id="secret",
        channel_id="77",
        channel_name="officers",
        author_id="2002",
        content=OFFICER_FACT,
        timestamp="2026-10-18T12:00:00+00:00",
    )
    viewers = {
        "77": [("1", "@everyone", True), ("2", "member", True), ("3", "officer", True)],
        "officers": [("1", "@everyone", True), ("2", "member", True), ("3", "officer", True)],
    }
    member = handle_question(
        db,
        "What is the zelkova latch code?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general", "77", "officers"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
        asker_roles=["member"],
        channel_viewers=viewers,
    )
    assert member.kind == "empty"
    assert "cedar" not in (member.context_block or "")
    assert "cedar" not in member.text
    officer = handle_question(
        db,
        "What is the zelkova latch code?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general", "77", "officers"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
        asker_roles=["officer"],
        channel_viewers=viewers,
    )
    assert "cedar-42" in officer.context_block


def test_derived_channel_tier_without_a_map(db, tmp_path):
    runtime = _runtime(tmp_path, _settings())
    docs = tmp_path / "notes"
    docs.mkdir()
    add(
        db,
        id="secret",
        channel_id="77",
        channel_name="officers",
        author_id="2002",
        content=OFFICER_FACT,
    )
    viewers = {
        "77": [("1", "@everyone", False), ("2", "member", False), ("3", "officer", True)],
    }
    member = handle_question(
        db,
        "What is the zelkova latch code?",
        asker_id="1001",
        channel_id="10",
        channel_name="general",
        visible={"10", "general", "77", "officers"},
        here={"10", "general"},
        excluded=runtime.blocklist,
        docs_dir=str(docs),
        llm=Recording(),
        runtime=runtime,
        asker_roles=["member"],
        channel_viewers=viewers,
    )
    assert "cedar" not in (member.context_block or "")
    assert member.kind == "empty"


def test_admin_tier_writes_local_settings_only(tmp_path):
    body = 'display_title = "Discord bot"\nexcluded_channels = ["private-channel"]\n\n[blocklist.guilds]\n"42" = ["lobby"]\n'
    runtime = _runtime(tmp_path, 'tiers = ["non-member", "member", "officer", "executive"]\n' + body)
    local = runtime.local_settings_path
    local.write_text(body, encoding="utf-8")
    note = assign_role_tier(
        local,
        "officer",
        role_id="55",
        role_name="Officers",
        tiers=runtime.tiers,
        tier_roles=runtime.tier_roles,
    )
    channel = set_channel_tier(
        local,
        "member",
        channel_id="77",
        channel_name="members",
        tiers=runtime.tiers,
        channel_tiers=runtime.channel_tiers,
    )
    runtime.reload()
    text = local.read_text(encoding="utf-8")
    assert "Officers" in note
    assert "members" in channel
    assert 'display_title = "Discord bot"' in text
    assert "private-channel" in text
    assert "[blocklist.guilds]" in text
    assert '"42"' in text
    assert "[tier_roles]" in text
    assert "55" in text
    assert "[channel_tiers]" in text
    assert runtime.tier_roles["officer"] == ["55"]
    assert runtime.channel_tiers["77"] == "member"
    assert runtime.channel_tiers["members"] == "member"
    shown = format_tiers(runtime)
    assert "officer: 55" in shown
    assert "77: member" in shown
    assert "non-member" in shown
    committed = (runtime.root / "config" / "settings.toml").read_text(encoding="utf-8")
    assert "55" not in committed
    assert "[channel_tiers]" not in committed


def test_unknown_tier_is_rejected(tmp_path):
    runtime = _runtime(tmp_path, _settings())
    try:
        set_channel_tier(
            runtime.local_settings_path,
            "secret",
            channel_id="9",
            tiers=runtime.tiers,
            channel_tiers={},
        )
    except Exception as exc:
        assert "Unknown tier" in str(exc)
    else:
        raise AssertionError("unknown tier was accepted")
    assert not runtime.local_settings_path.is_file()


def test_above_tier_hit_is_not_a_source():
    """A hit object above the asker cannot be selected just by being in the list."""
    from qa import split_for_answer
    from retrieve import Retrieval

    runtime = RuntimeConfig.__new__(RuntimeConfig)
    runtime.tiers = ["non-member", "member", "officer"]
    runtime.tier_roles = {"non-member": [], "member": ["member"], "officer": ["officer"]}
    runtime.channel_tiers = {}
    runtime.sensitive_topics = []
    secret = Hit(kind="doc", text=OFFICER_FACT, title="Officer board", visibility="officer")
    found = Retrieval(own=[], others=[], docs=[secret])
    auto, review = split_for_answer(
        found,
        asker_id="1001",
        question="What is the zelkova latch code?",
        excluded=set(),
        scope={"10"},
        runtime=runtime,
        asker_index=1,
    )
    assert auto == []
    assert review == []
