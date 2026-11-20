"""Task 1 - escalation engine.

Five independent triggers, evaluated in this order:

  1. high risk                  account compromise, duplicate payment, fraud, legal
  2. urgent, outside hours      goes to the on-call queue
  3. repeated negative          N negative turns in a row
  4. complaint, outside hours   scheduled for the next working day
  5. unresolved for 15 minutes  negative conversation nobody has closed

Sentiment and risk are separate inputs on purpose: "No rush, but someone changed
my password" is neutral in tone and still escalates on risk. Every trigger records
the reason, the condition that fired, and a masked conversation summary.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .. import db, store
from . import clock, config, security
from . import calendar as work_calendar

NEGATIVE_LABELS = {"negative", "frustrated", "sarcastic"}


def _parse(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=clock.tz())


def _queues() -> dict:
    return config.get("escalation.queues", {}) or {}


def streak_threshold() -> int:
    return int(config.get("sentiment.repeated_negative_threshold", 3))


def unresolved_minutes() -> int:
    return int(config.get("escalation.unresolved_minutes", 15))


def _label(message_row: dict) -> str:
    payload = message_row.get("triage") or "{}"
    try:
        return str(json.loads(payload).get("sentiment", {}).get("label", ""))
    except (json.JSONDecodeError, AttributeError):
        return ""


def negative_streak(session_id: str) -> tuple[int, str | None]:
    """Consecutive negative customer turns, newest first, and when the streak began."""
    rows = [
        row
        for row in store.recent_messages(session_id, 20)
        if row["role"] == "customer"
    ]
    count = 0
    started_at: str | None = None
    for row in reversed(rows):
        if _label(row) in NEGATIVE_LABELS:
            count += 1
            started_at = row["created_at"]
        else:
            break
    return count, started_at


def conversation_summary(session_id: str, limit: int = 4) -> str:
    """Masked one-line transcript for the handoff record."""
    parts: list[str] = []
    for row in store.recent_messages(session_id, limit * 2):
        if row["role"] != "customer":
            continue
        label = _label(row) or "neutral"
        text = security.mask(row.get("masked") or row["content"])[:120]
        parts.append(f"{label}: {text}")
    return " | ".join(parts[-limit:])[:500]


def evaluate(session_id: str, triage: dict, at: datetime | None = None) -> dict | None:
    """Decide whether this message triggers an escalation. No side effects."""
    at = at or clock.now()
    open_now = work_calendar.is_open(at)
    queues = _queues()
    risk = triage.get("risk", {})
    label = str(triage.get("sentiment", {}).get("label", "neutral"))
    summary = conversation_summary(session_id)

    if risk.get("level") == "high" and risk.get("categories"):
        category = str(risk["categories"][0])
        return {
            "reason": category,
            "condition": f"risk.level=high AND category={category}",
            "queue": queues.get("trust_safety", "trust_and_safety"),
            "sentiment": label,
            "summary": summary,
            "risk_level": "high",
            "scheduled_for": None,
        }

    if label == "urgent" and not open_now:
        return {
            "reason": "after_hours_urgent",
            "condition": "sentiment=urgent AND business_hours=closed",
            "queue": queues.get("on_call", "on_call_support"),
            "sentiment": label,
            "summary": summary,
            "risk_level": risk.get("level"),
            "scheduled_for": None,
        }

    count, _ = negative_streak(session_id)
    threshold = streak_threshold()
    if count >= threshold:
        return {
            "reason": "repeated_negative",
            "condition": f"negative_streak>={threshold}",
            "queue": queues.get("supervisor", "supervisor_queue"),
            "sentiment": label,
            "summary": summary,
            "risk_level": risk.get("level"),
            "scheduled_for": None,
        }

    if label in NEGATIVE_LABELS and not open_now:
        return {
            "reason": "after_hours_complaint",
            "condition": "sentiment in (negative,frustrated,sarcastic) AND business_hours=closed",
            "queue": queues.get("next_day", "next_working_day"),
            "sentiment": label,
            "summary": summary,
            "risk_level": risk.get("level"),
            "scheduled_for": work_calendar.next_working_start(at).isoformat(),
        }

    return None


def activate(session_id: str, decision: dict) -> dict | None:
    """Persist the escalation, unless the same reason is already open."""
    if store.has_open_escalation(session_id, decision["reason"]):
        return None
    record_id = store.add_escalation(
        session_id=session_id,
        reason=decision["reason"],
        condition=decision["condition"],
        summary=decision["summary"],
        queue=decision.get("queue"),
        risk_level=decision.get("risk_level"),
        sentiment=decision.get("sentiment"),
        scheduled_for=decision.get("scheduled_for"),
    )
    store.audit(
        "escalation.created",
        {
            "session_id": session_id,
            "reason": decision["reason"],
            "condition": decision["condition"],
            "queue": decision.get("queue"),
        },
    )
    return db.query_one("SELECT * FROM escalations WHERE id = ?", (record_id,))


def handle(session_id: str, triage: dict, at: datetime | None = None) -> dict | None:
    decision = evaluate(session_id, triage, at)
    if decision is None:
        return None
    return activate(session_id, decision)


def check_unresolved(session_id: str, at: datetime | None = None) -> dict | None:
    """Escalate a negative conversation that has stayed open past the timer."""
    if store.has_open_escalation(session_id):
        return None

    count, started_at = negative_streak(session_id)
    started = _parse(started_at)
    if count < 1 or started is None:
        return None

    at = at or clock.now()
    elapsed = int((at - started).total_seconds() // 60)
    limit = unresolved_minutes()
    if elapsed < limit:
        return None

    decision = {
        "reason": "unresolved_15m",
        "condition": f"unresolved_minutes>={limit}",
        "queue": _queues().get("supervisor", "supervisor_queue"),
        "sentiment": "negative",
        "summary": conversation_summary(session_id),
        "risk_level": None,
        "scheduled_for": None,
    }
    return activate(session_id, decision)


def sweep(at: datetime | None = None) -> list[dict]:
    """Check every recently active session. Called by /admin/sweep and on each turn."""
    at = at or clock.now()
    since = (at - timedelta(hours=24)).isoformat()
    created: list[dict] = []
    for session_id in store.sessions_with_messages_since(since):
        record = check_unresolved(session_id, at)
        if record:
            created.append(record)
    return created
