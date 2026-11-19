"""Prompt-injection detection for text that arrives from documents or images.

Used in step 2 (policy documents), step 3 (knowledge-base ingestion) and step 5
(text extracted from uploaded files). Detection is deliberately pattern based:
the model must never be the thing deciding whether an instruction is hostile.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, str]] = [
    (
        "ignore_instructions",
        r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+instructions",
    ),
    (
        "disregard_instructions",
        r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
    ),
    (
        "role_override",
        r"you\s+are\s+now\s+(?:in\s+)?(?:admin|administrator|developer|root|dan)\b",
    ),
    ("role_override", r"act\s+as\s+(?:an?\s+)?(?:admin|administrator|system)\b"),
    (
        "prompt_leak",
        r"(?:reveal|show|print|repeat)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)",
    ),
    (
        "secret_leak",
        r"(?:print|reveal|share)\s+(?:the\s+)?(?:api\s*key|password|secret|token)",
    ),
    (
        "citation_suppression",
        r"do\s+not\s+(?:include|provide|add)\s+(?:any\s+)?(?:source\s+)?citation",
    ),
    (
        "policy_override",
        r"(?:approve|grant|allow)\s+(?:every|all|any)\s+(?:refund|request)",
    ),
    (
        "policy_override",
        r"no\s+proof\s+(?:of\s+payment\s+)?(?:is\s+)?required",
    ),
    ("policy_override", r"override\s+(?:the\s+)?(?:policy|rules|instructions)"),
    ("new_instructions", r"new\s+instructions\s*:"),
    ("unrestricted_mode", r"(?:developer|admin|god)\s+mode"),
]

_COMPILED = [(label, re.compile(pattern, re.I)) for label, pattern in PATTERNS]

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def findings(text: str | None) -> list[str]:
    """Labels for every injection pattern present in `text`."""
    if not text:
        return []
    labels: list[str] = []
    for label, pattern in _COMPILED:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    return labels


def detects(text: str | None) -> bool:
    return bool(findings(text))


def sanitise(text: str | None) -> tuple[str, list[str]]:
    """Remove offending sentences. Returns (clean_text, detected_labels)."""
    if not text:
        return "", []
    labels: list[str] = []
    kept: list[str] = []
    for sentence in _SENTENCE.split(text):
        found = findings(sentence)
        if found:
            labels.extend(label for label in found if label not in labels)
            continue
        kept.append(sentence)
    return " ".join(part.strip() for part in kept if part.strip()), labels
