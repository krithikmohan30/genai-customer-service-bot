"""Task 6 - multilingual sessions: switching, context, isolation, expiry, restore."""

from __future__ import annotations

import uuid

import pytest

from app import store
from app.core import clock, config


@pytest.fixture(autouse=True)
def clean_sessions():
    clock.set_time("2026-11-24T10:00:00")
    yield


def say(
    client, message: str, session_id: str | None = None, customer_id: str | None = None
):
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    if customer_id:
        body["customer_id"] = customer_id
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_english_then_tamil_switches_and_keeps_the_order(client):
    first = say(
        client, "My order ORD-55551 has not arrived yet", customer_id="cust-switch"
    )
    assert first["language"] == "en"
    assert first["session"]["switched_from"] is None

    second = say(
        client,
        "இந்த ஆர்டர் இன்னும் வரவில்லை, என்ன ஆச்சு?",
        session_id=first["session_id"],
        customer_id="cust-switch",
    )
    assert second["session_id"] == first["session_id"]
    assert second["language"] == "ta"
    assert second["session"]["language"] == "ta"
    assert second["session"]["switched_from"] == "en"

    reply = second["reply"]
    assert any("\u0b80" <= char <= "\u0bff" for char in reply)
    # The Tamil turn never repeated the order id; it was carried from context.
    assert second["entities"]["order_id"] == "ORD-55551"
    assert "order_id" in second["carried_entities"]
    assert "ORD-55551" in second["references"]["ids"]


def test_mixed_and_transliterated_messages_keep_their_tone(client):
    tanglish = say(client, "Order ORD-77771 refund aagala, please check")
    assert tanglish["language"] == "tanglish"
    assert set(tanglish["languages"]) == {"tanglish", "en"}
    assert tanglish["mixed"] is True
    assert any(word in tanglish["reply"].lower() for word in ("idhu", "naan", "idha"))

    script_mix = say(client, "என் order ORD-77772 refund ஆகல, check பண்ணுங்க")
    assert script_mix["language"] == "ta"
    assert set(script_mix["languages"]) == {"ta", "en"}
    assert script_mix["mixed"] is True

    hinglish = say(client, "mera order ORD-88882 kaha hai, jaldi karo")
    assert hinglish["language"] == "hinglish"

    hindi = say(client, "मेरा ऑर्डर ORD-88881 कहाँ है?")
    assert hindi["language"] == "hi"
    assert any("\u0900" <= char <= "\u097f" for char in hindi["reply"])


def test_ten_message_context_preserves_ids_dates_codes(client):
    customer = f"cust-context-{uuid.uuid4().hex[:6]}"
    first = say(
        client,
        "Invoice INV-12345 dated 12/11/2026 for Rs 4,500 failed with error E-402",
        customer_id=customer,
    )
    session_id = first["session_id"]
    for text in ("Any update?", "Okay", "Thanks", "Still waiting", "Please check"):
        say(client, text, session_id=session_id, customer_id=customer)

    switched = say(
        client,
        "இந்த பிரச்சினை இன்னும் தீரவில்லை, என்ன செய்யலாம்?",
        session_id=session_id,
        customer_id=customer,
    )

    assert switched["language"] == "ta"
    assert switched["entities"]["invoice_id"] == "INV-12345"
    assert switched["entities"]["amount"] == "4500"
    assert switched["entities"]["error_code"] == "E-402"
    assert switched["entities"]["date"] == "12/11/2026"
    assert set(switched["carried_entities"]) >= {
        "invoice_id",
        "amount",
        "error_code",
        "date",
    }

    references = switched["references"]
    assert "INV-12345" in references["ids"]
    assert "12/11/2026" in references["dates"]
    assert "4500" in references["amounts"]
    assert "E-402" in references["error_codes"]
    assert switched["session"]["context_messages"] >= 10


def test_sessions_are_isolated_by_customer(client):
    first = say(client, "My order ORD-31313 is late", customer_id="cust-a")
    session_id = first["session_id"]

    denied = client.post(
        "/chat",
        json={
            "message": "Show me this conversation",
            "session_id": session_id,
            "customer_id": "cust-b",
        },
    )
    assert denied.status_code == 403

    other = say(client, "Hello, I need help", customer_id="cust-b")
    assert other["session_id"] != session_id
    assert other["references"]["ids"] == []
    assert other["entities"].get("order_id") is None


