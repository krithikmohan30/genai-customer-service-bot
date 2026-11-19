"""Build data/policies/index.json from the training dataset plus curated policies.

Two sources, one index:
  1. dataset/dataset.csv  - the 76 FAQ rows from the training project (lineage)
  2. data/policies/manual - policy documents with YAML front matter

Run: python scripts/build_policies.py
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset" / "dataset.csv"
MANUAL_DIR = ROOT / "data" / "policies" / "manual"
INDEX = ROOT / "data" / "policies" / "index.json"

FAQ_EFFECTIVE = "2025-01-01"

PRODUCT_RULES: list[tuple[str, str]] = [
    (
        r"\b(?:emi|refund|payment|fee|price|invoice|billing|charge|discount|coupon)\b",
        "billing",
    ),
    (r"\b(?:certificate|completion|attendance)\b", "certificate"),
    (r"\b(?:placement|job|interview|resume|salary|career)\b", "placement"),
    (
        r"\b(?:dataset|project|tools|software|laptop|python|sql|power\s?bi|javascript)\b",
        "curriculum",
    ),
    (r"\b(?:instructor|doubt|support|contact|batch|duration|timing)\b", "support"),
]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def guess_product(text: str) -> str:
    lowered = text.lower()
    for pattern, product in PRODUCT_RULES:
        if re.search(pattern, lowered):
            return product
    return "bootcamp"


def load_faqs() -> list[dict]:
    rows = list(csv.DictReader(DATASET.open(encoding="cp1252", newline="")))
    docs: list[dict] = []
    for index, row in enumerate(rows, start=1):
        prompt = clean(row.get("prompt", ""))
        response = clean(row.get("response", ""))
        if not prompt or not response:
            continue
        docs.append(
            {
                "id": f"faq_{index:03d}",
                "title": prompt[:90],
                "topic": f"faq_{index:03d}",
                "product": guess_product(prompt + " " + response),
                "region": "IN",
                "access_level": "public",
                "version": 1,
                "effective_at": FAQ_EFFECTIVE,
                "expires_at": None,
                "content": f"Question: {prompt}\nAnswer: {response}",
                "origin": "dataset/dataset.csv",
            }
        )
    return docs


def split_front_matter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    _, _, rest = raw.partition("---")
    header, sep, body = rest.partition("\n---")
    if not sep:
        return {}, raw
    meta = yaml.safe_load(header) or {}
    return meta, body.strip()


def load_manual() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(MANUAL_DIR.glob("*.md")):
        meta, body = split_front_matter(path.read_text(encoding="utf-8"))
        if not meta.get("id"):
            raise ValueError(f"{path.name}: front matter must define an id")
        docs.append(
            {
                "id": str(meta["id"]),
                "title": str(meta.get("title", meta["id"])),
                "topic": str(meta.get("topic", meta["id"])),
                "product": str(meta.get("product", "general")),
                "region": str(meta.get("region", "GLOBAL")),
                "access_level": str(meta.get("access_level", "public")),
                "version": int(meta.get("version", 1)),
                "effective_at": _date_string(meta.get("effective_at")),
                "expires_at": _date_string(meta.get("expires_at")),
                "content": body,
                "origin": f"data/policies/manual/{path.name}",
            }
        )
    return docs


def _date_string(value) -> str | None:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main() -> None:
    docs = load_faqs() + load_manual()
    ids = [doc["id"] for doc in docs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate document ids in policy index")

    INDEX.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {"faq": sum(d["origin"] == "dataset/dataset.csv" for d in docs)},
        "docs": docs,
    }
    INDEX.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    manual = [d for d in docs if d["origin"] != "dataset/dataset.csv"]
    print(f"indexed {len(docs)} docs ({len(manual)} policies) -> {INDEX}")
    for doc in sorted(manual, key=lambda d: (d["topic"], d["version"])):
        print(
            f"  {doc['topic']:<20} v{doc['version']:<2} {doc['region']:<6}"
            f" {doc['access_level']:<8} {doc['effective_at']} -> {doc['expires_at']}"
        )


if __name__ == "__main__":
    main()
