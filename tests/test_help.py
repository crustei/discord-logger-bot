import asyncio

import discord

from bot import help_text, presence_activity
from tests.conftest import committed_runtime
from voice import RuntimeConfig, set_runtime


def _write(tmp_path, body: str) -> RuntimeConfig:
    root = tmp_path / "root"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "settings.toml").write_text(body, encoding="utf-8")
    return RuntimeConfig(root)


def test_shipped_status_and_policy_urls(tmp_path):
    runtime = committed_runtime(tmp_path)
    assert runtime.status_text == "Always listening... | /help"
    assert runtime.terms_url == ""
    assert runtime.privacy_url == ""
    assert len(runtime.status_text) <= 128


def test_omitted_status_and_urls_use_defaults(tmp_path):
    runtime = _write(tmp_path, 'display_title = "Discord bot"\norg_name = "My Org"\n')
    assert runtime.status_text == "Always listening... | /help"
    assert runtime.terms_url == ""
    assert runtime.privacy_url == ""


def test_status_is_clipped_and_only_http_urls_are_kept(tmp_path):
    runtime = _write(
        tmp_path,
        "\n".join(
            [
                'display_title = "Club Desk"',
                'org_name = "Example Club"',
                'status_text = "' + ("x" * 200) + '"',
                'terms_url = "javascript:alert(1)"',
                'privacy_url = "HTTPS://example.test/privacy"',
                "optout_command = false",
            ]
        )
        + "\n",
    )
    assert len(runtime.status_text) == 128
    assert runtime.status_text == "x" * 128
    assert runtime.terms_url == ""
    assert runtime.privacy_url == "HTTPS://example.test/privacy"
    text = help_text(runtime)
    assert "Club Desk" in text
    assert "Example Club" in text
    assert "/ask" in text
    assert "@mention" in text
    assert "officer" in text
    assert "turned off" in text
    assert "javascript" not in text
    assert "HTTPS://example.test/privacy" in text
    assert "Terms:" not in text
    assert len(text) <= 2000


def test_help_includes_both_links_when_configured(tmp_path):
    runtime = _write(
        tmp_path,
        "\n".join(
            [
                'display_title = "Discord bot"',
                'org_name = "My Org"',
                'terms_url = "https://example.test/terms"',
                'privacy_url = "http://example.test/privacy"',
            ]
        )
        + "\n",
    )
    text = help_text(runtime)
    assert "My Org" in text
    assert "/optout" in text
    assert "blocklist" in text
    assert "https://example.test/terms" in text
    assert "http://example.test/privacy" in text


def test_presence_is_a_custom_status(tmp_path):
    runtime = _write(tmp_path, 'status_text = "Always listening... | /help"\n')
    activity = presence_activity(runtime.status_text)
    payload = activity.to_dict()
    assert isinstance(activity, discord.CustomActivity)
    assert payload["type"] == discord.ActivityType.custom.value
    assert payload["state"] == "Always listening... | /help"
    assert payload["name"] == "Custom Status"
    long = presence_activity("y" * 200)
    assert len(long.to_dict()["state"]) == 128


def test_ready_and_help_use_the_configured_status(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DB", str(tmp_path / "bot.db"))
    runtime = _write(
        tmp_path,
        "\n".join(
            [
                'display_title = "Discord bot"',
                'org_name = "My Org"',
                'status_text = "Ask with /help"',
                'terms_url = "https://example.test/terms"',
                'privacy_url = "https://example.test/privacy"',
            ]
        )
        + "\n",
    )
    set_runtime(runtime)
    from bot import QABot

    bot = QABot()
    seen = {}

    async def change_presence(*, activity=None, status=None):
        seen["activity"] = activity

    bot.change_presence = change_presence
    asyncio.run(bot.on_ready())
    assert seen["activity"].to_dict()["state"] == "Ask with /help"

    class Response:
        def __init__(self):
            self.sent = []

        async def send_message(self, text, *, ephemeral=False):
            self.sent.append((text, ephemeral))

    class Interaction:
        def __init__(self, guild):
            self.guild = guild
            self.response = Response()

    direct = Interaction(None)
    asyncio.run(bot.handle_help(direct))
    assert direct.response.sent == [("Use this command in a server.", True)]

    bot._watching = lambda guild_id: False
    missed = Interaction(type("G", (), {"id": 1})())
    asyncio.run(bot.handle_help(missed))
    assert missed.response.sent[0][1] is True
    assert "not watching" in missed.response.sent[0][0]

    bot._watching = lambda guild_id: True
    shown = Interaction(type("G", (), {"id": 1})())
    asyncio.run(bot.handle_help(shown))
    text, ephemeral = shown.response.sent[0]
    assert ephemeral is True
    assert "Discord bot" in text
    assert "My Org" in text
    assert "/ask" in text
    assert "https://example.test/terms" in text
    assert "https://example.test/privacy" in text
    assert "/admin" not in text
    bot.db.close()
