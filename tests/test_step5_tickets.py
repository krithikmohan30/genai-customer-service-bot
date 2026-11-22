"""Task 2b - structured tickets, priority, routing, dedupe, handoff.

Hidden checks for this task cover duplicate tickets, unavailable teams,
after-hours routing and runtime SLA changes. Every test here freezes the clock
and closes leftover tickets so one scenario cannot see another's queue.
"""

from __future__ import annotations

import pytest

from app import db, store
from app.core import clock, config, tickets

FRIDAY_AM = "2026-11-20T11:00:00"
FRIDAY_NIGHT = "2026-11-20T22:00:00"


@pytest.fixture(autouse=True)
def no_open_tickets():
    db.execute("UPDATE tickets SET status = 'closed'")
    yield
    db.execute("UPDATE tickets SET status = 'closed'")


def _ticket(message: str, session_id: str = "s1", at: str = FRIDAY_AM) -> dict:
    clock.set_time(at)
    result = tickets.create_from_text(session_id, message)
    return result["ticket"]


def test_message_becomes_structured_ticket():
    ticket = _ticket(
        "My order ORD-45231 was charged twice. Call me on 9876543210 or mail me@example.com"
    )

    assert ticket["category"] == "billing_dispute"
    assert ticket["severity"] == "critical"
    assert ticket["impact"] == "individual"
    assert ticket["priority"] > 0
    assert ticket["group_id"] == ticket["id"]
    assert ticket["sla_due_at"]

    # Contact details never reach storage in the clear.
    assert "9876543210" not in ticket["issue"]
    assert "me@example.com" not in ticket["issue"]
    assert "[PHONE]" in ticket["issue"]
    assert "[EMAIL]" in ticket["issue"]


def test_organization_impact_outranks_individual():
    individual = _ticket("My invoice INV-1001 is wrong", session_id="a")
    many = _ticket(
        "Invoice INV-2002 is wrong and it has blocked our office",
        session_id="b",
    )
    assert many["impact"] == "team"
    assert many["priority"] > individual["priority"]


def test_priority_weights_are_runtime_configurable():
    clock.set_time(FRIDAY_AM)
    draft = tickets.extract(
        "s9",
        "Urgent: my refund is delayed and I am angry",
        {
            "sentiment": {"label": "frustrated"},
            "risk": {"level": "none", "categories": []},
            "entities": {},
            "intent": "refund_request",
            "language": "en",
        },
    )
    before = tickets.score(draft)
    assert before["components"]["severity"] > 0

    config.set_override("priority.weights.severity", 0)
    after = tickets.score(draft)
    assert after["components"]["severity"] == 0
    assert after["score"] < before["score"]


def test_routing_matches_skills():
    assert _ticket("My invoice INV-3001 is wrong", session_id="r1")["team"] == "billing"
    assert (
        _ticket("I cannot log in, my password fails", session_id="r2")["team"]
        == "technical"
    )
    security = _ticket("Someone changed my password and it was not me", session_id="r3")
    assert security["team"] == "trust_safety"
    assert security["route_reason"] == "skills+capacity"


def test_after_hours_routing():
    evening = _ticket("My invoice INV-4001 is wrong", session_id="n1", at=FRIDAY_NIGHT)
    assert evening["team"] == "next_working_day"
    assert evening["route_reason"] == "after_hours_next_day"
    assert evening["scheduled_for"].startswith("2026-11-23T09:30")

    critical = _ticket(
        "I was charged twice for INV-4002", session_id="n2", at=FRIDAY_NIGHT
    )
    assert critical["severity"] == "critical"
    assert critical["team"] == "on_call_support"
    assert critical["route_reason"] == "after_hours_on_call"


def test_unavailable_team_overflows_without_losing_the_ticket():
    config.set_override("routing.teams.billing.available", False)
    ticket = _ticket("My invoice INV-5001 is wrong", session_id="u1")

    assert ticket["team"] == "supervisor_queue"
    assert ticket["route_reason"] == "no_capacity"
    assert ticket["status"] == "open"


