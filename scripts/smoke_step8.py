"""Live check for step 8: language switching, context, isolation, expiry, restore."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8139"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def chat(message: str, session_id: str | None = None, customer_id: str | None = None):
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    if customer_id:
        body["customer_id"] = customer_id
    status, payload = call("POST", "/chat", body)
    assert status == 200, payload
    return payload


def show(label: str, payload: dict) -> None:
    print(
        label,
        json.dumps(
            {
                "language": payload["language"],
                "languages": payload.get("languages"),
                "mixed": payload.get("mixed"),
                "switched_from": payload["session"].get("switched_from"),
                "session": payload["session_id"][:8],
                "carried": payload.get("carried_entities"),
                "references": payload.get("references"),
            },
            ensure_ascii=True,
        ),
    )


call("POST", "/admin/time", {"iso": "2026-11-24T10:00:00"})

print("--- english to tamil")
first = chat("My order ORD-55551 has not arrived", customer_id="smoke-switch")
show("en:", first)
tamil = chat(
    "\u0b87\u0ba8\u0bcd\u0ba4 \u0b86\u0bb0\u0bcd\u0b9f\u0bb0\u0bcd \u0b87\u0ba9\u0bcd\u0ba9\u0bc1\u0bae\u0bcd \u0bb5\u0bb0\u0bb5\u0bbf\u0bb2\u0bcd\u0bb2\u0bc8",
    session_id=first["session_id"],
    customer_id="smoke-switch",
)
show("ta:", tamil)
print(
    "tamil reply has tamil chars:",
    any("\u0b80" <= c <= "\u0bff" for c in tamil["reply"]),
)

print("--- mixed and transliterated")
show(
    "tanglish+en:",
    chat("Order ORD-77771 refund aagala, please check", customer_id="smoke-mixed"),
)
show(
    "tamil+en:",
    chat(
        "\u0b8e\u0ba9\u0bcd order ORD-77772 refund \u0b86\u0b95\u0bb2",
        customer_id="smoke-mixed",
    ),
)
show(
    "hinglish:",
    chat("mera order ORD-88882 kaha hai, jaldi karo", customer_id="smoke-mixed"),
)
show(
    "hindi:",
    chat(
        "\u092e\u0947\u0930\u093e \u0911\u0930\u094d\u0921\u0930 ORD-88881 \u0915\u0939\u093e\u0901 \u0939\u0948?",
        customer_id="smoke-mixed",
    ),
)

print("--- context across ten messages")
first = chat(
    "Invoice INV-12345 dated 12/11/2026 for Rs 4,500 failed with error E-402",
    customer_id="smoke-context",
)
session_id = first["session_id"]
for text in ("Any update?", "Okay", "Thanks", "Still waiting", "Please check"):
    chat(text, session_id=session_id, customer_id="smoke-context")
switched = chat(
    "\u0b87\u0ba8\u0bcd\u0ba4 \u0baa\u0bbf\u0bb0\u0b9a\u0bcd\u0b9a\u0bbf\u0ba9\u0bc8 \u0b87\u0ba9\u0bcd\u0ba9\u0bc1\u0bae\u0bcd \u0ba4\u0bc0\u0bb0\u0bb5\u0bbf\u0bb2\u0bcd\u0bb2\u0bc8",
    session_id=session_id,
    customer_id="smoke-context",
)
show("after five fillers:", switched)
print("entities:", json.dumps(switched["entities"], ensure_ascii=True))

print("--- isolation")
status, payload = call(
    "POST",
    "/chat",
    {
        "message": "show me",
        "session_id": switched["session_id"],
        "customer_id": "smoke-other",
    },
)
print("other customer:", status, payload.get("detail"))

print("--- expiry and restore")
first = chat("Order ORD-61616 was charged twice", customer_id="smoke-expiry")
call("POST", "/admin/time", {"advance_minutes": 31})
resumed = chat(
    "Any update?", session_id=first["session_id"], customer_id="smoke-expiry"
)
print(
    "resumed:",
    resumed["session_id"] == first["session_id"],
    resumed["session"]["restored"],
    resumed["session"]["lifecycle"],
    resumed["entities"].get("order_id"),
)

print("--- restore window passed")
call("POST", "/admin/time", {"advance_hours": 26})
fresh = chat(
    "Hello again", session_id=resumed["session_id"], customer_id="smoke-expiry"
)
print(
    "new session:",
    fresh["session_id"] != resumed["session_id"],
    fresh["session"]["lifecycle"],
)
_, old = call("GET", f"/sessions/{resumed['session_id']}")
print("old status:", old["session"]["status"])

print("--- sweep and settings")
_, sweep = call("POST", "/sessions/sweep")
print("sweep archived:", len(sweep["archived"]))
status, settings = call("GET", "/sessions/settings")
print("settings:", json.dumps(settings, ensure_ascii=True))

print("--- close")
first = chat("One small question", customer_id="smoke-close")
call("POST", f"/sessions/{first['session_id']}/close", {"reason": "smoke"})
again = chat(
    "Actually one more", session_id=first["session_id"], customer_id="smoke-close"
)
print(
    "closed then reopened:",
    again["session_id"] != first["session_id"],
    again["session"]["lifecycle"],
)
