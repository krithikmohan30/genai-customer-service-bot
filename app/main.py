"""FastAPI entry point.

Surfaces built so far:
  step 1  health, chat, simulated time, runtime configuration
  step 2  policy search/ask with citations (Task 4)
  step 3  escalation handling and the 15-minute sweep (Task 1)
  step 4  ticket SLA clocks: 75% warning and breach escalation (Task 2)
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db, store
from .core import clock, config, policy, response, security, triage
from .core import escalation as escalation_engine
from .core import sla as sla_engine

QUESTION = re.compile(
    r"^\s*(?:what|how|when|where|who|why|which|can|could|do|does|did|is|are|will|"
    r"should|kya|kaise|kab|kahan|enna|eppadi|eppo)\b",
    re.I,
)


def _looks_like_question(message: str) -> bool:
    return "?" in message or bool(QUESTION.search(message))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    store.audit("app.start", {"db": str(db.db_path())})
    yield


app = FastAPI(title="Elevance Support Bot", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- models


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    customer_id: str | None = None


class TimeIn(BaseModel):
    iso: str | None = None
    advance_minutes: int = 0
    advance_hours: int = 0
    advance_days: int = 0
    reset: bool = False


class ConfigIn(BaseModel):
    key: str
    value: object


class PolicyIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    requester: str = "customer"
    as_of: str | None = None
    product: str | None = None
    region: str | None = None


class TicketIn(BaseModel):
    subject: str = Field(min_length=2, max_length=200)
    issue: str = Field(default="", max_length=4000)
    severity: str = "medium"
    session_id: str | None = None
    team: str | None = None


# --------------------------------------------------------------------------- health


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if db.healthy() else "degraded",
        "version": app.version,
        "time": clock.now_iso(),
        "timezone": str(config.get("app.timezone")),
        "db": str(db.db_path()),
        "triage_mode": config.get("triage.mode"),
    }


# --------------------------------------------------------------------------- chat


@app.post("/chat")
def chat(body: ChatIn) -> dict:
    session = store.get_or_create_session(body.session_id, body.customer_id)
    session_id = session["id"]
    history = store.customer_texts(
        session_id, int(config.get("sessions.context_messages", 10))
    )

    result = triage.classify(body.message, history)
    store.add_message(
        session_id,
        "customer",
        body.message,
        triage=result,
        language=result["language"],
    )
    store.touch_session(session_id, result["language"])

    escalation = escalation_engine.handle(session_id, result)

    policy_result = None
    if _looks_like_question(body.message):
        policy_result = policy.answer(body.message, requester="customer")

    reply = response.compose(result, policy_result, escalation)

    # No escalation for this message means the conversation is still open; check
    # whether an older negative streak has crossed the 15-minute timer.
    if not escalation:
        escalation = escalation_engine.check_unresolved(session_id)
        if escalation:
            reply = response.compose(result, policy_result, escalation)

    store.add_message(session_id, "assistant", reply["reply"])

    return {
        "session_id": session_id,
        "reply": reply["reply"],
        "tone": reply["tone"],
        "sentiment": result["sentiment"],
        "risk": result["risk"],
        "language": result["language"],
        "intent": result["intent"],
        "entities": result["entities"],
        "escalation": escalation,
        "escalated": escalation is not None,
        "policy": {
            "status": policy_result["status"] if policy_result else "not_asked",
            "citations": [c["id"] for c in policy_result["citations"]]
            if policy_result
            else [],
        },
        "source": result["source"],
        "masked_message": security.mask(body.message),
    }


@app.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session": session,
        "messages": [
            {k: m[k] for k in ("role", "masked", "language", "created_at")}
            for m in store.recent_messages(session_id, 50)
        ],
        "escalations": store.escalations(session_id),
    }


@app.get("/escalations")
def escalation_list() -> dict:
    rows = store.escalations()
    return {"count": len(rows), "items": rows}


# --------------------------------------------------------------------------- policy (Task 4)


@app.post("/policy/search")
def policy_search(body: PolicyIn) -> dict:
    """Metadata-filtered retrieval only: useful for showing what was hidden."""
    return policy.search(
        body.question,
        requester=body.requester,
        as_of=body.as_of,
        product=body.product,
        region=body.region,
    )


@app.post("/policy/ask")
def policy_ask(body: PolicyIn) -> dict:
    result = policy.answer(
        body.question,
        requester=body.requester,
        as_of=body.as_of,
        product=body.product,
        region=body.region,
    )
    store.audit(
        "policy.answer",
        {
            "requester": body.requester,
            "status": result["status"],
            "reason": result.get("reason"),
            "citations": [c["id"] for c in result["citations"]],
        },
    )
    return result


@app.get("/policy/stats")
def policy_stats() -> dict:
    return policy.stats()


# --------------------------------------------------------------------------- admin


@app.get("/admin/time")
def get_time() -> dict:
    return clock.state()


@app.post("/admin/time")
def set_time(body: TimeIn) -> dict:
    """Freeze, advance or reset the injected clock."""
    if body.reset:
        clock.reset()
    else:
        if body.iso:
            clock.set_time(body.iso)
        if body.advance_minutes or body.advance_hours or body.advance_days:
            clock.advance(
                days=body.advance_days,
                hours=body.advance_hours,
                minutes=body.advance_minutes,
            )
    store.audit("admin.time", clock.state())
    return clock.state()


@app.get("/admin/config")
def get_config() -> dict:
    return config.snapshot()


@app.post("/admin/config")
def set_config(body: ConfigIn) -> dict:
    """Runtime override, e.g. shrink the SLA or add a holiday."""
    config.set_override(body.key, body.value)
    store.audit("admin.config.set", {"key": body.key, "value": body.value})
    return {"overrides": config.snapshot()["overrides"]}


@app.delete("/admin/config")
def reset_config() -> dict:
    config.clear_overrides()
    store.audit("admin.config.clear", None)
    return {"overrides": {}}


@app.get("/admin/audit")
def audit_tail(limit: int = 50) -> dict:
    return {"items": store.audit_tail(limit)}


# --------------------------------------------------------------------------- tickets


def _ticket_payload(ticket: dict) -> dict:
    return {**ticket, "sla": sla_engine.state(ticket)}


@app.post("/tickets")
def create_ticket(body: TicketIn) -> dict:
    """Raise a ticket and start its business-hour SLA clock.

    Step 5 derives these fields from the conversation; this endpoint keeps the
    SLA engine usable on its own (and testable without a chat turn).
    """
    severity = body.severity.lower()
    ticket = store.create_ticket(
        session_id=body.session_id,
        subject=body.subject,
        issue=body.issue,
        severity=severity,
        team=body.team,
    )
    sla_engine.apply(ticket)
    store.audit(
        "ticket.created",
        {"ticket_id": ticket["id"], "severity": severity},
    )
    return _ticket_payload(store.get_ticket(ticket["id"]) or ticket)


@app.get("/tickets")
def list_tickets() -> dict:
    return {"items": [_ticket_payload(ticket) for ticket in store.open_tickets()]}


@app.get("/tickets/{ticket_id}")
def read_ticket(ticket_id: str) -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return _ticket_payload(ticket)


@app.post("/admin/sweep")
def sweep_escalations() -> dict:
    """Run the timers now: unresolved conversations and ticket SLAs.

    Called automatically on every turn; exposed so simulated time jumps can be
    evaluated without waiting for the customer to speak again.
    """
    created = escalation_engine.sweep()
    sla_result = sla_engine.sweep()
    return {
        "created": len(created),
        "items": created,
        "sla_warnings": sla_result["warnings"],
        "sla_breaches": sla_result["breaches"],
    }


@app.get("/admin/calendar")
def calendar_state() -> dict:
    from .core import calendar as work_calendar

    moment = clock.now()
    return {
        "now": moment.isoformat(),
        "open": work_calendar.is_open(moment),
        "next_working_start": work_calendar.next_working_start(moment).isoformat(),
        "working_days": work_calendar.working_days(),
        "holidays": sorted(work_calendar.holidays()),
    }
