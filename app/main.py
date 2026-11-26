"""FastAPI entry point.

Surfaces built so far:
  step 1  health, chat, simulated time, runtime configuration
  step 2  policy search/ask with citations (Task 4)
  step 3  escalation handling and the 15-minute sweep (Task 1)
  step 4  ticket SLA clocks: 75% warning and breach escalation (Task 2)
  step 5  structured tickets, priority, routing, dedupe, handoff (Task 2)
  step 6  knowledge-base ingestion, versions, health checks (Task 3)
  step 7  uploads: OCR, cross-check, queue and retention (Task 5)
  step 8  multilingual sessions: switching, context, expiry, restore (Task 6)
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db, store
from .core import clock, config, policy, response, security, triage
from .core import escalation as escalation_engine
from .core import knowledge as knowledge_engine
from .core import routing as routing_engine
from .core import sessions as session_engine
from .core import sla as sla_engine
from .core import tickets as ticket_engine
from .core import uploads as upload_engine

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


class SessionCloseIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


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
    subject: str | None = Field(default=None, max_length=200)
    issue: str = Field(default="", max_length=4000)
    severity: str = "medium"
    session_id: str | None = None
    customer_id: str | None = None
    category: str | None = None
    impact: str | None = None
    team: str | None = None
    message: str | None = Field(default=None, max_length=4000)


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
    try:
        lifecycle = session_engine.open_or_resume(body.session_id, body.customer_id)
    except session_engine.SessionConflict as exc:
        raise HTTPException(
            status_code=403, detail="session belongs to another customer"
        ) from exc

    session = lifecycle["session"]
    session_id = session["id"]
    history = store.customer_texts(session_id, session_engine.context_limit())

    result = triage.classify(body.message, history)
    entities, carried = session_engine.merge_entities(session_id, result["entities"])
    result = {**result, "entities": entities, "carried_entities": carried}

    store.add_message(
        session_id,
        "customer",
        body.message,
        triage=result,
        language=result["language"],
    )
    language_state = session_engine.note(session, result)

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

    store.add_message(
        session_id, "assistant", reply["reply"], language=reply["language"]
    )

    ticket_result = ticket_engine.create_from_chat(session, body.message, result)

    return {
        "session_id": session_id,
        "session": session_engine.describe(
            store.get_session(session_id) or session, lifecycle, language_state
        ),
        "reply": reply["reply"],
        "tone": reply["tone"],
        "sentiment": result["sentiment"],
        "risk": result["risk"],
        "language": result["language"],
        "languages": result.get("languages"),
        "mixed": result.get("mixed", False),
        "intent": result["intent"],
        "entities": result["entities"],
        "carried_entities": carried,
        "references": session_engine.references(session_id),
        "escalation": escalation,
        "escalated": escalation is not None,
        "ticket": _ticket_brief(ticket_result),
        "policy": {
            "status": policy_result["status"] if policy_result else "not_asked",
            "citations": [c["id"] for c in policy_result["citations"]]
            if policy_result
            else [],
        },
        "source": result["source"],
        "masked_message": security.mask(body.message),
    }


@app.get("/sessions")
def session_list(
    customer_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict:
    rows = store.list_sessions(customer_id=customer_id, status=status, limit=limit)
    return {"count": len(rows), "items": rows}


@app.get("/sessions/settings")
def session_settings() -> dict:
    """Language set, inactivity window and restore window, all runtime-editable."""
    return session_engine.settings()


@app.post("/sessions/sweep")
def sessions_sweep() -> dict:
    return session_engine.sweep()


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


@app.get("/sessions/{session_id}/context")
def session_context(session_id: str) -> dict:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": session_id,
        "messages": session_engine.messages(session_id),
        "references": session_engine.references(session_id),
        "limit": session_engine.context_limit(),
    }


@app.post("/sessions/{session_id}/close")
def session_close(session_id: str, body: SessionCloseIn | None = None) -> dict:
    try:
        session = session_engine.close(
            session_id, (body.reason if body else None) or "customer_closed"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"session": session}


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


# --------------------------------------------------------------------------- knowledge (Task 3)


class ActivateIn(BaseModel):
    version: int | None = None
    force: bool = False


class RollbackIn(BaseModel):
    version: int | None = None
    reason: str = "manual"


@app.post("/kb/scan")
def kb_scan() -> dict:
    """Detect new and modified files, gate them, and stage versions."""
    return knowledge_engine.scan()


@app.post("/kb/sweep")
def kb_sweep() -> dict:
    """Run due activation jobs, then the 5-minute health check."""
    return knowledge_engine.run_maintenance()


@app.get("/kb/stats")
def kb_stats() -> dict:
    return knowledge_engine.stats()


@app.get("/kb/docs")
def kb_list(requester: str = "customer") -> dict:
    rows = knowledge_engine.active_documents(requester)
    return {
        "requester": requester,
        "count": len(rows),
        "items": [
            {
                "id": row["id"],
                "title": row["title"],
                "access_level": row["access_level"],
                "region": row["region"],
                "version": row["current_version"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
    }


@app.get("/kb/docs/{document_id}")
def kb_read(document_id: str, requester: str = "customer") -> dict:
    detail = knowledge_engine.document_detail(document_id, requester)
    if detail is None:
        raise HTTPException(status_code=404, detail="document not found or restricted")
    return detail


@app.get("/kb/docs/{document_id}/versions")
def kb_versions(document_id: str, requester: str = "admin") -> dict:
    document = store.get_document(document_id)
    if document is None or not knowledge_engine.can_read(document, requester):
        raise HTTPException(status_code=404, detail="document not found or restricted")
    return {
        "document_id": document_id,
        "current_version": document["current_version"],
        "items": [
            {
                "version": row["version"],
                "status": row["status"],
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
                "content_hash": row["content_hash"],
                "quality": row["quality"],
            }
            for row in store.versions(document_id)
        ],
    }


@app.post("/kb/docs/{document_id}/activate")
def kb_activate(document_id: str, body: ActivateIn) -> dict:
    """Activate a staged version; outside the window it is scheduled instead."""
    if store.get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        return knowledge_engine.activate(document_id, body.version, force=body.force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/kb/docs/{document_id}/rollback")
def kb_rollback(document_id: str, body: RollbackIn) -> dict:
    if store.get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        return knowledge_engine.rollback(document_id, body.version, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/kb/search")
def kb_search(q: str, requester: str = "customer", limit: int = 5) -> dict:
    return {
        "query": q,
        "requester": requester,
        "items": knowledge_engine.search(q, requester, limit),
    }


# --------------------------------------------------------------------------- uploads (Task 5)


def _upload_payload(upload: dict) -> dict:
    payload = {**upload}
    raw = payload.get("extracted")
    if raw:
        try:
            payload["extracted"] = json.loads(raw)
        except (ValueError, TypeError):
            payload["extracted"] = None
    else:
        payload["extracted"] = None
    return payload


@app.post("/uploads")
def create_upload(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    message: str | None = Form(None),
) -> dict:
    """Receive an invoice, screenshot or PDF; OCR it now or queue it."""
    data = file.file.read()
    filename = file.filename or "upload"
    try:
        result = upload_engine.receive(
            filename,
            data,
            session_id=session_id,
            message=message,
            content_type=file.content_type,
        )
    except upload_engine.UnsafeFile as exc:
        reason = str(exc.args[0]) if exc.args else "unsafe_file"
        detail = exc.args[1] if len(exc.args) > 1 else {}
        row = store.add_upload(
            filename=Path(filename).name,
            session_id=session_id,
            mime=file.content_type,
            size_bytes=len(data),
            message=security.mask(message) if message else None,
            status="rejected",
            reason=reason,
        )
        store.audit(
            "upload.rejected",
            {"upload_id": row["id"], "reason": reason, "details": detail},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "upload_id": row["id"],
                "status": "rejected",
                "reason": reason,
                "details": detail,
            },
        ) from exc

    return {
        "upload_id": result["id"],
        "status": result["status"],
        "queued": result.get("queued", False),
        "estimate_seconds": result.get("estimate_seconds"),
        "reason": result.get("reason"),
        "delete_at": result.get("delete_at"),
        "extracted": _upload_payload(result).get("extracted"),
    }


@app.post("/uploads/sweep")
def uploads_sweep() -> dict:
    """Run queued OCR jobs and delete uploads past retention."""
    return upload_engine.run_maintenance()


@app.get("/uploads")
def list_uploads(session_id: str | None = None, status: str | None = None) -> dict:
    rows = store.list_uploads(session_id=session_id, status=status)
    return {"count": len(rows), "items": [_upload_payload(row) for row in rows]}


@app.get("/uploads/{upload_id}")
def read_upload(upload_id: str) -> dict:
    row = store.get_upload(upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="upload not found")
    return _upload_payload(row)


@app.post("/uploads/{upload_id}/process")
def process_upload(upload_id: str) -> dict:
    """Force synchronous processing (admin path for a queued upload)."""
    if store.get_upload(upload_id) is None:
        raise HTTPException(status_code=404, detail="upload not found")
    try:
        upload_engine.process(upload_id)
    except upload_engine.ProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _upload_payload(store.get_upload(upload_id) or {})


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


def _ticket_brief(result: dict | None) -> dict | None:
    if not result:
        return None
    ticket = result["ticket"]
    return {
        "id": ticket["id"],
        "group_id": ticket.get("group_id"),
        "subject": ticket.get("subject"),
        "category": ticket.get("category"),
        "severity": ticket.get("severity"),
        "priority": ticket.get("priority"),
        "status": ticket.get("status"),
        "team": ticket.get("team"),
        "route_reason": ticket.get("route_reason"),
        "scheduled_for": ticket.get("scheduled_for"),
        "sla_due_at": ticket.get("sla_due_at"),
        "duplicate": result["duplicate"],
    }


def _explicit_draft(body: TicketIn) -> dict:
    subject = security.mask((body.subject or body.issue or "Support request").strip())[
        :200
    ]
    issue = security.mask((body.issue or subject).strip())[:4000]
    category = (body.category or "general_question").lower()

    draft = {
        "session_id": body.session_id,
        "customer_id": body.customer_id,
        "subject": subject,
        "issue": issue,
        "category": category,
        "severity": body.severity.lower(),
        "impact": (body.impact or "individual").lower(),
        "sentiment": {"label": "neutral", "confidence": 0.5},
        "language": "en",
        "risk": {"level": "none", "categories": []},
        "entities": {},
    }
    draft["fingerprint"] = ticket_engine.fingerprint(category, {}, subject)
    draft["priority_detail"] = ticket_engine.score(draft)
    draft["priority"] = draft["priority_detail"]["score"]
    return draft


@app.post("/tickets")
def create_ticket(body: TicketIn) -> dict:
    """Raise a ticket from a message (extracted) or from explicit fields.

    `message` runs triage and extraction, which is the normal path. Explicit
    fields keep the endpoint usable for agents and for tests.
    """
    if body.message:
        result = ticket_engine.create_from_text(
            body.session_id, body.message, body.customer_id
        )
    else:
        result = ticket_engine.create(
            _explicit_draft(body),
            team=body.team,
            severity=body.severity.lower(),
        )
    return {
        **_ticket_payload(result["ticket"]),
        "duplicate": result["duplicate"],
        "routing": result["routing"],
    }


@app.get("/tickets")
def list_tickets(
    status: str | None = None,
    team: str | None = None,
    group: str | None = None,
    limit: int = 200,
) -> dict:
    rows = store.search_tickets(status=status, team=team, group=group, limit=limit)
    return {"count": len(rows), "items": [_ticket_payload(ticket) for ticket in rows]}


@app.get("/tickets/{ticket_id}")
def read_ticket(ticket_id: str) -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return {**_ticket_payload(ticket), "handoff": ticket_engine.handoff(ticket)}


@app.get("/tickets/{ticket_id}/handoff")
def read_handoff(ticket_id: str) -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket_engine.handoff(ticket)


@app.post("/tickets/{ticket_id}/route")
def reroute_ticket(ticket_id: str) -> dict:
    """Re-run routing, e.g. after a team went unavailable or the queue filled."""
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    decision = routing_engine.apply(ticket)
    updated = store.get_ticket(ticket_id) or ticket
    return {
        "ticket_id": ticket_id,
        "routing": decision,
        "ticket": _ticket_payload(updated),
    }


@app.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str) -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    store.update_ticket(ticket_id, status="resolved")
    store.audit("ticket.resolved", {"ticket_id": ticket_id})
    return _ticket_payload(store.get_ticket(ticket_id) or ticket)


@app.post("/admin/sweep")
def sweep_escalations() -> dict:
    """Run the timers now: unresolved conversations and ticket SLAs.

    Called automatically on every turn; exposed so simulated time jumps can be
    evaluated without waiting for the customer to speak again.
    """
    created = escalation_engine.sweep()
    sla_result = sla_engine.sweep()
    kb_result = knowledge_engine.run_maintenance()
    upload_result = upload_engine.run_maintenance()
    session_result = session_engine.sweep()
    return {
        "created": len(created),
        "items": created,
        "sla_warnings": sla_result["warnings"],
        "sla_breaches": sla_result["breaches"],
        "knowledge": {
            "jobs": kb_result["jobs"]["processed"],
            "health": kb_result["health"]["results"],
        },
        "uploads": upload_result,
        "sessions": {
            "expired": session_result["expired"],
            "archived": session_result["archived"],
        },
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
