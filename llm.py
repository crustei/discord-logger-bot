"""Pluggable language-model providers.

The model never searches. ``qa.py`` builds one small context block and passes
that string here. Add a provider by subclassing ``LLMProvider`` and registering
it in ``PROVIDERS``.

The system prompt is the System prompt section of config/persona.md, not this file.

Environment (the only model settings; see .env.example):
  LLM_BASE_URL   OpenAI-compatible base, e.g. https://api.openai.com/v1
  LLM_API_KEY    API key. Use any non-empty placeholder for a local server.
  LLM_MODEL      model name
If any of the three is missing, the bot paraphrases matching notes locally instead of calling the network. A failed request does not paste those notes. A reply that copies the notes is asked once to rephrase, and is not edited word by word.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def system_prompt() -> str:
    """Current system prompt from config/persona.md, with the character cap filled in."""
    from voice import get_runtime

    return get_runtime().system_prompt


class LLMError(RuntimeError):
    """The provider failed. Callers reply that they do not know."""


class LLMProvider:
    """Student hook: implement ``answer(question, context_block) -> str``."""

    name = "base"

    def answer(self, question: str, context_block: str) -> str:
        raise NotImplementedError

    def expand_query(self, question: str) -> str:
        """Optional search keywords. The default adds none."""
        return ""


class ExtractiveProvider(LLMProvider):
    """No network. Quotes the context block our code already assembled."""

    name = "extractive"

    def answer(self, question: str, context_block: str) -> str:
        from voice import get_runtime

        block = (context_block or "").strip()
        if not block:
            return "I don't have anything relevant in the notes I was given."
        return get_runtime().say("no_model") + "\n\n" + block


class OpenAICompatibleProvider(LLMProvider):
    """POST {base_url}/chat/completions. Works with OpenAI, OpenRouter, Ollama, and similar."""

    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30, opener=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def answer(self, question: str, context_block: str) -> str:
        user = f"Question:\n{question}\n\nContext:\n{context_block}"
        return self._complete(system_prompt(), user)

    def expand_query(self, question: str) -> str:
        """A few extra search words. Failures stay local; answers still work."""
        import re

        try:
            text = self._complete(
                "List up to six search keywords for the question. Spaces only. No sentences.",
                (question or "")[:300],
            )
        except LLMError:
            return ""
        words = re.findall(r"[0-9A-Za-z]{2,}", text.lower())
        return " ".join(words[:6])

    def _complete(self, system: str, user: str) -> str:
        url = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise LLMError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach the model ({exc.reason})") from exc
        except TimeoutError as exc:
            raise LLMError("timed out") from exc
        try:
            body = json.loads(raw)
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError, TypeError) as exc:
            raise LLMError("unexpected model response") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty model response")
        return content.strip()


PROVIDERS = {
    "extractive": ExtractiveProvider,
    "openai": OpenAICompatibleProvider,
}


def provider_from_env() -> LLMProvider:
    """OpenAI-compatible client when LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are set."""
    base = os.environ.get("LLM_BASE_URL", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if base and key and model:
        return OpenAICompatibleProvider(base, key, model)
    return ExtractiveProvider()
