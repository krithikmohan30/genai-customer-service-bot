"""Step 3 acceptance: escalation rules, after-hours queues, 15-minute timer (Task 1)."""

from __future__ import annotations

from datetime import datetime

from app import store
from app.core import clock, config, triage
from app.core import calendar as work_calendar

FRIDAY_IN_HOURS = "2026-11-20T11:00:00+05:30"
FRIDAY_NIGHT = "2026-11-20T22:00:00+05:30"


def chat(client, message: str, session_id: str | None = None) -> dict:
    payload = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    return response.json()


def escalations_for(session_id: str) -> list[dict]:
    return [row for row in store.escalations(session_id)]


# --------------------------------------------------------------------------- business calendar


def test_business_minutes_skip_the_weekend():
    clock.set_time("2026-11-20T18:00:00+05:30")  # Friday
    start = clock.now()
    monday = clock.now().replace(day=23, hour=10, minute=30)
    assert work_calendar.business_minutes_between(start, monday) == 90
    after_close = clock.now().replace(hour=19)
    assert work_calendar.is_open(after_close) is False
    assert (
        work_calendar.next_working_start(after_close)
        .isoformat()
        .startswith("2026-11-23T09:30")
    )


def test_holiday_closes_the_business_day():
    config.set_override("business_hours.holidays", ["2026-11-20"])
    clock.set_time(FRIDAY_IN_HOURS)
    assert work_calendar.is_open() is False
    assert work_calendar.next_working_start().isoformat().startswith("2026-11-23T09:30")


# --------------------------------------------------------------------------- escalation rules


def test_sarcasm_in_hours_does_not_escalate(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "Great, another delay on my refund")

    assert body["sentiment"]["label"] == "sarcastic"
    assert body["escalated"] is False
    assert escalations_for(body["session_id"]) == []


def test_calm_high_risk_escalates_to_trust_and_safety(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "No rush, but someone changed my password and it wasn't me")

    assert body["sentiment"]["label"] == "neutral"
    assert body["risk"]["level"] == "high"
    assert body["escalation"]["reason"] == "account_compromise"
    assert body["escalation"]["queue"] == "trust_and_safety"

    record = escalations_for(body["session_id"])[0]
    assert record["condition"].startswith("risk.level=high")
    assert record["summary"]
    assert record["sentiment"] == "neutral"


def test_duplicate_payment_escalates(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "I was charged twice for order ORD-4512")
    assert body["escalation"]["reason"] == "duplicate_payment"


def test_escalation_is_recorded_once_per_reason(client):
    clock.set_time(FRIDAY_IN_HOURS)
    first = chat(client, "Someone changed my password, this is not me")
    chat(client, "This is a legal matter now, my lawyer will call", first["session_id"])

    reasons = [row["reason"] for row in escalations_for(first["session_id"])]
    assert reasons.count("account_compromise") == 1
    assert "legal_threat" in reasons


# --------------------------------------------------------------------------- after hours


def test_after_hours_urgent_goes_to_on_call(client):
    clock.set_time(FRIDAY_NIGHT)
    body = chat(client, "URGENT: I can't access my account")

    assert body["sentiment"]["label"] == "urgent"
    assert body["escalation"]["reason"] == "after_hours_urgent"
    assert body["escalation"]["queue"] == "on_call_support"
    assert body["escalation"]["scheduled_for"] is None


def test_after_hours_complaint_is_scheduled_for_next_working_day(client):
    clock.set_time(FRIDAY_NIGHT)
    body = chat(client, "My certificate is still missing and no one is responding")

    assert body["escalation"]["reason"] == "after_hours_complaint"
    assert body["escalation"]["queue"] == "next_working_day"

    scheduled = datetime.fromisoformat(body["escalation"]["scheduled_for"])
    assert scheduled.date().isoformat() == "2026-11-23"  # Monday
    assert (scheduled.hour, scheduled.minute) == (9, 30)


def test_in_hours_complaint_is_not_escalated(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "The recorded video for module 3 is not working")

    assert body["escalated"] is False
    assert escalations_for(body["session_id"]) == []


def test_urgent_in_hours_is_not_sent_to_on_call(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "URGENT: I can't access my account")

    assert body["sentiment"]["label"] == "urgent"
    assert body["escalated"] is False


# --------------------------------------------------------------------------- repeated negative


def test_repeated_negative_messages_escalate(client):
    clock.set_time(FRIDAY_IN_HOURS)
    first = chat(client, "The recorded video for module 3 is not working")
    chat(client, "It is still not working", first["session_id"])
    third = chat(client, "This is the third time it failed", first["session_id"])

    assert third["escalation"]["reason"] == "repeated_negative"
    assert third["escalation"]["queue"] == "supervisor_queue"
    assert third["escalation"]["condition"] == "negative_streak>=3"


