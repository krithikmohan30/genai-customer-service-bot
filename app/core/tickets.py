"""Task 2b - structured tickets.

The conversation is unstructured; a ticket is not. This module turns one customer
message plus its triage result into a record with:

    subject, issue, category, severity, impact, priority
    team, route reason, waiting state
    SLA clock (step 4)
    duplicate/group handling
    a masked handoff summary for the next agent

Priority is a weighted 0-100 score. The weights live in `priority.weights`, so a
runtime change (severity matters more today, wait time matters less) is applied
on the next calculation.

Nothing here invents data. Every field is derived from the message, the triage
result, the session or the configuration.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .. import store
from . import clock, config, security, triage as triage_engine
from . import routing, sla

TICKET_INTENTS = {
    "billing_dispute",
    "refund_request",
    "access_issue",
    "account_security",
    "legal_escalation",
    "complaint",
    "urgent_issue",
}

NEGATIVE_LABELS = {"negative", "frustrated", "sarcastic"}

_COMPLAINT_SIGNALS = re.compile(
    r"\bdelay(?:ed|s)?\b|\bnot\s+(?:received|working|delivered)\b"
    r"|\bfail(?:ed|ing|ure)?\b|\bbroken\b|\bwrong\b|\bmissing\b"
    r"|\btwice\b|\bdouble\b|\bduplicate\b|\bstill\s+(?:waiting|not)\b"
    r"|\bcharged\s+again\b|\bno\s+(?:response|reply)\b",
    re.I,
)

SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.75, "medium": 0.5, "low": 0.25}
SENTIMENT_WEIGHT = {
    "urgent": 1.0,
    "frustrated": 0.85,
    "negative": 0.7,
    "sarcastic": 0.7,
    "neutral": 0.3,
    "positive": 0.1,
}
IMPACT_WEIGHT = {"organization": 1.0, "team": 0.7, "individual": 0.3}

_ORGANIZATION_IMPACT = re.compile(
    r"\b(?:all|every|entire|whole)\s+(?:users|customers|team|company|office|students)\b"
    r"|\boutage\b|\bproduction\s+down\b|\bcompany[- ]wide\b",
    re.I,
)
_TEAM_IMPACT = re.compile(
    r"\b(?:my|our)\s+(?:team|office|class|department|colleagues)\b"
    r"|\bmultiple\s+(?:users|customers|people)\b|\beveryone\s+in\b",
    re.I,
)
_ENTITY_KEYS = ("order_id", "error_code", "amount", "product")


# --------------------------------------------------------------------------- helpers


def _detail(ticket: dict) -> dict:
    raw = ticket.get("detail")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_sentence(text: str, limit: int = 110) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"^(.{10,120}?[.!?])(?:\s|$)", cleaned)
    subject = match.group(1) if match else cleaned
    if len(subject) > limit:
        subject = subject[: limit - 1].rstrip() + "…"
    return subject


def _impact(message: str) -> str:
    if _ORGANIZATION_IMPACT.search(message):
        return "organization"
    if _TEAM_IMPACT.search(message):
        return "team"
    return "individual"


def _category(intent: str, label: str) -> str:
    if intent in ("billing_question",):
        return "billing_question"
    if intent in TICKET_INTENTS:
        return intent
    return "complaint" if label in NEGATIVE_LABELS else "general_question"


def _severity(risk_level: str, label: str, category: str) -> str:
    if risk_level == "high":
        return "critical"
    if label == "urgent" or risk_level == "medium":
        return "high"
    if category in ("account_security", "legal_escalation"):
        return "high"
    if label in NEGATIVE_LABELS:
        return "medium"
    return "low"


def fingerprint(category: str, entities: dict, subject: str) -> str:
    """Two tickets with the same identity are the same ticket.

    An order id or error code is strong evidence. Without one, the normalised
    subject is used, which is weak but only applied inside a short window.
    """
    identity = [
        f"{key}={str(entities.get(key)).lower()}"
        for key in _ENTITY_KEYS
        if entities.get(key)
    ]
    if identity:
        return f"{category}|" + "|".join(sorted(identity))
    normalised = re.sub(r"[^a-z0-9 ]", "", subject.lower())
    return f"{category}|text:{normalised[:80]}"


def _wait_minutes(created_at: str | None) -> int:
    if not created_at:
        return 0
    moment = datetime.fromisoformat(created_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.tz())
    return max(0, int((clock.now() - moment).total_seconds() // 60))


# --------------------------------------------------------------------------- scoring


def score(
    draft: dict, at: datetime | None = None, created_at: str | None = None
) -> dict:
    """Weighted priority, 0-100, with the component values kept for explanation."""
    at = at or clock.now()
    weights = config.get("priority.weights", {}) or {}
    wait_full = max(1, int(config.get("priority.wait_full_minutes", 120)))

    severity = str(draft.get("severity") or "medium").lower()
    label = str((draft.get("sentiment") or {}).get("label") or "neutral").lower()
    impact = str(draft.get("impact") or "individual").lower()

    wait_minutes = (
        _wait_minutes(created_at) if created_at else int(draft.get("_wait", 0) or 0)
    )

    sla_pressure = 0.0
    if draft.get("id"):
        position = sla.state(store.get_ticket(str(draft["id"])) or draft, at)
        allowed = max(1, int(position["allowed_minutes"]))
        sla_pressure = min(1.0, position["consumed_minutes"] / allowed)

    parts = {
        "severity": float(weights.get("severity", 40))
        * SEVERITY_WEIGHT.get(severity, 0.5),
        "sentiment": float(weights.get("sentiment", 20))
        * SENTIMENT_WEIGHT.get(label, 0.3),
        "impact": float(weights.get("impact", 20)) * IMPACT_WEIGHT.get(impact, 0.3),
        "wait": float(weights.get("wait", 10)) * min(1.0, wait_minutes / wait_full),
        "sla": float(weights.get("sla", 10)) * sla_pressure,
    }
    total = round(sum(parts.values()), 1)
    return {
        "score": max(0.0, min(100.0, total)),
        "components": {key: round(value, 2) for key, value in parts.items()},
        "wait_minutes": wait_minutes,
        "inputs": {"severity": severity, "sentiment": label, "impact": impact},
    }


# --------------------------------------------------------------------------- extraction


def extract(
    session_id: str | None,
    message: str,
    result: dict,
    customer_id: str | None = None,
) -> dict:
    """Turn one message into the fields a ticket needs."""
    masked = security.mask(message).strip()
    sentiment = result.get("sentiment") or {}
    risk = result.get("risk") or {}
    entities = result.get("entities") or {}
    label = str(sentiment.get("label") or "neutral").lower()
    category = _category(str(result.get("intent") or ""), label)
    severity = _severity(str(risk.get("level") or "none"), label, category)
    impact = _impact(message)
    subject = _first_sentence(masked)

    draft = {
        "session_id": session_id,
        "customer_id": customer_id,
        "subject": subject,
        "issue": masked[:4000],
        "category": category,
        "severity": severity,
        "impact": impact,
        "sentiment": sentiment,
        "language": result.get("language"),
        "risk": risk,
        "entities": entities,
        "intent": result.get("intent"),
        "fingerprint": fingerprint(category, entities, subject),
    }
    draft["priority_detail"] = score(draft)
    draft["priority"] = draft["priority_detail"]["score"]
    return draft


# --------------------------------------------------------------------------- dedupe


def _within_window(ticket: dict, minutes: int) -> bool:
    created = ticket.get("created_at")
    if not created:
        return False
    moment = datetime.fromisoformat(created)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.tz())
    return clock.now() - moment <= timedelta(minutes=minutes)


def find_duplicate(draft: dict) -> dict | None:
    """An open ticket that this message belongs to, if any."""
    window = int(config.get("tickets.duplicate_window_minutes", 30))
    for ticket in store.open_tickets():
        detail = _detail(ticket)
        if detail.get("fingerprint") != draft.get("fingerprint"):
            continue
        if draft.get("session_id") and ticket.get("session_id") == draft["session_id"]:
            return ticket
        if (
            draft.get("customer_id")
            and ticket.get("customer_id") == draft["customer_id"]
        ):
            return ticket
        if _within_window(ticket, window):
            return ticket
    return None


def _record_occurrence(ticket: dict, draft: dict) -> dict:
    detail = _detail(ticket)
    detail["occurrences"] = int(detail.get("occurrences", 1)) + 1
    detail["last_seen_at"] = clock.now_iso()
    seen = list(detail.get("duplicate_messages") or [])
    seen.append(clock.now_iso())
    detail["duplicate_messages"] = seen[-20:]
    store.update_ticket(
        str(ticket["id"]),
        detail=detail,
        priority=max(
            float(ticket.get("priority") or 0.0), float(draft.get("priority") or 0.0)
        ),
    )
    store.audit(
        "ticket.duplicate",
        {"ticket_id": ticket["id"], "occurrences": detail["occurrences"]},
    )
    return store.get_ticket(str(ticket["id"])) or ticket


# --------------------------------------------------------------------------- create


def create(
    draft: dict,
    team: str | None = None,
    severity: str | None = None,
) -> dict:
    """Create or merge a ticket. Returns the ticket, the duplicate flag and routing."""
    if severity:
        draft = {**draft, "severity": severity.lower()}
        draft["priority_detail"] = score(draft)
        draft["priority"] = draft["priority_detail"]["score"]

    existing = find_duplicate(draft)
    if existing:
        merged = _record_occurrence(existing, draft)
        return {"ticket": merged, "duplicate": True, "routing": None}

    ticket = store.create_ticket(
        session_id=draft.get("session_id"),
        customer_id=draft.get("customer_id"),
        subject=draft.get("subject") or "",
        issue=draft.get("issue") or "",
        category=draft.get("category"),
        severity=draft.get("severity") or "medium",
        impact=draft.get("impact"),
        priority=float(draft.get("priority") or 0.0),
        detail={
            "fingerprint": draft.get("fingerprint"),
            "sentiment": draft.get("sentiment"),
            "language": draft.get("language"),
            "entities": draft.get("entities"),
            "priority": draft.get("priority_detail"),
            "occurrences": 1,
        },
    )
    # A lone ticket is its own group; duplicates join the same group.
    store.update_ticket(str(ticket["id"]), group_id=ticket["id"])

    decision = None
    if team:
        store.update_ticket(str(ticket["id"]), team=team, route_reason="manual")
    else:
        decision = routing.apply(ticket)
    sla.apply(store.get_ticket(str(ticket["id"])) or ticket)

    store.audit(
        "ticket.created",
        {
            "ticket_id": ticket["id"],
            "category": draft.get("category"),
            "severity": draft.get("severity"),
            "priority": draft.get("priority"),
            "duplicate": False,
        },
    )
    return {
        "ticket": store.get_ticket(str(ticket["id"])) or ticket,
        "duplicate": False,
        "routing": decision,
    }


def create_from_text(
    session_id: str | None,
    message: str,
    customer_id: str | None = None,
) -> dict:
    """Classify and create in one call - used by `POST /tickets` and `/chat`."""
    history = store.customer_texts(session_id, 10) if session_id else []
    result = triage_engine.classify(message, history)
    draft = extract(session_id, message, result, customer_id)
    return create(draft)


def create_from_chat(
    session: dict,
    message: str,
    result: dict,
) -> dict | None:
    """Chat turns create tickets only when the intent needs follow-up work.

    A calm policy question ("What is the refund window?") is answered from the
    knowledge base; it is not a piece of work. The same words with a complaint
    ("Why is my refund still delayed?") do create a ticket.
    """
    intent = str(result.get("intent") or "")
    if intent not in TICKET_INTENTS:
        return None

    label = str((result.get("sentiment") or {}).get("label") or "neutral")
    risk = str((result.get("risk") or {}).get("level") or "none")
    informational = message.rstrip().endswith("?") or bool(
        re.match(
            r"^\s*(?:what|when|where|which|who|how|can|could|do|does|is|are|will)\b",
            message,
            re.I,
        )
    )
    if informational and risk in ("none", "low") and label in ("neutral", "positive"):
        return None
    if (
        informational
        and risk in ("none", "low")
        and label not in ("frustrated", "urgent", "sarcastic")
        and not _COMPLAINT_SIGNALS.search(message)
    ):
        return None

    draft = extract(session.get("id"), message, result, session.get("customer_id"))
    return create(draft)


# --------------------------------------------------------------------------- handoff


def handoff(ticket: dict) -> dict:
    """Masked, self-contained summary for the next agent."""
    detail = _detail(ticket)
    position = sla.state(ticket)
    entities = detail.get("entities") or {}
    sentiment = detail.get("sentiment") or {}
    priority = detail.get("priority") or {}

    escalation = None
    if ticket.get("session_id"):
        open_rows = [
            row
            for row in store.escalations(str(ticket["session_id"]))
            if not row.get("resolved_at")
        ]
        if open_rows:
            latest = open_rows[-1]
            escalation = {
                "reason": latest.get("reason"),
                "queue": latest.get("queue"),
                "condition": latest.get("condition"),
            }

    if ticket.get("status") == "escalated":
        next_action = "SLA breached: supervisor review before any further reply."
    elif escalation:
        next_action = (
            f"Escalation open ({escalation['reason']}): owner in {escalation['queue']}."
        )
    elif ticket.get("route_reason") == "after_hours_next_day":
        next_action = (
            f"Queued for the next working start: {ticket.get('scheduled_for')}."
        )
    else:
        next_action = f"{ticket.get('team') or 'Unassigned'} to respond before {position['due_at']}."

    return {
        "ticket_id": ticket.get("id"),
        "group_id": ticket.get("group_id") or ticket.get("id"),
        "subject": security.mask(ticket.get("subject") or ""),
        "category": ticket.get("category"),
        "severity": ticket.get("severity"),
        "impact": ticket.get("impact"),
        "priority": ticket.get("priority"),
        "priority_detail": priority,
        "sentiment": sentiment,
        "language": detail.get("language"),
        "status": ticket.get("status"),
        "counts_as": "grouped" if int(detail.get("occurrences", 1)) > 1 else "primary",
        "occurrences": int(detail.get("occurrences", 1)),
        "identifiers": {
            key: entities.get(key) for key in _ENTITY_KEYS if entities.get(key)
        },
        "summary": security.mask(ticket.get("issue") or "")[:500],
        "team": ticket.get("team"),
        "route_reason": ticket.get("route_reason"),
        "scheduled_for": ticket.get("scheduled_for"),
        "sla": {
            "status": position["status"],
            "ratio": position["ratio"],
            "due_at": position["due_at"],
            "warn_at": position["warn_at"],
            "remaining_minutes": position["remaining_minutes"],
        },
        "escalation": escalation,
        "next_action": next_action,
    }
