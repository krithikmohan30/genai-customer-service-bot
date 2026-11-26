"""Task 6 - multilingual conversations with expiring, restorable sessions.

Three things have to be true at the same time:

* **The language follows the customer, turn by turn.** A session starts in one
  language and may switch mid-conversation, mid-sentence, or into
  transliterated Tamil or Hindi. Every message keeps its own detected language
  and the session stores the latest one; replies use that language.
* **Session state expires but can be restored.** After `sessions.inactivity_minutes`
  (30) without a message the conversation is expired. Within
  `sessions.restore_hours` (24) the same session id comes back with its history;
  after that a fresh session starts and the old one is archived.
* **References survive a language switch.** Order ids, invoice ids, error codes,
  dates and amounts seen in the last `sessions.context_messages` customer turns
  are carried into the current turn, so switching to Tamil without repeating
  `ORD-123` does not lose the order.

Sessions are isolated by id and, when `sessions.bind_customer` is on, by
customer: another customer presenting the id is refused rather than shown
someone else's history.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .. import store
from . import clock, config

MIN_CONTEXT_MESSAGES = 10

_ID = re.compile(
    r"\b(ORDER|ORD|INVOICE|INV|TICKET|TKT)[-_.:#]+\s*([A-Z0-9][A-Z0-9._/-]{2,24})",
    re.I,
)
_ID_PREFIX = {
    "ORDER": "ORD",
    "ORD": "ORD",
    "INVOICE": "INV",
    "INV": "INV",
    "TICKET": "TKT",
    "TKT": "TKT",
}
_ERROR = re.compile(r"\b(?:ERR|ERROR|E)[-_.:# ]?(\d{3,6})\b", re.I)
_AMOUNT = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_DATE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")


class SessionConflict(PermissionError):
    """The session id exists but belongs to a different customer."""


# --------------------------------------------------------------------------- settings


def inactivity_minutes() -> int:
    return max(1, int(config.get("sessions.inactivity_minutes", 30)))


def restore_hours() -> int:
    return max(0, int(config.get("sessions.restore_hours", 24)))


def context_limit() -> int:
    """Never below ten messages: Task 6 requires at least ten turns of context."""
    return max(MIN_CONTEXT_MESSAGES, int(config.get("sessions.context_messages", 10)))


def languages() -> list[str]:
    configured = config.get_list(
        "sessions.languages", ["en", "hi", "ta", "tanglish", "hinglish"]
    )
    return [str(item) for item in configured] or ["en"]


def settings() -> dict:
    return {
        "inactivity_minutes": inactivity_minutes(),
        "restore_hours": restore_hours(),
        "context_messages": context_limit(),
        "languages": languages(),
        "bind_customer": bool(config.get("sessions.bind_customer", True)),
    }


# --------------------------------------------------------------------------- time


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.tz())
    return moment


def _idle_minutes(session: dict, at: datetime) -> float:
    last_seen = _parse(session.get("last_seen_at"))
    if last_seen is None:
        return 0.0
    return max(0.0, (at - last_seen).total_seconds() / 60.0)


def _restore_deadline(session: dict) -> datetime | None:
    stored = _parse(session.get("restore_until"))
    if stored:
        return stored
    last_seen = _parse(session.get("last_seen_at"))
    if last_seen is None:
        return None
    return last_seen + timedelta(minutes=inactivity_minutes() + restore_hours() * 60)


def _windows(at: datetime) -> tuple[str, str]:
    return store.session_windows(at)


# --------------------------------------------------------------------------- lifecycle


def _check_owner(session: dict, customer_id: str | None) -> None:
    if not bool(config.get("sessions.bind_customer", True)):
        return
    if not customer_id:
        return
    owner = session.get("customer_id")
    if owner and owner != customer_id:
        store.audit(
            "session.conflict",
            {"session_id": session.get("id"), "owner": owner},
        )
        raise SessionConflict("session belongs to another customer")


def _expire(session: dict, at: datetime) -> dict:
    expires_at = _parse(session.get("expires_at")) or (
        _parse(session.get("last_seen_at")) or at
    ) + timedelta(minutes=inactivity_minutes())
    restore_until = expires_at + timedelta(hours=restore_hours())
    updated = (
        store.update_session(
            str(session["id"]),
            status="expired",
            expired_at=expires_at.isoformat(),
            expires_at=expires_at.isoformat(),
            restore_until=restore_until.isoformat(),
        )
        or session
    )
    store.audit(
        "session.expired",
        {"session_id": session["id"], "restore_until": restore_until.isoformat()},
    )
    return updated


def _restore(session: dict, at: datetime) -> dict:
    expires_at, restore_until = _windows(at)
    updated = (
        store.update_session(
            str(session["id"]),
            status="active",
            restored_at=at.isoformat(),
            last_seen_at=at.isoformat(),
            expires_at=expires_at,
            restore_until=restore_until,
        )
        or session
    )
    store.audit("session.restored", {"session_id": session["id"], "at": at.isoformat()})
    return updated


def _archive(session: dict, at: datetime, reason: str) -> dict:
    archived = (
        store.update_session(
            str(session["id"]), status="archived", closed_at=at.isoformat()
        )
        or session
    )
    store.audit(
        "session.archived",
        {"session_id": session["id"], "reason": reason},
    )
    return archived


def open_or_resume(
    session_id: str | None = None,
    customer_id: str | None = None,
    language: str | None = None,
    at: datetime | None = None,
) -> dict:
    """Resolve the session for this turn, creating/expiring/restoring as needed."""
    at = at or clock.now()
    previous = store.get_session(session_id) if session_id else None

    if previous:
        _check_owner(previous, customer_id)
        if not previous.get("customer_id") and customer_id:
            store.update_session(str(previous["id"]), customer_id=customer_id)

        status = str(previous.get("status") or "active")
        if status in ("closed", "archived"):
            session = store.create_session(customer_id, language)
            return {
                "session": session,
                "created": True,
                "restored": False,
                "reason": f"previous_{status}",
                "previous_id": previous["id"],
            }

        idle = _idle_minutes(previous, at)
        expired = status == "expired" or idle > inactivity_minutes()
        if expired:
            expired_row = previous if status == "expired" else _expire(previous, at)
            deadline = _restore_deadline(expired_row)
            if deadline and at <= deadline:
                session = _restore(expired_row, at)
                return {
                    "session": session,
                    "created": False,
                    "restored": True,
                    "reason": "resumed_within_restore_window",
                    "previous_id": expired_row["id"],
                }
            _archive(expired_row, at, "restore_window_passed")
            session = store.create_session(customer_id, language)
            return {
                "session": session,
                "created": True,
                "restored": False,
                "reason": "restore_window_passed",
                "previous_id": previous["id"],
            }

        return {
            "session": previous,
            "created": False,
            "restored": False,
            "reason": "active",
            "previous_id": previous["id"],
        }

    session = store.create_session(customer_id, language)
    return {
        "session": session,
        "created": True,
        "restored": False,
        "reason": "new" if not session_id else "unknown_session",
        "previous_id": None,
    }


def note(session: dict, result: dict, at: datetime | None = None) -> dict:
    """Record the turn's language and push the expiry windows forward."""
    at = at or clock.now()
    previous_language = session.get("language")
    language = result.get("language") or previous_language
    expires_at, restore_until = _windows(at)
    store.update_session(
        str(session["id"]),
        status="active",
        language=language,
        last_seen_at=at.isoformat(),
        expires_at=expires_at,
        restore_until=restore_until,
    )
    switched = bool(previous_language and language and previous_language != language)
    if switched:
        store.audit(
            "session.language_switched",
            {
                "session_id": session["id"],
                "from": previous_language,
                "to": language,
                "languages": result.get("languages"),
            },
        )
    turn_languages = list(result.get("languages") or [])
    if language and language not in turn_languages:
        turn_languages.append(language)
    return {
        "language": language,
        "languages": turn_languages,
        "mixed": bool(result.get("mixed")),
        "switched_from": previous_language if switched else None,
        "switched": switched,
    }


