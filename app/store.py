"""Persistence helpers. SQL lives here, not in route handlers or modules."""

from __future__ import annotations

import json
import uuid

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
