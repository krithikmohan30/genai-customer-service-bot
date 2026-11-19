"""Injectable clock.

Four of the six internship tasks are graded on simulated time changes, so every
timestamp in the app must come from here. No `datetime.now()` inside app/.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

_lock = threading.RLock()
_fixed: datetime | None = None
_offset = timedelta(0)


def tz() -> ZoneInfo:
    return ZoneInfo(str(config.get("app.timezone", "Asia/Kolkata")))


def now() -> datetime:
    with _lock:
        if _fixed is not None:
            return _fixed.astimezone(tz())
        return (datetime.now(timezone.utc) + _offset).astimezone(tz())


def now_iso() -> str:
    return now().isoformat()


def set_time(iso: str | None) -> str:
    """Freeze the clock at `iso`, or unfreeze with None."""
    global _fixed
    with _lock:
        if iso is None:
            _fixed = None
        else:
            moment = datetime.fromisoformat(iso)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tz())
            _fixed = moment.astimezone(timezone.utc)
    return now_iso()


def advance(
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
) -> str:
    """Move the clock forward, frozen or live."""
    delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    global _fixed, _offset
    with _lock:
        if _fixed is not None:
            _fixed += delta
        else:
            _offset += delta
    return now_iso()


def reset() -> str:
    global _fixed, _offset
    with _lock:
        _fixed = None
        _offset = timedelta(0)
    return now_iso()


def state() -> dict:
    with _lock:
        return {
            "frozen": _fixed is not None,
            "offset_seconds": int(_offset.total_seconds()),
            "now": now_iso(),
        }
