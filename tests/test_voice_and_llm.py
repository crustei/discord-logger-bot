import json

from llm import ExtractiveProvider, OpenAICompatibleProvider, provider_from_env, system_prompt
from qa import fit_answer
from voice import RuntimeConfig


def _write_voice(
    root,
    *,
    chars=600,
    snippets=4,
    excluded=None,
    held="Held #{request_id} custom",
    title="Club Bot",
    org="Example Club",
):
    config = root / "config"
    config.mkdir(parents=True)
    names = ", ".join(repr(name) for name in (excluded or []))
    (config / "settings.toml").write_text(
        "\n".join(
            [
                f"display_title = {title!r}".replace("'", '"'),
                f"org_name = {org!r}".replace("'", '"'),
                f"max_answer_chars = {chars}",
                f"max_context_snippets = {snippets}",
                "excluded_channels = [" + names + "]",
                'allowed_channels = ["general"]',
                "public_channels = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (config / "persona.md").write_text(
        "\n".join(
            [
                "# Voice",
                "## System prompt",
                "You help {org_name}. Answer in at most {max_answer_chars} characters. Use only the Context block.",
                "## Refusal",
                "Nothing matched the custom refusal.",
                "## Held for approval",
                held,
                "## Held for approval preview",
                "Preview hold.",
                "## Denied",
                "Custom denial.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_settings_and_persona_reload_without_a_new_object(tmp_path):
    _write_voice(tmp_path, chars=600, held="Held #{request_id} first", excluded=["officers-only"])
    settings = tmp_path / "config" / "settings.toml"
    settings.write_text(settings.read_text() + '\n[blocklist.guilds]\n"77" = ["leadership"]\n', encoding="utf-8")
    runtime = RuntimeConfig(tmp_path)
    assert runtime.max_answer_chars == 600
    assert "600" in runtime.system_prompt
    assert "Example Club" in runtime.system_prompt
    assert runtime.held(7) == "Held #7 first"
    assert "officers-only" in {name.casefold() for name in runtime.excluded_channels}
    assert "private-channel" not in {name.casefold() for name in runtime.excluded_channels}
    assert runtime.allowed_channels == {"general"}
    from permissions import is_excluded

    assert is_excluded("8", "leadership", runtime.blocklist, "77")
    assert not is_excluded("8", "leadership", runtime.blocklist, "88")

    (tmp_path / "config" / "settings.toml").write_text(
        "max_answer_chars = 180\nmax_context_snippets = 2\nexcluded_channels = []\nallowed_channels = []\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "persona.md").write_text(
        "## System prompt\nCap {max_answer_chars}. Use only the Context block.\n"
        "## Held for approval\nWaiting on #{request_id} second\n",
        encoding="utf-8",
    )
    assert runtime.reload_if_changed() is True
    assert runtime.max_answer_chars == 180
    assert runtime.max_context_snippets == 2
    assert "180" in runtime.system_prompt
    assert runtime.held(3) == "Waiting on #3 second"
    assert not any(name.casefold().lstrip("#") == "private-channel" for name in runtime.excluded_channels)
    assert not any(name.casefold() == "officers-only" for name in runtime.excluded_channels)
    assert runtime.display_title == "Discord bot"
    assert runtime.org_name == "this server"


def test_local_settings_overlay_replaces_keys_and_merges_guilds(tmp_path):
    from permissions import is_excluded

    _write_voice(tmp_path, excluded=["base-channel"], title="Base Title", org="Base Org")
    base = tmp_path / "config" / "settings.toml"
    base.write_text(base.read_text() + '\n[blocklist.guilds]\n"1" = ["room-a"]\n', encoding="utf-8")
    runtime = RuntimeConfig(tmp_path)
    assert runtime.display_title == "Base Title"
    local = tmp_path / "config" / "settings.local.toml"
    local.write_text(
        "\n".join(
            [
                'display_title = "Local Title"',
                'org_name = "Local Org"',
                'excluded_channels = ["private-channel"]',
                "",
                "[blocklist.guilds]",
                '"9" = ["officers"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert runtime.reload_if_changed() is True
    assert runtime.display_title == "Local Title"
    assert runtime.org_name == "Local Org"
    names = {name.casefold() for name in runtime.excluded_channels}
    assert "private-channel" in names
    assert "base-channel" not in names
    assert is_excluded("1", "officers", runtime.blocklist, "9")
    assert is_excluded("2", "room-a", runtime.blocklist, "1")


def test_answer_cap_is_enforced_in_code():
    footer = "Sources:\n- #general · 2026-10-18 · Ethan"
    text = fit_answer("word " * 400, footer, limit=120)
    assert len(text) <= 120
    assert "#general" in text
    assert "2026-10-18" in text


def test_shipped_prompt_mentions_the_cap_and_context(tmp_path):
    from tests.conftest import committed_runtime

    committed_runtime(tmp_path)
    prompt = system_prompt()
    assert "600" in prompt
    assert "Context" in prompt


def test_provider_reads_only_llm_env_names(monkeypatch):
    monkeypatch.setenv("BOGUS_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("BOGUS_LLM_API_KEY", "nope")
    monkeypatch.setenv("BOGUS_LLM_MODEL", "nope")
    assert isinstance(provider_from_env(), ExtractiveProvider)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret-key")
    monkeypatch.setenv("LLM_MODEL", "demo-model")
    provider = provider_from_env()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://example.test/v1"
    assert provider.model == "demo-model"


def test_openai_request_uses_persona_and_does_not_echo_the_key():
    seen = {}

    class Response:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Meet in the Solarium."}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout=30):
        seen["url"] = request.full_url
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.data.decode())
        return Response()

    provider = OpenAICompatibleProvider(
        "https://example.test/v1/",
        "secret-key",
        "demo-model",
        opener=opener,
    )
    text = provider.answer("When is it?", "[1] #general · 2026-10-18 · Ethan\nSunday at 3PM")
    assert text == "Meet in the Solarium."
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "demo-model"
    assert "Context" in seen["body"]["messages"][0]["content"]
    assert "secret-key" not in seen["body"]["messages"][1]["content"]
    assert "[1] #general" in seen["body"]["messages"][1]["content"]