def test_expiry_after_30_minutes_restores_the_same_session(client):
    customer = "cust-expiry"
    first = say(client, "Order ORD-61616 was charged twice", customer_id=customer)
    session_id = first["session_id"]

    clock.advance(minutes=31)
    resumed = say(client, "Any update?", session_id=session_id, customer_id=customer)

    assert resumed["session_id"] == session_id
    assert resumed["session"]["restored"] is True
    assert resumed["session"]["lifecycle"] == "resumed_within_restore_window"
    assert resumed["session"]["status"] == "active"
    assert "ORD-61616" in resumed["references"]["ids"]
    assert resumed["entities"].get("order_id") == "ORD-61616"

    stored = store.get_session(session_id) or {}
    assert stored["restored_at"] is not None
    assert stored["expired_at"] is not None


def test_beyond_24_hours_a_new_session_starts_and_the_old_is_archived(client):
    customer = "cust-archive"
    first = say(client, "Order ORD-71717 is missing", customer_id=customer)
    session_id = first["session_id"]

    clock.advance(hours=26)
    fresh = say(client, "Hello again", session_id=session_id, customer_id=customer)

    assert fresh["session_id"] != session_id
    assert fresh["session"]["created"] is True
    assert fresh["session"]["lifecycle"] == "restore_window_passed"
    assert fresh["references"]["ids"] == []
    assert (store.get_session(session_id) or {})["status"] == "archived"


def test_inactivity_and_restore_windows_are_configurable(client):
    config.set_override("sessions.inactivity_minutes", 5)
    config.set_override("sessions.restore_hours", 0)
    customer = "cust-config"
    first = say(client, "Order ORD-82828 help", customer_id=customer)
    session_id = first["session_id"]

    clock.advance(minutes=4)
    still_open = say(client, "Still here", session_id=session_id, customer_id=customer)
    assert still_open["session_id"] == session_id
    assert still_open["session"]["restored"] is False

    clock.advance(minutes=6)
    past = say(client, "Now?", session_id=session_id, customer_id=customer)
    assert past["session_id"] != session_id
    assert past["session"]["lifecycle"] == "restore_window_passed"


def test_sweep_expires_then_archives_idle_sessions(client):
    customer = f"cust-sweep-{uuid.uuid4().hex[:6]}"
    first = say(client, "Order ORD-91919 late", customer_id=customer)
    session_id = first["session_id"]

    clock.advance(minutes=31)
    expired = client.post("/sessions/sweep").json()
    assert session_id in expired["expired"]
    assert (store.get_session(session_id) or {})["status"] == "expired"

    clock.advance(hours=25)
    archived = client.post("/sessions/sweep").json()
    assert session_id in archived["archived"]
    assert (store.get_session(session_id) or {})["status"] == "archived"

    settings = client.get("/sessions/settings").json()
    assert settings["inactivity_minutes"] == 30
    assert settings["restore_hours"] == 24
    assert {"en", "hi", "ta", "tanglish"} <= set(settings["languages"])


def test_closed_session_starts_fresh_on_return(client):
    customer = "cust-close"
    first = say(client, "Order ORD-12121", customer_id=customer)
    session_id = first["session_id"]

    closed = client.post(
        f"/sessions/{session_id}/close", json={"reason": "customer done"}
    )
    assert closed.status_code == 200
    assert closed.json()["session"]["status"] == "closed"

    again = say(client, "One more thing", session_id=session_id, customer_id=customer)
    assert again["session_id"] != session_id
    assert again["session"]["lifecycle"] == "previous_closed"


def test_context_endpoint_reports_language_history(client):
    first = say(client, "My invoice INV-50000 is wrong")
    session_id = first["session_id"]
    say(client, "இது தவறு, சரி பண்ணுங்க", session_id=session_id)

    context = client.get(f"/sessions/{session_id}/context").json()
    assert context["limit"] >= 10
    customer_languages = [
        row["language"] for row in context["messages"] if row["role"] == "customer"
    ]
    assert customer_languages == ["en", "ta"]
    assert "INV-50000" in context["references"]["ids"]
