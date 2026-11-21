"""Live check for step 4: real server, simulated clock, temporary database."""

from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8137"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


print("health:", call("GET", "/health")["status"])
print("clock:", call("POST", "/admin/time", {"iso": "2026-11-20T17:00:00"})["now"])

ticket = call(
    "POST", "/tickets", {"subject": "Invoice double charged", "severity": "high"}
)
print("ticket:", ticket["id"], "due", ticket["sla"]["due_at"])

call("POST", "/admin/config", {"key": "sla.by_severity.high", "value": 60})
changed = call("GET", f"/tickets/{ticket['id']}")
print(
    "after override:",
    changed["sla"]["allowed_minutes"],
    "min, due",
    changed["sla"]["due_at"],
)

call("POST", "/admin/time", {"iso": "2026-11-20T18:05:00"})
swept = call("POST", "/admin/sweep", {})
print("sla breaches:", [item["ticket_id"] for item in swept["sla_breaches"]])

final = call("GET", f"/tickets/{ticket['id']}")
print("final status:", final["status"], "| warned:", bool(final["sla_warned_at"]))

records = call("GET", "/escalations")["items"]
match = [r for r in records if r["reason"] == "sla_breach"]
print("escalations:", [(r["reason"], r["queue"]) for r in match])
