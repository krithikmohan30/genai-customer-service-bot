"""Business calendar.

Every hours/holiday question in the build (on-call routing, next-working-day
scheduling, SLA clocks, maintenance windows) resolves through here, and every
`now` comes from the injected clock, so simulated time changes are testable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import clock, config

MAX_LOOKAHEAD_DAYS = 60


def _parse_hhmm(value: str, default: str) -> time:
    raw = str(value or default)
    hours, _, minutes = raw.partition(":")
    return time(int(hours), int(minutes or 0))


def start_time() -> time:
    return _parse_hhmm(config.get("business_hours.start", "09:30"), "09:30")


def end_time() -> time:
    return _parse_hhmm(config.get("business_hours.end", "18:30"), "18:30")


def working_days() -> list[int]:
    return [
        int(day)
        for day in config.get_list("business_hours.working_days", [0, 1, 2, 3, 4])
    ]


def holidays() -> set[str]:
    return {str(day) for day in config.get_list("business_hours.holidays", [])}


def is_working_day(day: date) -> bool:
    return day.weekday() in working_days() and day.isoformat() not in holidays()


def window(day: date) -> tuple[datetime, datetime]:
    zone = clock.tz()
    return (
        datetime.combine(day, start_time(), tzinfo=zone),
        datetime.combine(day, end_time(), tzinfo=zone),
    )


def is_open(moment: datetime | None = None) -> bool:
    moment = moment or clock.now()
    if not is_working_day(moment.date()):
        return False
    opens, closes = window(moment.date())
    return opens <= moment <= closes


def next_working_day(moment: datetime | None = None) -> date:
    """The next working day *after* `moment`'s day."""
    moment = moment or clock.now()
    day = moment.date() + timedelta(days=1)
    for _ in range(MAX_LOOKAHEAD_DAYS):
        if is_working_day(day):
            return day
        day += timedelta(days=1)
    raise RuntimeError("no working day found in the next 60 days")


def next_working_start(moment: datetime | None = None) -> datetime:
    """When an after-hours message will first be seen: next day 09:30."""
    moment = moment or clock.now()
    if is_working_day(moment.date()):
        opens, closes = window(moment.date())
        if moment < opens:
            return opens
        if moment <= closes:
            return moment
    day = next_working_day(moment)
    return window(day)[0]


def next_working_moment(moment: datetime | None = None) -> datetime:
    """Next instant at which agents are working, moving past the weekend."""
    moment = moment or clock.now()
    if is_open(moment):
        return moment
    return next_working_start(moment)


def business_minutes_between(start: datetime, end: datetime) -> int:
    """Working minutes between two instants, excluding weekends and holidays."""
    if end <= start:
        return 0
    total = 0
    day = start.date()
    guard = 0
    while day <= end.date() and guard < 400:
        guard += 1
        if is_working_day(day):
            opens, closes = window(day)
            overlap_start = max(start, opens)
            overlap_end = min(end, closes)
            if overlap_end > overlap_start:
                total += int((overlap_end - overlap_start).total_seconds() // 60)
        day += timedelta(days=1)
    return total


def add_business_minutes(start: datetime, minutes: int) -> datetime:
    """Add working minutes to an instant, skipping closed hours."""
    remaining = max(0, int(minutes))
    cursor = next_working_moment(start)
    guard = 0
    while remaining > 0 and guard < 400:
        guard += 1
        opens, closes = window(cursor.date())
        available = int((closes - cursor).total_seconds() // 60)
        if remaining <= available:
            return cursor + timedelta(minutes=remaining)
        remaining -= available
        cursor = next_working_start(closes + timedelta(seconds=1))
    return cursor