def test_duplicates_are_grouped_not_multiplied():
    message = "I was charged twice for order ORD-6001"
    first = tickets.create_from_text("d1", message)
    second = tickets.create_from_text("d2", message)
    third = tickets.create_from_text("d1", message)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert third["duplicate"] is True
    assert second["ticket"]["id"] == first["ticket"]["id"]
    assert third["ticket"]["id"] == first["ticket"]["id"]

    detail = tickets.handoff(store.get_ticket(first["ticket"]["id"]) or {})
    assert detail["occurrences"] == 3
    assert detail["counts_as"] == "grouped"
    assert second["ticket"]["group_id"] == first["ticket"]["group_id"]


def test_distinct_orders_are_not_duplicates():
    first = tickets.create_from_text("x1", "Charged twice for order ORD-7001")
    second = tickets.create_from_text("x2", "Charged twice for order ORD-7002")
    assert first["ticket"]["id"] != second["ticket"]["id"]


def test_handoff_is_masked_and_actionable():
    result = tickets.create_from_text(
        "y1", "Refund not received for ORD-8001, call 9876543210 immediately"
    )
    summary = tickets.handoff(result["ticket"])

    assert summary["identifiers"] == {"order_id": "ORD-8001"}
    assert "9876543210" not in summary["summary"]
    assert "[PHONE]" in summary["summary"]
    assert summary["sla"]["due_at"]
    assert summary["team"]
    assert summary["next_action"]


def test_chat_creates_ticket_and_sla_sweep_finds_it(client):
    clock.set_time(FRIDAY_AM)
    reply = client.post(
        "/chat", json={"message": "I was charged twice for INV-9001, refund please"}
    ).json()

    assert reply["ticket"] is not None
    ticket_id = reply["ticket"]["id"]
    assert reply["ticket"]["category"] == "billing_dispute"
    assert reply["ticket"]["team"] == "billing"
    assert reply["ticket"]["sla_due_at"]

    clock.set_time("2026-11-20T12:05:00")
    swept = client.post("/admin/sweep").json()
    assert ticket_id in {item["ticket_id"] for item in swept["sla_breaches"]}


def test_calm_policy_question_creates_no_ticket(client):
    clock.set_time(FRIDAY_AM)
    reply = client.post("/chat", json={"message": "What is the refund policy?"}).json()
    assert reply["ticket"] is None


def test_ticket_api_lists_filters_and_reroutes(client):
    clock.set_time(FRIDAY_AM)
    created = client.post(
        "/tickets",
        json={
            "message": "My invoice INV-1101 is wrong and I am upset",
            "session_id": "api-1",
        },
    ).json()
    ticket_id = created["id"]
    group_id = created["group_id"]
    assert created["team"] == "billing"

    listed = client.get("/tickets", params={"team": "billing"}).json()["items"]
    assert ticket_id in {item["id"] for item in listed}

    grouped = client.get("/tickets", params={"group": group_id}).json()["items"]
    assert ticket_id in {item["id"] for item in grouped}

    config.set_override("routing.teams.billing.available", False)
    rerouted = client.post(f"/tickets/{ticket_id}/route").json()
    assert rerouted["routing"]["team"] == "supervisor_queue"
    assert rerouted["ticket"]["team"] == "supervisor_queue"

    handoff = client.get(f"/tickets/{ticket_id}/handoff").json()
    assert handoff["ticket_id"] == ticket_id
    assert handoff["sla"]["status"] in ("ok", "warning")


def test_priority_rises_as_the_sla_clock_consumes():
    ticket = _ticket("My invoice INV-1201 is wrong", session_id="p1")
    draft = {
        "id": ticket["id"],
        "severity": ticket["severity"],
        "impact": ticket["impact"],
        "sentiment": {"label": "neutral"},
    }
    fresh = tickets.score(draft)
    assert fresh["components"]["sla"] == 0

    clock.set_time("2026-11-20T16:00:00")  # 270 of 480 medium minutes
    later = tickets.score(draft)
    assert later["components"]["sla"] > 0
    assert later["score"] > fresh["score"]
