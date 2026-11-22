"""Live check for step 5: extraction, priority, routing, dedupe, handoff."""

from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8138"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode())


print("health:", call("GET", "/health")["status"])
call("POST", "/admin/time", {"iso": "2026-11-20T11:00:00"})

first = call(
    "POST",
    "/tickets",
    {
        "message": "I was charged twice for order ORD-45231, call me on 9876543210",
        "session_id": "live-1",
    },
)
print(
    "primary:",
    first["id"],
    first["category"],
    first["severity"],
    "priority",
    first["priority"],
)
print("routing:", first["team"], first["route_reason"], "due", first["sla_due_at"])

again = call(
    "POST",
    "/tickets",
    {
        "message": "I was charged twice for order ORD-45231, call me on 9876543210",
        "session_id": "live-1",
    },
)
print("duplicate:", again["duplicate"], "same id:", again["id"] == first["id"])

handoff = call("GET", f"/tickets/{first['id']}/handoff")
print("handoff masked:", handoff["summary"])
print("identifiers:", handoff["identifiers"], "| occurrences:", handoff["occurrences"])
print("next action:", handoff["next_action"])

call("POST", "/admin/time", {"iso": "2026-11-20T22:00:00"})
evening = call(
    "POST",
    "/tickets",
    {"message": "My invoice INV-7711 is wrong", "session_id": "live-2"},
)
print(
    "after hours:",
    evening["team"],
    evening["route_reason"],
    "|",
    evening["scheduled_for"],
)

critical = call(
    "POST",
    "/tickets",
    {"message": "Charged twice for INV-7712", "session_id": "live-3"},
)
print("after hours critical:", critical["team"], critical["route_reason"])

call(
    "POST", "/admin/config", {"key": "routing.teams.billing.available", "value": False}
)
call("POST", "/admin/time", {"iso": "2026-11-20T11:30:00"})
rerouted = call("POST", f"/tickets/{evening['id']}/route")
print(
    "unavailable billing:", rerouted["routing"]["team"], rerouted["routing"]["reason"]
)

listed = call("GET", "/tickets?team=supervisor_queue")
print("overflow queue size:", listed["count"])
