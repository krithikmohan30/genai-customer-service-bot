"""PII masking.

Used on every log line, audit record and handoff summary. Rules-of-thumb
patterns for Indian support traffic; deliberately conservative.
"""

from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern]] = [
    # 13-19 digit sequences first, so card numbers are not partly eaten by Aadhaar.
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    (
        "UPI",
        re.compile(
            r"\b[\w.\-]{2,}@(?:okhdfcbank|oksbi|okicici|okaxis|paytm|ybl|ibl|upi)\b",
            re.I,
        ),
    ),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("OTP", re.compile(r"\b(?:otp|code)\D{0,10}\d{4,8}\b", re.I)),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")),
]


def mask(text: str | None) -> str:
    if not text:
        return ""
    masked = text
    for label, pattern in _RULES:
        masked = pattern.sub(f"[{label}]", masked)
    return masked


def contains_pii(text: str | None) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for _, pattern in _RULES)


def findings(text: str | None) -> list[str]:
    if not text:
        return []
    return [label for label, pattern in _RULES if pattern.search(text)]
