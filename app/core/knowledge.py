"""Task 3 - knowledge-base ingestion pipeline.

A document is not trusted because it arrived. Every file goes through:

    size -> type -> parse -> injection -> PII mask -> quality gate -> dedupe
    -> version row (pending) -> maintenance-window activation job

Then it stays watched:

    every 5 minutes  hash + content health check, auto-rollback on failure
    every failure     retry after 15, then 30, then 60 minutes, then give up

Versions are additive. Activation moves `pending` to `active` and marks the old
one `superseded`; rollback moves back. A document that cannot be rolled back is
retired rather than served broken.

Access level travels with the document and its versions, and reads are filtered
through the same `policy.access_levels` mapping the RAG layer uses.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, time, timedelta
from pathlib import Path

import yaml

from .. import store
from . import clock, config, injection, security

ROOT = Path(__file__).resolve().parents[2]
JOB_KIND = "kb.activate"

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_HEADING = re.compile(r"^#\s+(.+)$", re.M)
_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|FIXME|lorem ipsum)\b", re.I)
_BROKEN = re.compile(r"\[\[(?:broken|fail)\]\]|health\s*:\s*fail", re.I)


# --------------------------------------------------------------------------- paths


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def incoming_dir() -> Path:
    return _resolve(
        str(config.get("knowledge.incoming_dir", "data/knowledge/incoming"))
    )


def quarantine_dir() -> Path:
    return _resolve(
        str(config.get("knowledge.quarantine_dir", "data/knowledge/quarantine"))
    )


def allowed_types() -> list[str]:
    return [
        str(item).lstrip(".").lower()
        for item in config.get_list("knowledge.allowed_types", ["md", "txt"])
    ]


# --------------------------------------------------------------------------- window


def _hhmm(value: str, default: str) -> time:
    raw = str(value or default)
    hours, _, minutes = raw.partition(":")
    return time(int(hours), int(minutes or 0))


def maintenance_window() -> tuple[time, time]:
    return (
        _hhmm(config.get("knowledge.maintenance_window.start", "01:00"), "01:00"),
        _hhmm(config.get("knowledge.maintenance_window.end", "04:00"), "04:00"),
    )


def in_maintenance_window(at: datetime | None = None) -> bool:
    at = at or clock.now()
    start, end = maintenance_window()
    current = at.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def next_maintenance_start(at: datetime | None = None) -> datetime:
    at = at or clock.now()
    if in_maintenance_window(at):
        return at
    start, _ = maintenance_window()
    current = at.timetz().replace(tzinfo=None)
    candidate = datetime.combine(at.date(), start, tzinfo=clock.tz())
    if current < start:
        return candidate
    return candidate + timedelta(days=1)


# --------------------------------------------------------------------------- parsing


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return cleaned or "document"


def parse_front_matter(raw: str) -> tuple[dict, str]:
    match = _FRONT_MATTER.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, match.group(2)


def content_hash(content: str) -> str:
    normalised = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _title(meta: dict, content: str, path: Path) -> str:
    declared = str(meta.get("title") or "").strip()
    if declared:
        return declared[:200]
    heading = _HEADING.search(content)
    if heading:
        return heading.group(1).strip()[:200]
    return path.stem.replace("_", " ")[:200]


def quality(content: str, meta: dict, title: str) -> dict:
    """Deterministic quality score in [0, 1]; the gate compares it to config."""
    checks: dict[str, float] = {}
    checks["title"] = 0.15 if title else 0.0
    checks["length"] = 0.2 if len(content.strip()) >= 200 else 0.0
    checks["structure"] = (
        0.15
        if re.search(r"^#{2,3}\s", content, re.M) or content.count("\n- ") >= 2
        else 0.0
    )
    present = sum(1 for key in ("product", "region", "effective_at") if meta.get(key))
    checks["metadata"] = round(0.2 * present / 3, 3)
    checks["no_placeholders"] = 0.0 if _PLACEHOLDER.search(content) else 0.15
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", content.strip()) if part]
    average = sum(len(part.split()) for part in sentences) / max(1, len(sentences))
    checks["readability"] = 0.15 if average <= 30 else 0.0
    return {
        "score": round(sum(checks.values()), 2),
        "checks": checks,
        "sentences": len(sentences),
    }


# --------------------------------------------------------------------------- access


def access_levels() -> list[str]:
    return [
        str(level).lower()
        for level in config.get_list(
            "knowledge.access_levels", ["public", "customer", "agent", "internal"]
        )
    ]


def readable_levels(requester: str) -> list[str]:
    mapping = config.get("policy.access_levels", {}) or {}
    levels = mapping.get(str(requester).lower()) or ["public"]
    return [str(level).lower() for level in levels]


def can_read(document: dict, requester: str = "customer") -> bool:
    return str(document.get("access_level") or "internal").lower() in readable_levels(
        requester
    )


# --------------------------------------------------------------------------- quarantine


def _move_to_quarantine(path: Path) -> str:
    target_dir = quarantine_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().strftime("%Y%m%dT%H%M%S")
    target = target_dir / f"{path.stem}-{stamp}{path.suffix}"
    counter = 1
    while target.exists():
        counter += 1
        target = target_dir / f"{path.stem}-{stamp}-{counter}{path.suffix}"
    try:
        shutil.move(str(path), str(target))
    except OSError:
        return ""
    return str(target)


def _reject(
    document_id: str,
    path: Path,
    title: str,
    body: str,
    digest: str,
    reason: str,
    details: dict,
    at: datetime,
) -> dict:
    """Persist the rejection, then move the file out of the incoming folder."""
    document = store.get_document(document_id)
    if document is None:
        store.add_document(
            document_id,
            str(path),
            title=title,
            access_level=str(details.get("access_level") or "internal"),
            status="quarantined",
            current_version=0,
        )
    latest = store.latest_version(document_id)
    version = (int(latest["version"]) + 1) if latest else 1
    store.add_document_version(
        document_id,
        version,
        body[:20000],
        digest,
        status="rejected",
        title=title,
        access_level=str(details.get("access_level") or "internal"),
        quality=json.dumps({"reason": reason, **details}, ensure_ascii=False),
        pii_masked=1 if details.get("pii_masked") else 0,
    )
    moved = _move_to_quarantine(path)
    store.audit(
        "kb.quarantined",
        {
            "document_id": document_id,
            "reason": reason,
            "path": str(path),
            "moved_to": moved,
        },
    )
    return {
        "outcome": "quarantined",
        "document_id": document_id,
        "version": version,
        "reason": reason,
        "details": details,
        "moved_to": moved,
        "at": at.isoformat(),
    }


# --------------------------------------------------------------------------- scan


def _ingest(path: Path, at: datetime) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    max_bytes = int(float(config.get("knowledge.quarantine_max_mb", 5)) * 1024 * 1024)
    meta, body = parse_front_matter(raw)
    document_id = str(meta.get("id") or slug(path.stem))
    title = security.mask(_title(meta, body, path))
    access = str(meta.get("access_level") or meta.get("access") or "internal").lower()

    if len(raw.encode("utf-8")) > max_bytes:
        return _reject(
            document_id,
            path,
            title,
            body,
            content_hash(body),
            "oversized",
            {"bytes": len(raw.encode("utf-8")), "limit_bytes": max_bytes},
            at,
        )
    if not body.strip():
        return _reject(
            document_id, path, title, body, content_hash(body), "empty", {}, at
        )

    labels = injection.findings(raw)
    if (
        labels
        and str(config.get("knowledge.injection_action", "quarantine")) == "quarantine"
    ):
        return _reject(
            document_id,
            path,
            title,
            body,
            content_hash(body),
            "injection",
            {"injection": labels},
            at,
        )
    if labels:
        body = injection.sanitise(body)[0]

    masked = security.mask(body)
    pii_masked = masked != body
    digest = content_hash(masked)

    if access not in access_levels():
        return _reject(
            document_id,
            path,
            title,
            masked,
            digest,
            "unknown_access_level",
            {"access_level": access, "allowed": access_levels()},
            at,
        )

    gate = quality(masked, meta, title)
    threshold = float(config.get("knowledge.quality_gate_min_score", 0.6))
    if gate["score"] < threshold:
        return _reject(
            document_id,
            path,
            title,
            masked,
            digest,
            "quality_gate",
            {
                "access_level": access,
                "quality": gate,
                "threshold": threshold,
                "pii_masked": pii_masked,
            },
            at,
        )

    known = store.find_version_by_hash(digest)
    if known:
        store.audit(
            "kb.duplicate",
            {"document_id": document_id, "known_document": known["document_id"]},
        )
        return {
            "outcome": "duplicates",
            "document_id": document_id,
            "version": known["version"],
            "of": known["document_id"],
            "at": at.isoformat(),
        }

    latest = store.latest_version(document_id)
    if latest and latest["content_hash"] == digest:
        return {
            "outcome": "duplicates",
            "document_id": document_id,
            "version": latest["version"],
            "of": document_id,
            "at": at.isoformat(),
        }

    document = store.get_document(document_id)
    version = (int(latest["version"]) + 1) if latest else 1
    fields = {
        "title": title,
        "access_level": access,
        "product": meta.get("product"),
        "region": meta.get("region"),
        "effective_at": str(meta.get("effective_at") or "") or None,
        "expires_at": str(meta.get("expires_at") or "") or None,
        "quality": json.dumps(gate),
        "pii_masked": 1 if pii_masked else 0,
    }
    if document is None:
        store.add_document(
            document_id,
            str(path),
            title=title,
            product=meta.get("product"),
            region=meta.get("region"),
            access_level=access,
            effective_at=fields["effective_at"],
            expires_at=fields["expires_at"],
            content_hash=digest,
            quality=gate["score"],
            status="staged",
            current_version=version,
        )
    store.add_document_version(
        document_id, version, masked, digest, status="pending", **fields
    )

    run_after = next_maintenance_start(at)
    job_id = store.add_job(
        JOB_KIND,
        {"document_id": document_id, "version": version},
        run_after=run_after.isoformat(),
    )
    store.audit(
        "kb.staged",
        {
            "document_id": document_id,
            "version": version,
            "quality": gate["score"],
            "pii_masked": pii_masked,
            "activation": run_after.isoformat(),
        },
    )
    return {
        "outcome": "new" if document is None else "modified",
        "document_id": document_id,
        "version": version,
        "quality": gate["score"],
        "pii_masked": pii_masked,
        "activation": run_after.isoformat(),
        "job_id": job_id,
        "at": at.isoformat(),
    }


def scan(at: datetime | None = None) -> dict:
    """Detect new and modified files in the incoming directory."""
    at = at or clock.now()
    directory = incoming_dir()
    directory.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {
        "new": [],
        "modified": [],
        "duplicates": [],
        "quarantined": [],
        "skipped": [],
    }

    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        kind = path.suffix.lstrip(".").lower()
        if kind not in allowed_types():
            results["skipped"].append(
                {"file": path.name, "reason": f"unsupported_type:{kind or 'none'}"}
            )
            continue
        outcome = _ingest(path, at)
        results.setdefault(str(outcome["outcome"]), []).append(outcome)

    store.audit(
        "kb.scan",
        {key: len(value) for key, value in results.items() if isinstance(value, list)},
    )
    return {
        "at": at.isoformat(),
        "directory": str(directory),
        "counts": {key: len(value) for key, value in results.items()},
        **results,
    }


# --------------------------------------------------------------------------- versions


def schedule_activation(document_id: str, version: int, run_after: datetime) -> str:
    return store.add_job(
        JOB_KIND,
        {"document_id": document_id, "version": version},
        run_after=run_after.isoformat(),
    )


def _apply(document: dict, target: dict, at: datetime) -> dict:
    current = store.active_version(str(document["id"]))
    if current and current["id"] != target["id"]:
        store.update_version(int(current["id"]), status="superseded")
    store.update_version(
        int(target["id"]), status="active", activated_at=at.isoformat()
    )
    store.update_document(
        str(document["id"]),
        status="active",
        current_version=target["version"],
        content_hash=target["content_hash"],
        title=target.get("title"),
        access_level=target.get("access_level"),
        product=target.get("product"),
        region=target.get("region"),
        effective_at=target.get("effective_at"),
        expires_at=target.get("expires_at"),
        quality=_quality_score(target),
        health_failures=0,
        last_health_at=at.isoformat(),
    )
    store.audit(
        "kb.activated",
        {"document_id": document["id"], "version": target["version"]},
    )
    return {
        "status": "active",
        "document_id": document["id"],
        "version": target["version"],
        "content_hash": target["content_hash"],
        "activated_at": at.isoformat(),
    }


def _quality_score(version: dict) -> float | None:
    raw = version.get("quality")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value.get("score") if isinstance(value, dict) else None


def activate(
    document_id: str,
    version: int | None = None,
    force: bool = False,
    at: datetime | None = None,
    schedule_job: bool = True,
) -> dict:
    """Activate a pending version, but only inside the maintenance window."""
    at = at or clock.now()
    document = store.get_document(document_id)
    if document is None:
        raise KeyError(f"unknown document: {document_id}")
    target = (
        store.get_version(document_id, int(version))
        if version is not None
        else store.latest_version(document_id)
    )
    if target is None:
        raise ValueError(f"version not found: {document_id} v{version}")
    if target["status"] == "active":
        return {
            "status": "active",
            "document_id": document_id,
            "version": target["version"],
            "already": True,
        }
    if target["status"] == "rejected":
        raise ValueError(f"version {target['version']} was rejected at ingestion")

    if not force and not in_maintenance_window(at):
        run_after = next_maintenance_start(at)
        job_id = (
            schedule_activation(document_id, int(target["version"]), run_after)
            if schedule_job
            else None
        )
        store.audit(
            "kb.activation_scheduled",
            {
                "document_id": document_id,
                "version": target["version"],
                "run_after": run_after.isoformat(),
            },
        )
        return {
            "status": "scheduled",
            "document_id": document_id,
            "version": target["version"],
            "run_after": run_after.isoformat(),
            "job_id": job_id,
        }

    return _apply(document, target, at)


def rollback(
    document_id: str,
    to_version: int | None = None,
    reason: str = "manual",
    at: datetime | None = None,
) -> dict:
    at = at or clock.now()
    document = store.get_document(document_id)
    if document is None:
        raise KeyError(f"unknown document: {document_id}")
    current = store.active_version(document_id)
    if current is None:
        raise ValueError("document has no active version")

    if to_version is not None:
        target = store.get_version(document_id, int(to_version))
    else:
        history = [
            row
            for row in store.versions(document_id)
            if row["version"] < current["version"]
            and row["status"] in ("superseded", "rolled_back", "pending")
        ]
        target = history[-1] if history else None

    if target is None or target["id"] == current["id"]:
        store.audit("kb.rollback_none", {"document_id": document_id, "reason": reason})
        return {"status": "no_target", "document_id": document_id}

    store.update_version(int(current["id"]), status="rolled_back")
    store.update_version(
        int(target["id"]),
        status="active",
        activated_at=(target.get("activated_at") or at.isoformat()),
    )
    store.update_document(
        document_id,
        status="active",
        current_version=target["version"],
        content_hash=target["content_hash"],
        title=target.get("title"),
        access_level=target.get("access_level"),
        product=target.get("product"),
        region=target.get("region"),
        effective_at=target.get("effective_at"),
        expires_at=target.get("expires_at"),
        quality=_quality_score(target),
        health_failures=0,
        last_health_at=at.isoformat(),
    )
    store.audit(
        "kb.rolled_back",
        {
            "document_id": document_id,
            "from_version": current["version"],
            "to_version": target["version"],
            "reason": reason,
        },
    )
    return {
        "status": "rolled_back",
        "document_id": document_id,
        "from_version": current["version"],
        "to_version": target["version"],
        "reason": reason,
        "at": at.isoformat(),
    }


# --------------------------------------------------------------------------- health


def _healthy(version: dict | None) -> tuple[bool, str | None]:
    if version is None:
        return False, "no_active_version"
    content = str(version.get("content") or "")
    if len(content.strip()) < 50:
        return False, "content_too_short"
    if content_hash(content) != version.get("content_hash"):
        return False, "hash_mismatch"
    if _BROKEN.search(content):
        return False, "broken_marker"
    return True, None


def health_check(at: datetime | None = None) -> dict:
    at = at or clock.now()
    interval = int(config.get("knowledge.health_check_minutes", 5))
    limit = int(config.get("knowledge.health_fail_limit", 1))
    results: list[dict] = []

    for document in store.list_documents(status="active"):
        last = document.get("last_health_at")
        if last:
            moment = datetime.fromisoformat(last)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=clock.tz())
            if at - moment < timedelta(minutes=interval):
                continue

        version = store.active_version(str(document["id"]))
        ok, problem = _healthy(version)
        store.update_document(str(document["id"]), last_health_at=at.isoformat())

        if ok:
            if int(document.get("health_failures") or 0):
                store.update_document(str(document["id"]), health_failures=0)
            results.append({"document_id": document["id"], "status": "healthy"})
            continue

        failures = int(document.get("health_failures") or 0) + 1
        store.update_document(str(document["id"]), health_failures=failures)
        entry = {
            "document_id": document["id"],
            "status": "failed",
            "problem": problem,
            "failures": failures,
        }
        if failures >= limit:
            outcome = rollback(str(document["id"]), reason="health_check", at=at)
            entry["rollback"] = outcome
            if outcome["status"] == "no_target":
                store.update_document(str(document["id"]), status="retired")
                entry["status"] = "retired"
        results.append(entry)
        store.audit(
            "kb.health_failed",
            {"document_id": document["id"], "problem": problem, "failures": failures},
        )

    return {"at": at.isoformat(), "checked": len(results), "results": results}


# --------------------------------------------------------------------------- jobs


def _retry_delays() -> list[int]:
    return [
        int(value) for value in config.get_list("knowledge.retry_minutes", [15, 30, 60])
    ]


def process_jobs(at: datetime | None = None) -> dict:
    """Run due activation jobs; failures retry at 15, 30 and 60 minutes."""
    at = at or clock.now()
    delays = _retry_delays()
    processed: list[dict] = []

    for job in store.due_jobs(at, kind=JOB_KIND):
        payload = {}
        try:
            payload = json.loads(job.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        store.update_job(str(job["id"]), status="running")
        try:
            outcome = activate(
                str(payload.get("document_id")),
                payload.get("version"),
                force=False,
                at=at,
                schedule_job=False,
            )
            if outcome["status"] == "scheduled":
                store.update_job(
                    str(job["id"]),
                    status="pending",
                    run_after=outcome["run_after"],
                )
            else:
                store.update_job(str(job["id"]), status="done")
            processed.append(
                {"job_id": job["id"], "status": outcome["status"], "outcome": outcome}
            )
        except Exception as exc:  # activation failures are retried, never lost
            attempts = int(job.get("attempts") or 0) + 1
            message = str(exc)[:300]
            if attempts <= len(delays):
                retry_at = at + timedelta(minutes=delays[attempts - 1])
                store.update_job(
                    str(job["id"]),
                    status="pending",
                    attempts=attempts,
                    run_after=retry_at.isoformat(),
                    last_error=message,
                )
            else:
                store.update_job(
                    str(job["id"]),
                    status="failed",
                    attempts=attempts,
                    last_error=message,
                )
            store.audit(
                "kb.job_failed",
                {"job_id": job["id"], "attempts": attempts, "error": message},
            )
            processed.append(
                {
                    "job_id": job["id"],
                    "status": "retry" if attempts <= len(delays) else "failed",
                    "attempts": attempts,
                    "error": message,
                }
            )

    return {"at": at.isoformat(), "processed": processed}


def run_maintenance(at: datetime | None = None) -> dict:
    """The 5-minute timer: activation jobs first, then health checks."""
    at = at or clock.now()
    jobs = process_jobs(at)
    health = health_check(at)
    return {"at": at.isoformat(), "jobs": jobs, "health": health}


# --------------------------------------------------------------------------- reads


def active_documents(requester: str = "customer") -> list[dict]:
    documents = store.list_documents(status="active")
    return [row for row in documents if can_read(row, requester)]


def document_detail(document_id: str, requester: str = "customer") -> dict | None:
    document = store.get_document(document_id)
    if document is None or not can_read(document, requester):
        return None
    version = store.active_version(document_id)
    return {
        **document,
        "content": security.mask((version or {}).get("content") or ""),
        "versions": [
            {
                "version": row["version"],
                "status": row["status"],
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
            }
            for row in store.versions(document_id)
        ],
    }


_STOP = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "what",
    "when",
    "where",
    "how",
    "why",
    "who",
    "are",
    "is",
    "was",
    "were",
    "will",
    "can",
    "does",
    "not",
}


def search(query: str, requester: str = "customer", limit: int = 5) -> list[dict]:
    terms = {
        term for term in re.findall(r"[a-z0-9]{3,}", query.lower()) if term not in _STOP
    }
    if not terms:
        return []

    matches: list[dict] = []
    for document in active_documents(requester):
        version = store.active_version(str(document["id"])) or {}
        text = f"{document.get('title') or ''} {version.get('content') or ''}".lower()
        overlap = terms & set(re.findall(r"[a-z0-9]{3,}", text))
        if not overlap:
            continue
        body = str(version.get("content") or "")
        index = body.lower().find(sorted(overlap)[0])
        snippet = body[max(0, index - 60) : index + 200] if index >= 0 else body[:200]
        matches.append(
            {
                "document_id": document["id"],
                "title": document.get("title"),
                "access_level": document.get("access_level"),
                "version": document.get("current_version"),
                "region": document.get("region"),
                "score": round(len(overlap) / len(terms), 3),
                "snippet": security.mask(snippet).strip(),
            }
        )
    matches.sort(key=lambda row: row["score"], reverse=True)
    return matches[:limit]


def stats() -> dict:
    documents = store.list_documents()
    document_counts: dict[str, int] = {}
    for row in documents:
        document_counts[row["status"]] = document_counts.get(row["status"], 0) + 1

    version_counts: dict[str, int] = {}
    for row in documents:
        for version in store.versions(str(row["id"])):
            version_counts[version["status"]] = (
                version_counts.get(version["status"], 0) + 1
            )

    last_scan = next(
        (row for row in store.audit_tail(200) if row["action"] == "kb.scan"), None
    )
    return {
        "documents": {"total": len(documents), "by_status": document_counts},
        "versions": version_counts,
        "jobs": store.jobs_summary(),
        "maintenance": {
            "window": [value.strftime("%H:%M") for value in maintenance_window()],
            "open": in_maintenance_window(),
            "next_start": next_maintenance_start().isoformat(),
        },
        "quality_gate": float(config.get("knowledge.quality_gate_min_score", 0.6)),
        "retry_minutes": _retry_delays(),
        "health_check_minutes": int(config.get("knowledge.health_check_minutes", 5)),
        "last_scan": last_scan,
    }
