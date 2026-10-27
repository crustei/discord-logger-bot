import io, json, urllib.error

import pytest

import llm
from llm import FALLBACK_TAG, LLMError, OpenAICompatibleProvider


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener_for(code, calls):
    def opener(req, timeout=None):
        calls.append(json.loads(req.data)["model"])
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return FakeResp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
    return opener


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(llm, "_state", {"exhausted_until": 0.0})
    monkeypatch.setattr(llm, "system_prompt", lambda: "sys")


def test_no_backup_model_is_ever_called(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "big")
    calls = []
    p = OpenAICompatibleProvider("http://x", "k", "lite", opener=opener_for(429, calls))
    with pytest.raises(LLMError):
        p.answer("q", "c")
    assert calls == ["lite"]


def test_429_caches_exhausted_and_skips_network(caplog, tmp_path):
    import counters
    calls = []
    p = OpenAICompatibleProvider("http://x", "k", "lite", opener=opener_for(429, calls))
    with caplog.at_level("WARNING", logger="llm"):
        with pytest.raises(LLMError):
            p.answer("q", "c")
    assert llm.primary_exhausted()
    with pytest.raises(LLMError):
        p.rerank("q", "l")
    assert calls == ["lite"]
    assert sum("429" in r.message for r in caplog.records) == 1
    assert counters.load(tmp_path / "counters.json").get("llm_429") == 1


def test_500_does_not_cache():
    calls = []
    p = OpenAICompatibleProvider("http://x", "k", "lite", opener=opener_for(500, calls))
    for _ in range(2):
        with pytest.raises(LLMError):
            p.answer("q", "c")
    assert calls == ["lite", "lite"] and not llm.primary_exhausted()


def test_exhausted_answer_path_makes_no_model_calls_and_tags(db, tmp_path, caplog):
    import counters
    from qa import handle_question
    from tests.test_kb_bases import _write
    root = tmp_path / "knowledge"
    _write(root, "public/where.md", "Where ACME meets", "ACME meets in the lab building on Tuesdays.")
    llm.mark_exhausted()
    calls = []
    p = OpenAICompatibleProvider("http://x", "k", "lite", opener=opener_for(200, calls))
    with caplog.at_level("WARNING", logger="llm"):
        r = handle_question(db, "Where does ACME meet?", asker_id="1", channel_id="0", channel_name="x",
                            visible={"0"}, here={"0"}, docs_dir=str(root), llm=p, asker_tier="member")
    assert calls == []
    assert r.kind == "answer" and "lab" in r.text and r.text.endswith("\n" + FALLBACK_TAG)
    assert any("fallback extractive" in x.message for x in caplog.records)
    assert counters.load(tmp_path / "counters.json").get("llm_fallback_extractive") == 1


def test_backup_mode_respects_tiers(db, tmp_path):
    from qa import handle_question
    from tests.test_kb_bases import _write
    root = tmp_path / "knowledge"
    _write(root, "member/secret.md", "Hangar code", "The hangar door code is 4321.")
    llm.mark_exhausted()
    p = OpenAICompatibleProvider("http://x", "k", "lite", opener=opener_for(200, []))
    r = handle_question(db, "What is the hangar door code?", asker_id="1", channel_id="0", channel_name="x",
                        visible={"0"}, here={"0"}, docs_dir=str(root), llm=p, asker_tier="non-member")
    assert "4321" not in r.text


def test_tag_survives_badge():
    from qa import with_fallback_tag
    from feedback import badged_text, BADGE
    t = with_fallback_tag("Answer.\n\nBased on: doc", 100)
    assert t.endswith("\n" + FALLBACK_TAG) and len(t) <= 100 and with_fallback_tag(t, 100) == t
    b = badged_text(t)
    assert "Based on" not in b and BADGE in b and b.endswith("\n" + FALLBACK_TAG)
    assert badged_text(b) == b
    assert badged_text("Plain.\n\nBased on: x") == f"Plain.\n\n{BADGE}"
