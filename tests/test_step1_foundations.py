"""Step 1 acceptance: clock, config, masking, triage, chat endpoint."""

from __future__ import annotations

from app.core import clock, config, security, triage


# --------------------------------------------------------------------------- clock


def test_clock_freezes_and_advances():
    clock.set_time("2026-11-20T10:00:00+05:30")
    assert clock.now_iso().startswith("2026-11-20T10:00:00")

    clock.advance(minutes=90)
    assert clock.now_iso().startswith("2026-11-20T11:30:00")

    clock.advance(days=1)
    assert clock.now_iso().startswith("2026-11-21")

    clock.reset()
    assert clock.state()["frozen"] is False


# --------------------------------------------------------------------------- config


def test_config_override_then_clear():
    original = config.get("sla.warn_ratio")
    config.set_override("sla.warn_ratio", 0.5)
    assert config.get("sla.warn_ratio") == 0.5

    config.clear_overrides()
    config.set_override("triage.mode", "rules")
    assert config.get("sla.warn_ratio") == original


def test_nested_config_sections_are_rebuilt():
    levels = config.get("policy.access_levels")
    assert levels["customer"] == ["public", "customer"]
    assert levels["admin"][-1] == "internal"
    assert config.get("escalation.queues")["trust_safety"] == "trust_and_safety"
    assert config.get_section("policy")["max_docs"] == 4


# --------------------------------------------------------------------------- security


def test_pii_is_masked():
    text = "Call 9876543210 or mail a.b@example.com, card 4111 1111 1111 1111"
    masked = security.mask(text)
    assert "9876543210" not in masked
    assert "a.b@example.com" not in masked
    assert "4111" not in masked
    assert set(security.findings(text)) >= {"EMAIL", "PHONE", "CARD"}


# --------------------------------------------------------------------------- triage


def test_sarcasm_is_not_scored_positive():
    result = triage.classify("Great, another delay on my refund")
    assert result["sentiment"]["label"] == "sarcastic"
    assert result["sentiment"]["tone"] == "calm_professional"


def test_calm_high_risk_escalates_without_being_negative():
    message = "No rush, but someone changed my password and it wasn't me"
    result = triage.classify(message)

    assert result["sentiment"]["label"] == "neutral"
    assert result["risk"]["level"] == "high"
    assert "account_compromise" in result["risk"]["categories"]

    escalation = triage.decide_escalation(result, message)
    assert escalation is not None
    assert escalation["reason"] == "account_compromise"
    assert escalation["queue"] == "trust_and_safety"


def test_duplicate_payment_is_high_risk_billing():
    result = triage.classify("I was charged twice for order ORD-4512")
    assert result["risk"]["level"] == "high"
    assert "duplicate_payment" in result["risk"]["categories"]
    assert result["intent"] == "billing_dispute"
    assert result["entities"]["order_id"] == "ORD-4512"


def test_plain_question_stays_neutral_and_unescalated():
    result = triage.classify("What is the EMI option for the data science course?")
    assert result["sentiment"]["label"] == "neutral"
    assert result["risk"]["level"] == "none"
    assert triage.decide_escalation(result, "What is the EMI option?") is None


# --------------------------------------------------------------------------- api


def test_chat_endpoint_persists_turns_and_escalation(client):
    response = client.post(
        "/chat",
        json={"message": "Someone changed my password, this is not me"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["escalated"] is True
    assert body["escalation"]["reason"] == "account_compromise"
    assert body["session_id"]

    detail = client.get(f"/sessions/{body['session_id']}").json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["customer", "assistant"]
    assert len(detail["escalations"]) == 1


def test_admin_time_moves_the_clock(client):
    client.post("/admin/time", json={"iso": "2026-11-20T10:00:00+05:30"})
    assert client.get("/health").json()["time"].startswith("2026-11-20T10:00:00")

    client.post("/admin/time", json={"advance_hours": 2})
    assert client.get("/health").json()["time"].startswith("2026-11-20T12:00:00")

    client.post("/admin/time", json={"reset": True})
    assert client.get("/admin/time").json()["frozen"] is False


def test_admin_config_override_is_visible(client):
    client.post("/admin/config", json={"key": "sla.warn_ratio", "value": 0.9})
    assert client.get("/admin/config").json()["overrides"]["sla.warn_ratio"] == 0.9

    client.delete("/admin/config")
    assert client.get("/admin/config").json()["overrides"] == {}
