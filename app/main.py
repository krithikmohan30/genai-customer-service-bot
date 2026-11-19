"""FastAPI entry point.

Step 1 surface: health, chat (triage only), and the two admin endpoints the
hidden evaluation needs — simulated time and runtime configuration changes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db, store
from .core import clock, config, policy, security, triage

REPLIES = {
    "positive": "Glad that worked. Anything else I can check for you?",
    "neutral": "Thanks for the details. Let me look into this.",
    "negative": "I'm sorry about the trouble. I'm checking your issue now.",
    "frustrated": "I understand the frustration, and I'm sorry. I'm taking this up now.",
    "urgent": "I've flagged this as urgent and a specialist is picking it up now.",
    "sarcastic": "I hear you, and I'm sorry this keeps happening. I'm on it now.",
}

ESCALATION_NOTE = {
    "account_compromise": "I've escalated this to our security team as a priority.",
    "duplicate_payment": "I've escalated this to our billing team to reverse the duplicate charge.",
    "legal_threat": "I've escalated this to our resolution team, who will contact you.",
    "fraud": "I've escalated this to our trust and safety team.",
}


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

    escalation = triage.decide_escalation(result, body.message, history)
    if escalation and not store.has_open_escalation(session_id, escalation["reason"]):
        store.add_escalation(
            session_id=session_id,
            reason=escalation["reason"],
            condition=escalation["condition"],
            summary=escalation["summary"],
            queue=escalation["queue"],
            risk_level=result["risk"]["level"],
        )
        store.audit(
            "escalation.created",
            {"session_id": session_id, "reason": escalation["reason"]},
        )

    reply = REPLIES.get(result["sentiment"]["label"], REPLIES["neutral"])
    if escalation:
        reply = f"{reply} {ESCALATION_NOTE.get(escalation['reason'], '')}".strip()

    store.add_message(session_id, "assistant", reply)

    return {
        "session_id": session_id,
        "reply": reply,
        "sentiment": result["sentiment"],
        "risk": result["risk"],
        "language": result["language"],
        "intent": result["intent"],
        "entities": result["entities"],
        "escalation": escalation,
        "escalated": escalation is not None,
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
