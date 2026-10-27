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
import logging
import os
import time
import urllib.error
import urllib.request


def system_prompt() -> str:
    """Current system prompt from config/persona.md, with the character cap filled in."""
    from voice import get_runtime

    return get_runtime().system_prompt


class LLMError(RuntimeError):
    """The provider failed. Callers reply that they do not know."""


log = logging.getLogger("llm")
FALLBACK_TAG = "-# answered without AI (backup mode)"
EXHAUSTED_SECONDS = 15 * 60
_state = {"exhausted_until": 0.0}


def primary_exhausted() -> bool:
    """True for 15 minutes after a 429, so the answer path skips the model instead of wasting calls."""
    return time.monotonic() < _state["exhausted_until"]


def mark_exhausted() -> None:
    _state["exhausted_until"] = time.monotonic() + EXHAUSTED_SECONDS


class LLMProvider:
    """Student hook: implement ``answer(question, context_block) -> str``."""

    name = "base"

    def answer(self, question: str, context_block: str) -> str:
        raise NotImplementedError

    def expand_query(self, question: str) -> str:
        """Optional search keywords. The default adds none."""
        return ""

    def rerank(self, question: str, listing: str) -> str:
        """Optional: ids of the best entries, best first. The default keeps keyword order."""
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
        except Exception:
            return ""
        words = re.findall(r"[0-9A-Za-z]{2,}", text.lower())
        return " ".join(words[:6])

    def rerank(self, question: str, listing: str) -> str:
        """One compact call: ids of the best <=20 entries, ordered. Errors propagate to the caller's fallback."""
        system = ("Rank knowledge entries for answering the question. Each line: id | date | source type | start of entry. "
                  "Reply with up to 20 ids, best first, comma-separated, nothing else. Prefer entries that directly answer, "
                  "then current-year and authoritative ones (drive > kb > discord-note), then the newest. Skip off-topic entries.")
        return self._complete(system, f"Question: {question}\n\n{listing}")

    def check_grounded(self, question: str, answer: str, context_block: str) -> bool | None:
        """Second pass: is every claim in the answer supported by the numbered entries?

        True / False, or None when the check itself fails (the answer then stands).
        """
        system = (
            "You verify answers. Reply with exactly one word: SUPPORTED if every factual claim "
            "in the answer (names, dates, places, numbers) is stated in the entries, otherwise "
            "UNSUPPORTED. Ignore citations, greetings, and formatting."
        )
        user = f"Question:\n{question}\n\nEntries:\n{context_block}\n\nAnswer:\n{answer}"
        try:
            verdict = self._complete(system, user).strip().upper()
        except LLMError:
            return None
        if verdict.startswith("UNSUPPORTED"):
            return False
        if verdict.startswith("SUPPORTED"):
            return True
        return None

    def _complete(self, system: str, user: str) -> str:
        if primary_exhausted():
            raise LLMError("primary exhausted (cached 429)")
        try:
            return self._post(system, user)
        except LLMError as exc:
            if "HTTP 429" in str(exc):
                mark_exhausted()
                log.warning("llm 429 from %s; skipping the model for 15 min (backup mode)", self.model)
                try:
                    import counters

                    counters.bump("llm_429", self.model, quiet=True)
                except Exception:
                    pass
            raise

    def _post(self, system: str, user: str) -> str:
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
