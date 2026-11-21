"""Task 2a - SLA clocks.

Allowed time is measured in **business minutes**, so weekends and configured
holidays cost nothing. A ticket raised at 22:00 Friday has not consumed 14 hours
by Monday morning; it has consumed zero, because nobody was working.

Three numbers come out of `state()`:

    due_at    creation + allowed business minutes
    warn_at   75% of the allowed time consumed (configurable)
    status    ok | warning | breached

`apply()` writes the due date onto a ticket; `sweep()` raises the warning once
and escalates a breach once. Both read configuration on every call, so a runtime
change to `sla.by_severity.*` or `sla.warn_ratio` takes effect immediately.
"""

from __future__ import annotations

import math
from datetime import datetime

from .. import store
from . import clock, config
from . import calendar as work_calendar

STATUS_ORDER = ("ok", "warning", "breached")


def warn_ratio() -> float:
    return float(config.get("sla.warn_ratio", 0.75))


def policy() -> dict:
    return {
        "warn_ratio": warn_ratio(),
        "by_severity": config.get("sla.by_severity", {}) or {},
        "timezone": str(config.get("app.timezone")),
    }


def allowed_minutes(severity: str | None) -> int:
    table = config.get("sla.by_severity", {}) or {}
    value = table.get(str(severity or "medium").lower())
    if value is None:
        value = table.get("medium", 480)
    return int(value)


def _parse(timestamp: str | None) -> datetime:
    if not timestamp:
        return clock.now()
    moment = datetime.fromisoformat(timestamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=clock.tz())


def state(ticket: dict, at: datetime | None = None) -> dict:
    """SLA position of one ticket at `at` (defaults to the injected clock)."""
    at = at or clock.now()
    created = _parse(ticket.get("created_at"))
    severity = str(ticket.get("severity") or "medium").lower()

    allowed = allowed_minutes(severity)
    consumed = work_calendar.business_minutes_between(created, at)
    ratio = round(consumed / allowed, 4) if allowed else 1.0

    due_at = work_calendar.add_business_minutes(created, allowed)
    warn_at = work_calendar.add_business_minutes(
        created, int(math.ceil(allowed * warn_ratio()))
    )

    if at >= due_at:
        status = "breached"
    elif consumed >= math.ceil(allowed * warn_ratio()):
        status = "warning"
    else:
        status = "ok"

    return {
        "ticket_id": ticket.get("id"),
        "severity": severity,
        "allowed_minutes": allowed,
        "consumed_minutes": consumed,
        "remaining_minutes": max(0, allowed - consumed),
        "ratio": ratio,
        "warn_ratio": warn_ratio(),
        "created_at": created.isoformat(),
        "warn_at": warn_at.isoformat(),
        "due_at": due_at.isoformat(),
        "status": status,
        "at": at.isoformat(),
    }


def apply(ticket: dict, at: datetime | None = None) -> dict:
    """Compute the SLA state and persist the due date on the ticket."""
    result = state(ticket, at)
    if ticket.get("sla_due_at") != result["due_at"]:
        store.update_ticket(str(ticket["id"]), sla_due_at=result["due_at"])
    return result


def sweep(at: datetime | None = None) -> dict:
    """Raise warnings once and escalate breaches once. Safe to call repeatedly."""
    from . import escalation

    at = at or clock.now()
    warnings: list[dict] = []
    breaches: list[dict] = []

    for ticket in store.open_tickets():
        position = state(ticket, at)

        if position["status"] in ("warning", "breached") and not ticket.get(
            "sla_warned_at"
        ):
            store.update_ticket(str(ticket["id"]), sla_warned_at=at.isoformat())
            store.audit(
                "sla.warning",
                {
                    "ticket_id": ticket["id"],
                    "severity": position["severity"],
                    "ratio": position["ratio"],
                },
            )
            warnings.append(
                {
                    "ticket_id": ticket["id"],
                    "severity": position["severity"],
                    "ratio": position["ratio"],
                    "due_at": position["due_at"],
                }
            )

        if position["status"] == "breached" and not ticket.get("sla_breached_at"):
            store.update_ticket(
                str(ticket["id"]),
                sla_breached_at=at.isoformat(),
                status="escalated",
            )
            session_id = ticket.get("session_id") or f"ticket:{ticket['id']}"
            record = escalation.activate(
                session_id,
                {
                    "reason": "sla_breach",
                    "condition": f"sla_breached AND severity={position['severity']}",
                    "queue": (config.get("escalation.queues", {}) or {}).get(
                        "supervisor", "supervisor_queue"
                    ),
                    "sentiment": None,
                    "summary": (
                        f"Ticket {ticket['id']} breached its "
                        f"{position['allowed_minutes']} minute SLA: "
                        f"{ticket.get('subject') or ticket.get('issue') or 'no subject'}"
                    )[:500],
                    "risk_level": None,
                    "scheduled_for": None,
                },
            )
            breaches.append(
                {
                    "ticket_id": ticket["id"],
                    "severity": position["severity"],
                    "due_at": position["due_at"],
                    "escalation_id": record["id"] if record else None,
                }
            )

    if warnings or breaches:
        store.audit("sla.sweep", {"warnings": len(warnings), "breaches": len(breaches)})

    return {"at": at.isoformat(), "warnings": warnings, "breaches": breaches}
