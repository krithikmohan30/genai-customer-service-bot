"""Persistence helpers. SQL lives here, not in route handlers or modules."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from . import db
from .core import clock, config, security


def _dumps(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


# --------------------------------------------------------------------------- sessions


def get_session(session_id: str) -> dict | None:
    return db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))


def create_session(customer_id: str | None = None, language: str | None = None) -> dict:
    session_id = uuid.uuid4().hex
    now = clock.now_iso()
    db.execute(
        "INSERT INTO sessions (id, customer_id, language, status, created_at, last_seen_at)"
        " VALUES (?, ?, ?, 'active', ?, ?)",
        (session_id, customer_id, language, now, now),
    )
    return get_session(session_id) or {}


def get_or_create_session(
    session_id: str | None, customer_id: str | None = None
) -> dict:
    if session_id:
        existing = get_session(session_id)
        if existing:
            touch_session(session_id)
            return existing
    return create_session(customer_id)


def touch_session(session_id: str, language: str | None = None) -> None:
    if language:
        db.execute(
            "UPDATE sessions SET last_seen_at = ?, language = ? WHERE id = ?",
            (clock.now_iso(), language, session_id),
        )
    else:
        db.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (clock.now_iso(), session_id),
        )


def set_session_summary(session_id: str, summary: str) -> None:
    db.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id))


# --------------------------------------------------------------------------- messages


def add_message(
    session_id: str,
    role: str,
    content: str,
    triage: dict | None = None,
    language: str | None = None,
) -> int:
    """Store a turn. `masked` is the only copy safe to show in logs or handoffs."""
    return db.execute(
        "INSERT INTO messages (session_id, role, content, masked, language, triage, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            role,
            content,
            security.mask(content),
            language,
            _dumps(triage),
            clock.now_iso(),
        ),
    )


def recent_messages(session_id: str, limit: int = 10) -> list[dict]:
    rows = db.query(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return list(reversed(rows))


def customer_texts(session_id: str, limit: int = 10) -> list[str]:
    return [
        row["content"]
        for row in recent_messages(session_id, limit)
        if row["role"] == "customer"
    ]


# --------------------------------------------------------------------------- escalations


def add_escalation(
    session_id: str,
    reason: str,
    condition: str,
    summary: str,
    queue: str | None = None,
    risk_level: str | None = None,
    sentiment: str | None = None,
    scheduled_for: str | None = None,
) -> int:
    return db.execute(
        "INSERT INTO escalations (session_id, reason, condition, summary, queue,"
        " risk_level, sentiment, scheduled_for, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            reason,
            condition,
            security.mask(summary),
            queue,
            risk_level,
            sentiment,
            scheduled_for,
            clock.now_iso(),
        ),
    )


def has_open_escalation(session_id: str, reason: str | None = None) -> bool:
    if reason:
        row = db.query_one(
            "SELECT id FROM escalations WHERE session_id = ? AND reason = ?"
            " AND resolved_at IS NULL",
            (session_id, reason),
        )
    else:
        row = db.query_one(
            "SELECT id FROM escalations WHERE session_id = ? AND resolved_at IS NULL",
            (session_id,),
        )
    return row is not None


def escalations(session_id: str | None = None) -> list[dict]:
    if session_id:
        return db.query(
            "SELECT * FROM escalations WHERE session_id = ? ORDER BY id", (session_id,)
        )
    return db.query("SELECT * FROM escalations ORDER BY id")


def sessions_with_messages_since(since_iso: str) -> list[str]:
    rows = db.query(
        "SELECT DISTINCT session_id FROM messages WHERE created_at >= ?", (since_iso,)
    )
    return [row["session_id"] for row in rows]


def mark_escalation_resolved(escalation_id: int) -> None:
    db.execute(
        "UPDATE escalations SET resolved_at = ? WHERE id = ?",
        (clock.now_iso(), escalation_id),
    )


# --------------------------------------------------------------------------- tickets

TICKET_FIELDS = {
    "subject",
    "issue",
    "category",
    "severity",
    "impact",
    "priority",
    "status",
    "team",
    "assignee",
    "group_id",
    "duplicate_of",
    "route_reason",
    "scheduled_for",
    "sla_due_at",
    "sla_warned_at",
    "sla_breached_at",
    "detail",
}


def create_ticket(
    session_id: str | None = None,
    subject: str = "",
    issue: str = "",
    severity: str = "medium",
    priority: float = 0.0,
    team: str | None = None,
    assignee: str | None = None,
    detail: dict | None = None,
    created_at: str | None = None,
    customer_id: str | None = None,
    category: str | None = None,
    impact: str | None = None,
    group_id: str | None = None,
    duplicate_of: str | None = None,
    route_reason: str | None = None,
    scheduled_for: str | None = None,
) -> dict:
    ticket_id = "TKT-" + uuid.uuid4().hex[:8].upper()
    now = created_at or clock.now_iso()
    db.execute(
        "INSERT INTO tickets (id, session_id, customer_id, subject, issue, category,"
        " severity, impact, priority, status, team, assignee, group_id, duplicate_of,"
        " route_reason, scheduled_for, created_at, updated_at, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticket_id,
            session_id,
            customer_id,
            subject,
            issue,
            category,
            severity,
            impact,
            priority,
            team,
            assignee,
            group_id,
            duplicate_of,
            route_reason,
            scheduled_for,
            now,
            now,
            _dumps(detail),
        ),
    )
    return get_ticket(ticket_id) or {}


def get_ticket(ticket_id: str) -> dict | None:
    return db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))


def open_tickets() -> list[dict]:
    return db.query(
        "SELECT * FROM tickets WHERE status NOT IN ('closed', 'resolved') ORDER BY created_at"
    )


def open_tickets_for(session_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM tickets WHERE session_id = ?"
        " AND status NOT IN ('closed', 'resolved') ORDER BY created_at",
        (session_id,),
    )


def open_tickets_by_team(team: str) -> list[dict]:
    return db.query(
        "SELECT * FROM tickets WHERE team = ? AND status NOT IN ('closed', 'resolved')",
        (team,),
    )


def search_tickets(
    status: str | None = None,
    team: str | None = None,
    group: str | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if team:
        clauses.append("team = ?")
        params.append(team)
    if group:
        clauses.append("(group_id = ? OR id = ?)")
        params.extend([group, group])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return db.query(
        f"SELECT * FROM tickets {where} ORDER BY priority DESC, created_at LIMIT ?",
        (*params, int(limit)),
    )


def update_ticket(ticket_id: str, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in TICKET_FIELDS}
    if not updates:
        return get_ticket(ticket_id)
    if isinstance(updates.get("detail"), dict):
        updates["detail"] = _dumps(updates["detail"])
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE tickets SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), clock.now_iso(), ticket_id),
    )
    return get_ticket(ticket_id)


# --------------------------------------------------------------------------- jobs

JOB_FIELDS = {"status", "payload", "attempts", "run_after", "last_error"}


def add_job(
    kind: str,
    payload: dict | None = None,
    run_after: str | None = None,
    status: str = "pending",
) -> str:
    job_id = "JOB-" + uuid.uuid4().hex[:8].upper()
    now = clock.now_iso()
    db.execute(
        "INSERT INTO jobs (id, kind, status, payload, attempts, run_after,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (job_id, kind, status, _dumps(payload), run_after or now, now, now),
    )
    return job_id


def get_job(job_id: str) -> dict | None:
    return db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def due_jobs(
    at: datetime | None = None, kind: str | None = None, limit: int = 50
) -> list[dict]:
    at_iso = (at or clock.now()).isoformat()
    if kind:
        return db.query(
            "SELECT * FROM jobs WHERE status = 'pending' AND kind = ?"
            " AND (run_after IS NULL OR run_after <= ?) ORDER BY run_after LIMIT ?",
            (kind, at_iso, int(limit)),
        )
    return db.query(
        "SELECT * FROM jobs WHERE status = 'pending'"
        " AND (run_after IS NULL OR run_after <= ?) ORDER BY run_after LIMIT ?",
        (at_iso, int(limit)),
    )


def update_job(job_id: str, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in JOB_FIELDS}
    if not updates:
        return get_job(job_id)
    if isinstance(updates.get("payload"), dict):
        updates["payload"] = _dumps(updates["payload"])
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), clock.now_iso(), job_id),
    )
    return get_job(job_id)


def jobs_summary() -> dict:
    rows = db.query("SELECT status, COUNT(*) AS total FROM jobs GROUP BY status")
    return {row["status"]: row["total"] for row in rows}


# --------------------------------------------------------------------------- knowledge


DOCUMENT_FIELDS = {
    "title",
    "path",
    "product",
    "region",
    "access_level",
    "effective_at",
    "expires_at",
    "content_hash",
    "quality",
    "status",
    "current_version",
    "health_failures",
    "last_health_at",
}

VERSION_FIELDS = {
    "title",
    "content",
    "content_hash",
    "access_level",
    "product",
    "region",
    "effective_at",
    "expires_at",
    "quality",
    "pii_masked",
    "status",
    "activated_at",
}


def add_document(document_id: str, path: str, **fields) -> dict:
    now = clock.now_iso()
    values = {
        "id": document_id,
        "path": path,
        "status": "active",
        "current_version": 1,
        "created_at": now,
        "updated_at": now,
        **fields,
    }
    db.execute(
        "INSERT INTO documents (id, path, title, product, region, access_level,"
        " effective_at, expires_at, content_hash, quality, status, current_version,"
        " health_failures, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            values["id"],
            values["path"],
            values.get("title"),
            values.get("product"),
            values.get("region"),
            values.get("access_level"),
            values.get("effective_at"),
            values.get("expires_at"),
            values.get("content_hash"),
            values.get("quality"),
            values["status"],
            values["current_version"],
            values["created_at"],
            values["updated_at"],
        ),
    )
    return get_document(document_id) or {}


def get_document(document_id: str) -> dict | None:
    return db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))


def get_document_by_path(path: str) -> dict | None:
    return db.query_one("SELECT * FROM documents WHERE path = ?", (path,))


def list_documents(
    status: str | None = None, access_levels: list[str] | None = None
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if access_levels:
        placeholders = ", ".join("?" for _ in access_levels)
        clauses.append(f"access_level IN ({placeholders})")
        params.extend(access_levels)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return db.query(
        f"SELECT * FROM documents {where} ORDER BY updated_at DESC", tuple(params)
    )


def update_document(document_id: str, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in DOCUMENT_FIELDS}
    if not updates:
        return get_document(document_id)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE documents SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), clock.now_iso(), document_id),
    )
    return get_document(document_id)


def add_document_version(
    document_id: str,
    version: int,
    content: str,
    content_hash: str,
    status: str = "pending",
    **fields,
) -> int:
    values = {key: value for key, value in fields.items() if key in VERSION_FIELDS}
    columns = [
        "document_id",
        "version",
        "content",
        "content_hash",
        "status",
        "created_at",
        *values.keys(),
    ]
    params = [
        document_id,
        version,
        content,
        content_hash,
        status,
        clock.now_iso(),
        *values.values(),
    ]
    placeholders = ", ".join("?" for _ in columns)
    db.execute(
        f"INSERT INTO document_versions ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(params),
    )
    row = db.query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND version = ?",
        (document_id, version),
    )
    return int(row["id"]) if row else 0


def get_version(document_id: str, version: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND version = ?",
        (document_id, version),
    )


def versions(document_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version",
        (document_id,),
    )


def latest_version(document_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM document_versions WHERE document_id = ?"
        " ORDER BY version DESC LIMIT 1",
        (document_id,),
    )


def active_version(document_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND status = 'active'"
        " ORDER BY version DESC LIMIT 1",
        (document_id,),
    )


def find_version_by_hash(content_hash: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM document_versions WHERE content_hash = ?"
        " AND status IN ('pending', 'active') ORDER BY version DESC LIMIT 1",
        (content_hash,),
    )


def update_version(version_id: int, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in VERSION_FIELDS}
    if not updates:
        return db.query_one(
            "SELECT * FROM document_versions WHERE id = ?", (version_id,)
        )
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE document_versions SET {assignments} WHERE id = ?",
        (*updates.values(), version_id),
    )
    return db.query_one("SELECT * FROM document_versions WHERE id = ?", (version_id,))


# --------------------------------------------------------------------------- uploads

UPLOAD_FIELDS = {
    "session_id",
    "filename",
    "mime",
    "path",
    "size_bytes",
    "message",
    "sha256",
    "status",
    "extracted",
    "reason",
    "attempts",
    "processing_ms",
    "processed_at",
    "delete_at",
}


def add_upload(
    filename: str,
    session_id: str | None = None,
    mime: str | None = None,
    path: str | None = None,
    size_bytes: int = 0,
    message: str | None = None,
    sha256: str | None = None,
    status: str = "received",
    reason: str | None = None,
    delete_at: str | None = None,
) -> dict:
    upload_id = "UP-" + uuid.uuid4().hex[:8].upper()
    db.execute(
        "INSERT INTO uploads (id, session_id, filename, mime, path, size_bytes,"
        " message, sha256, status, reason, created_at, delete_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            upload_id,
            session_id,
            filename,
            mime,
            path,
            int(size_bytes),
            message,
            sha256,
            status,
            reason,
            clock.now_iso(),
            delete_at,
        ),
    )
    return get_upload(upload_id) or {}


def get_upload(upload_id: str) -> dict | None:
    return db.query_one("SELECT * FROM uploads WHERE id = ?", (upload_id,))


def list_uploads(
    session_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return db.query(
        f"SELECT * FROM uploads {where} ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    )


def update_upload(upload_id: str, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in UPLOAD_FIELDS}
    if not updates:
        return get_upload(upload_id)
    if isinstance(updates.get("extracted"), dict):
        updates["extracted"] = _dumps(updates["extracted"])
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE uploads SET {assignments} WHERE id = ?",
        (*updates.values(), upload_id),
    )
    return get_upload(upload_id)


def expired_uploads(at_iso: str) -> list[dict]:
    return db.query(
        "SELECT * FROM uploads WHERE status != 'deleted'"
        " AND delete_at IS NOT NULL AND delete_at <= ? ORDER BY delete_at",
        (at_iso,),
    )


# --------------------------------------------------------------------------- audit


def audit(action: str, detail: dict | str | None = None, actor: str = "system") -> None:
    db.execute(
        "INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (
            clock.now_iso(),
            actor,
            action,
            detail if isinstance(detail, str) else _dumps(detail),
        ),
    )


def audit_tail(limit: int = 50) -> list[dict]:
    return db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
