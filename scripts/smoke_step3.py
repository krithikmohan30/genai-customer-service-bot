"""Live smoke test against a running server, printed through Python so the
console encoding cannot mangle Hindi or Tamil text.

Usage: python scripts/smoke_step3.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8010"


def call(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    call("/admin/time", {"iso": "2026-11-23T11:00:00+05:30"})

    hindi = call("/chat", {"message": "कोर्स नहीं चल रहा, समस्या है"})
    print(
        "hindi  ->", hindi["language"], hindi["sentiment"]["label"], "|", hindi["reply"]
    )

    tamil = call("/chat", {"message": "உடனே உதவி வேண்டும்"})
    print(
        "tamil  ->", tamil["language"], tamil["sentiment"]["label"], "|", tamil["reply"]
    )

    hinglish = call("/chat", {"message": "mera refund nahi aaya, pareshan hoon"})
    print(
        "hinglish ->",
        hinglish["language"],
        hinglish["sentiment"]["label"],
        "|",
        hinglish["reply"],
    )

    call("/admin/time", {"advance_minutes": 16})
    sweep = call("/admin/sweep", {})
    print("sweep  -> created", sweep["created"])

    detail = call(f"/sessions/{hindi['session_id']}")
    for record in detail["escalations"]:
        print(
            "  escalation:",
            record["reason"],
            "|",
            record["condition"],
            "|",
            record["queue"],
        )
        print("  summary   :", record["summary"])


if __name__ == "__main__":
    sys.exit(main())