def close(
    session_id: str, reason: str = "customer_closed", at: datetime | None = None
) -> dict:
    at = at or clock.now()
    session = store.get_session(session_id)
    if session is None:
        raise KeyError("session not found")
    updated = (
        store.update_session(
            session_id,
            status="closed",
            closed_at=at.isoformat(),
        )
        or session
    )
    store.audit("session.closed", {"session_id": session_id, "reason": reason})
    return updated


def sweep(at: datetime | None = None) -> dict:
    """Expire idle sessions, archive sessions past their restore window."""
    at = at or clock.now()
    expired: list[str] = []
    archived: list[str] = []

    for session in store.list_sessions(status="active", limit=1000):
        if _idle_minutes(session, at) > inactivity_minutes():
            _expire(session, at)
            expired.append(str(session["id"]))

    for session in store.list_sessions(status="expired", limit=1000):
        deadline = _restore_deadline(session)
        if deadline and at > deadline:
            _archive(session, at, "restore_window_passed")
            archived.append(str(session["id"]))

    return {"at": at.isoformat(), "expired": expired, "archived": archived}


# --------------------------------------------------------------------------- context


def messages(session_id: str, limit: int | None = None) -> list[dict]:
    rows = store.recent_messages(session_id, limit or context_limit())
    return [
        {
            "role": row["role"],
            "content": row.get("content"),
            "language": row.get("language"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def references(session_id: str, limit: int | None = None) -> dict:
    """Ids, error codes, amounts and dates seen in recent customer turns.

    Newest first: the most recent reference wins when the same field appears
    more than once. The window is two messages per context slot because
    assistant replies occupy half of the stored turns.
    """
    window = limit or context_limit() * 2
    ids: list[str] = []
    errors: list[str] = []
    amounts: list[str] = []
    dates: list[str] = []

    for row in reversed(store.recent_messages(session_id, window)):
        if row["role"] != "customer":
            continue
        text = str(row.get("content") or "")
        for match in _ID.finditer(text):
            prefix = _ID_PREFIX[match.group(1).upper()]
            body = match.group(2).upper()
            value = body if body.startswith(f"{prefix}-") else f"{prefix}-{body}"
            if value not in ids:
                ids.append(value)
        for match in _ERROR.finditer(text):
            value = f"E-{match.group(1)}"
            if value not in errors:
                errors.append(value)
        for match in _AMOUNT.finditer(text):
            value = match.group(1).replace(",", "")
            if value not in amounts:
                amounts.append(value)
        for match in _DATE.finditer(text):
            value = match.group(1)
            if value not in dates:
                dates.append(value)

    return {"ids": ids, "error_codes": errors, "amounts": amounts, "dates": dates}


def merge_entities(
    session_id: str, entities: dict | None, limit: int | None = None
) -> tuple[dict, list[str]]:
    """Fill entity fields this message left out from earlier turns."""
    merged = dict(entities or {})
    found = references(session_id, limit)
    carried: list[str] = []

    def take(key: str, predicate) -> None:
        if merged.get(key):
            return
        for value in found["ids"]:
            if predicate(value):
                merged[key] = value
                carried.append(key)
                return

    take("order_id", lambda value: value.startswith(("ORD", "ORDER")))
    take("invoice_id", lambda value: value.startswith(("INV", "INVOICE")))
    take("ticket_id", lambda value: value.startswith(("TKT", "TICKET")))
    if not merged.get("error_code") and found["error_codes"]:
        merged["error_code"] = found["error_codes"][0]
        carried.append("error_code")
    if not merged.get("amount") and found["amounts"]:
        merged["amount"] = found["amounts"][0]
        carried.append("amount")
    if not merged.get("date") and found["dates"]:
        merged["date"] = found["dates"][0]
        carried.append("date")
    return merged, carried


def describe(
    session: dict,
    lifecycle: dict | None = None,
    language: dict | None = None,
) -> dict:
    """Stable shape for the chat response and the UI."""
    lifecycle = lifecycle or {}
    language = language or {}
    return {
        "id": session.get("id"),
        "status": session.get("status"),
        "customer_id": session.get("customer_id"),
        "language": language.get("language") or session.get("language"),
        "languages": language.get("languages") or [],
        "mixed": bool(language.get("mixed")),
        "switched_from": language.get("switched_from"),
        "created": bool(lifecycle.get("created")),
        "restored": bool(lifecycle.get("restored")),
        "lifecycle": lifecycle.get("reason"),
        "expires_at": session.get("expires_at"),
        "restore_until": session.get("restore_until"),
        "context_messages": context_limit(),
    }
