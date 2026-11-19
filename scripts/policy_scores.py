"""Score the policy index and record it as evidence for the README.

Run: python scripts/policy_scores.py
Writes results/policy_scores.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config, policy  # noqa: E402

QUESTIONS = [
    ("refund, no region", "What is the refund policy?"),
    ("refund, India", "How many days do I have to request a refund in India?"),
    ("refund, historical", "What was the refund window in March 2025?"),
    ("EMI", "Is 9 month EMI available?"),
    ("certificate", "Can I still get a certificate if I miss two sessions?"),
    ("fee 2027", "What will the course fee be in 2027?"),
    ("support hours", "When is support available?"),
    ("duplicate charge", "My invoice shows a duplicate charge, what happens now?"),
    ("restricted doc", "What are the internal steps for a hacked account?"),
    ("noise 1", "What is the capital of France?"),
    ("noise 2", "Who is the Prime Minister of India?"),
    ("noise 3", "Do you sell laptops?"),
]


def main() -> None:
    config.set_override("policy.mode", "extractive")
    lines = ["Policy retrieval scores (extractive mode, cosine on MiniLM)", ""]
    for label, question in QUESTIONS:
        result = policy.answer(question)
        lines.append(f"[{label}] {question}")
        lines.append(
            f"  status={result['status']} reason={result.get('reason')} at={result['at']}"
        )
        for hit in result["hits"][:4]:
            lines.append(
                f"  {hit['score']:>7.3f}  {hit['id']:<28} v{hit['version']}"
                f" {hit['region']:<6} {hit['topic']}"
            )
        lines.append("")

    out = ROOT / "results" / "policy_scores.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"written to {out}")


if __name__ == "__main__":
    main()
