"""Task 2a - SLA clocks.

The hidden checks for this task change the clock and the configuration, so every
assertion here freezes time and moves it. A Friday evening ticket must not burn
its SLA over the weekend, the 75% warning must fire, a breach must escalate
exactly once, and a runtime change to the table must take effect immediately.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import db, store
from app.core import clock, config, sla


@pytest.fixture(autouse=True)
def no_open_tickets():
    """Other tests' tickets must not leak into sweep results."""
    db.execute("UPDATE tickets SET status = 'closed'")
    yield
    db.execute("UPDATE tickets SET status = 'closed'")


def _moment(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(clock.tz())


def _raise(at_iso: str, severity: str = "medium") -> dict:
    clock.set_time(at_iso)
    ticket = store.create_ticket(
        subject=f"{severity} issue",
        issue="printer will not connect",
        severity=severity,
    )
    sla.apply(ticket)
    return store.get_ticket(ticket["id"]) or ticket


def test_weekend_does_not_consume_sla_time():
    ticket = _raise("2026-11-20T17:00:00")  # Friday 17:00, 480 minute target
    position = sla.state(ticket)

    assert position["consumed_minutes"] == 0
    assert position["status"] == "ok"
    # 90 minutes Friday + 390 on Monday, closing at 16:00 Monday.
    assert _moment(position["due_at"]) == _moment("2026-11-23T16:00:00+05:30")

    clock.set_time("2026-11-21T12:00:00")  # Saturday
    weekend = sla.state(ticket)
    assert weekend["consumed_minutes"] == 90  # Friday 17:00-18:30 only, no weekend
    assert weekend["status"] == "ok"


def test_holiday_is_skipped():
    ticket = _raise("2026-12-24T17:00:00")  # Thursday before 25 Dec
    assert _moment(sla.state(ticket)["due_at"]) == _moment(
        "2026-12-28T16:00:00+05:30"
    )  # Friday 25 Dec is a configured holiday


def test_warning_fires_at_75_percent():
    ticket = _raise("2026-11-20T17:00:00")

    clock.set_time("2026-11-23T13:59:00")  # 359 of 480 minutes used
    before = sla.state(ticket)
    assert before["status"] == "ok"
    assert before["ratio"] == pytest.approx(0.748, abs=0.001)

    clock.set_time("2026-11-23T14:00:00")  # 360 of 480 = 75%
    after = sla.state(ticket)
    assert after["status"] == "warning"
    assert after["warn_ratio"] == 0.75


def test_breach_escalates_once():
    ticket = _raise("2026-11-20T17:00:00")

    clock.set_time("2026-11-23T16:01:00")
    assert sla.state(ticket)["status"] == "breached"

    first = sla.sweep()
    assert ticket["id"] in {item["ticket_id"] for item in first["breaches"]}
    breach = next(
        item for item in first["breaches"] if item["ticket_id"] == ticket["id"]
    )
    assert breach["escalation_id"]

    stored = store.get_ticket(ticket["id"]) or {}
    assert stored["status"] == "escalated"
    assert stored["sla_warned_at"]
    assert stored["sla_breached_at"]

    second = sla.sweep()
    assert ticket["id"] not in {item["ticket_id"] for item in second["breaches"]}

    record = next(
        row for row in store.escalations() if row["id"] == breach["escalation_id"]
    )
    assert record["reason"] == "sla_breach"
    assert "sla_breached" in record["condition"]
    assert record["queue"] == "supervisor_queue"


def test_runtime_sla_change_applies_to_open_tickets():
    ticket = _raise("2026-11-20T17:00:00", severity="high")
    assert _moment(sla.state(ticket)["due_at"]) == _moment(
        "2026-11-23T12:00:00+05:30"
    )  # 240 minutes: 90 Friday + 150 Monday

    config.set_override("sla.by_severity.high", 60)
    config.set_override("sla.warn_ratio", 0.5)

    shrunk = sla.state(ticket)
    assert shrunk["allowed_minutes"] == 60
    assert _moment(shrunk["due_at"]) == _moment("2026-11-20T18:00:00+05:30")
    assert _moment(shrunk["warn_at"]) == _moment("2026-11-20T17:30:00+05:30")

    clock.set_time("2026-11-20T17:31:00")
    assert sla.state(ticket)["status"] == "warning"

    clock.set_time("2026-11-20T18:01:00")
    assert sla.state(ticket)["status"] == "breached"


def test_sla_endpoints(client):
    clock.set_time("2026-11-20T17:00:00")
    created = client.post(
        "/tickets", json={"subject": "Login fails", "severity": "high"}
    ).json()
    assert created["sla"]["allowed_minutes"] == 240
    ticket_id = created["id"]

    listed = client.get("/tickets").json()["items"]
    assert ticket_id in {item["id"] for item in listed}

    client.post("/admin/config", json={"key": "sla.by_severity.high", "value": 60})
    updated = client.get(f"/tickets/{ticket_id}").json()
    assert updated["sla"]["allowed_minutes"] == 60

    clock.set_time("2026-11-20T18:01:00")
    swept = client.post("/admin/sweep").json()
    assert ticket_id in {item["ticket_id"] for item in swept["sla_breaches"]}

    after = client.get(f"/tickets/{ticket_id}").json()
    assert after["status"] == "escalated"
    assert client.get("/tickets/does-not-exist").status_code == 404
