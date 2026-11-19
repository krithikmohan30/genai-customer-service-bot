"""Thin wrapper over Google's `google-genai` SDK.

Two callers exist in the app: triage (needs strict JSON) and generation
(needs prose). Both go through here so model choice, retries and timeouts are
configured once.

The SDK is imported lazily: `tests/` run with TRIAGE_MODE=rules and must not
need a network stack or an API key.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any

from . import config

try:  # pragma: no cover - import guard only
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    load_dotenv()


class LLMError(RuntimeError):
    """Raised when every configured model failed."""


@lru_cache(maxsize=1)
def _client() -> Any:
    from google import genai

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise LLMError(
            "GOOGLE_API_KEY is not set. Copy .env.example to .env and add a key."
        )
    return genai.Client(api_key=api_key)


def available() -> bool:
    """True when a key is present, so callers can fall back to rules."""
    return bool(os.getenv("GOOGLE_API_KEY"))


def models() -> tuple[str, ...]:
    primary = str(config.get("llm.model", "gemini-3.5-flash"))
    fallback = str(config.get("llm.fallback_model", "gemini-3.5-flash-lite"))
    return (primary,) if primary == fallback else (primary, fallback)


def generate(prompt: str, temperature: float | None = None) -> str:
    """Return model text, trying the primary model then the fallback."""
    if temperature is None:
        temperature = float(config.get("llm.temperature", 0.2))

    last_error: Exception | None = None
    for model in models():
        try:
            response = _client().models.generate_content(
                model=model,
                contents=prompt,
                config={"temperature": temperature, "max_output_tokens": 2048},
            )
            text = (getattr(response, "text", "") or "").strip()
            if text:
                return text
            last_error = LLMError("empty response")
        except Exception as exc:  # 503s are routine on new flash models
            last_error = exc
    raise LLMError(f"Gemini call failed: {last_error}")


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def strip_fences(text: str) -> str:
    """Models still wrap JSON in markdown fences; strip them before parsing."""
    return _FENCE.sub("", text or "").strip()


def generate_json(prompt: str, temperature: float = 0.0) -> dict:
    """Generate and parse a JSON object, returning {} if the model misbehaves."""
    raw = generate(prompt, temperature=temperature)
    cleaned = strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}
