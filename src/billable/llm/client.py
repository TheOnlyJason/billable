"""Provider-agnostic LLM client interface.

v1 ships with `OpenAIClient` only. The interface is intentionally small —
just a `complete_json(...)` method — because both stages return JSON. If we
ever need streaming or tool use, add new methods rather than overloading.

Adding a new provider (Anthropic, Azure OpenAI, Bedrock, Ollama):
    1. Implement `LLMClient` in a new file in this directory.
    2. Wire it up in `cli.py`'s provider selection.
    3. Update DESIGN.md §3 / §7.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any


class LLMError(RuntimeError):
    """Raised when the LLM call fails after retries or returns unparseable JSON."""


class LLMClient(ABC):
    """Provider-agnostic LLM interface used by the cluster and narrate stages."""

    @abstractmethod
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Return the model's response parsed as JSON.

        Implementations MUST request JSON-mode output from the provider when
        available. If parsing fails after retries, raise `LLMError` with the
        original cause attached.
        """
        ...


class OpenAIClient(LLMClient):
    """OpenAI-backed implementation.

    Reads `OPENAI_API_KEY` from the environment if `api_key` is not given.
    Uses Chat Completions with `response_format={"type": "json_object"}`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_retries: int = 3,
        backoff_seconds: float = 1.5,
    ) -> None:
        from openai import OpenAI  # imported lazily so tests don't need network

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Put it in your environment or .env file."
            )
        self._client = OpenAI(api_key=resolved_key)
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        from openai import APIError, APIStatusError, RateLimitError

        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
            except (RateLimitError, APIStatusError, APIError) as e:
                last_err = e
                if attempt >= self._max_retries:
                    break
                time.sleep(self._backoff * attempt)
                continue

            content = (response.choices[0].message.content or "").strip()
            if not content:
                last_err = LLMError("OpenAI returned an empty response.")
                if attempt >= self._max_retries:
                    break
                time.sleep(self._backoff * attempt)
                continue

            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                # JSON mode should make this impossible, but surface it cleanly if it happens.
                raise LLMError(
                    f"OpenAI returned non-JSON despite json_object mode: {content[:200]!r}"
                ) from e

        raise LLMError(
            f"OpenAI call failed after {self._max_retries} attempts: {last_err!r}"
        ) from last_err