# --------------------------------------------------------------------------- 15 minute timer


def test_unresolved_negative_escalates_after_15_minutes(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "The recorded video for module 3 is not working")
    session_id = body["session_id"]
    assert escalations_for(session_id) == []

    clock.advance(minutes=16)
    sweep = client.post("/admin/sweep").json()
    assert sweep["created"] >= 1

    reasons = [row["reason"] for row in escalations_for(session_id)]
    assert reasons == ["unresolved_15m"]

    record = escalations_for(session_id)[0]
    assert record["condition"] == "unresolved_minutes>=15"
    assert record["queue"] == "supervisor_queue"


def test_resolved_conversation_is_not_swept(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "The recorded video for module 3 is not working")
    chat(client, "It works now, thank you", body["session_id"])

    clock.advance(minutes=30)
    client.post("/admin/sweep")

    assert escalations_for(body["session_id"]) == []


def test_timer_does_not_fire_before_15_minutes(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "The recorded video for module 3 is not working")

    clock.advance(minutes=10)
    client.post("/admin/sweep")

    assert escalations_for(body["session_id"]) == []


# --------------------------------------------------------------------------- multilingual


def test_hindi_sentiment_and_language():
    result = triage.classify("कोर्स नहीं चल रहा, समस्या है")
    assert result["language"] == "hi"
    assert result["sentiment"]["label"] == "negative"


def test_tamil_urgency():
    result = triage.classify("உடனே உதவி வேண்டும்")
    assert result["language"] == "ta"
    assert result["sentiment"]["label"] == "urgent"


def test_hinglish_frustration():
    result = triage.classify("mera refund nahi aaya, pareshan hoon")
    assert result["language"] == "hinglish"
    assert result["sentiment"]["label"] == "frustrated"


def test_tanglish_frustration():
    result = triage.classify("velai aagala, romba kastam")
    assert result["language"] == "tanglish"
    assert result["sentiment"]["label"] == "frustrated"


def test_reply_language_matches_the_customer(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "mera refund nahi aaya, pareshan hoon")
    assert body["language"] == "hinglish"
    assert "sorry" in body["reply"].lower()


# --------------------------------------------------------------------------- tone


def test_tone_prefix_does_not_change_the_policy_text(client):
    clock.set_time(FRIDAY_IN_HOURS)
    body = chat(client, "My refund is still not processed, what is the refund policy?")

    assert body["policy"]["status"] == "answered"
    assert body["policy"]["citations"] == ["refund_policy_in_v2"]
    assert body["reply"].startswith("I understand the frustration")
    assert "14 calendar days" in body["reply"]


def test_escalated_reply_names_the_queue(client):
    clock.set_time(FRIDAY_NIGHT)
    body = chat(client, "URGENT: I can't access my account")
    assert "on call support team" in body["reply"]


# --------------------------------------------------------------------------- admin surface


def test_calendar_endpoint_reports_closed_at_night(client):
    clock.set_time(FRIDAY_NIGHT)
    state = client.get("/admin/calendar").json()
    assert state["open"] is False
    assert state["next_working_start"].startswith("2026-11-23T09:30")


# --------------------------------------------------------------------------- model guards


def test_model_cannot_downgrade_an_explicit_complaint():
    message = "कोर्स नहीं चल रहा, समस्या है"
    rules = triage._rule_classify(message)
    merged = triage._merge(
        {"sentiment": {"label": "neutral", "confidence": 0.9}}, rules, message
    )
    assert merged["sentiment"]["label"] == "negative"


def test_model_cannot_invent_risk_without_evidence():
    message = "URGENT: I can't access my account"
    rules = triage._rule_classify(message)
    merged = triage._merge(
        {
            "risk": {"level": "high", "categories": ["account_compromise"]},
            "sentiment": {"label": "urgent", "confidence": 0.9},
        },
        rules,
        message,
    )
    assert merged["risk"]["level"] == "medium"
    assert merged["risk"]["categories"] == []


def test_evidence_backed_risk_is_kept():
    message = "No rush, but someone changed my password and it wasn't me"
    rules = triage._rule_classify(message)
    merged = triage._merge(
        {"risk": {"level": "high", "categories": ["account_compromise"]}},
        rules,
        message,
    )
    assert merged["risk"]["level"] == "high"
    assert merged["risk"]["categories"] == ["account_compromise"]
