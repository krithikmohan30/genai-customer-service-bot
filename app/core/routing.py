"""Task 2b - skill, availability and workload routing.

A ticket carries a category; the category needs skills; the skills decide which
teams can take it. Among the teams that can, the one with the best mix of skill
match and spare capacity wins.

Two situations override the score:

* **unavailable** - a team is outside its availability window, switched off, or
  at capacity. It cannot be chosen just because it is the best skill match.
* **after hours** - nobody is working. Security work still goes to the 24x7
  trust-and-safety desk, genuinely critical work goes to on-call, and everything
  else waits for the next working morning instead of silently queueing overnight.

Every decision returns the reason and the numbers behind it, so a routing choice
can be explained after the fact.
"""

from __future__ import annotations

from datetime import datetime

from .. import store
from . import clock, config
from . import calendar as work_calendar

# Category (triage intent) -> the skills a team must offer to take the ticket.
# "support" is the wildcard: no team owns it, so every team gets a fair chance.
SKILL_MAP: dict[str, list[str]] = {
    "billing_dispute": ["billing", "payment", "refund"],
    "refund_request": ["refund", "billing"],
    "billing_question": ["billing"],
    "access_issue": ["login", "technical", "account"],
    "account_security": ["account", "security"],
    "legal_escalation": ["legal", "security"],
    "complaint": ["support"],
    "urgent_issue": ["support"],
    "general_question": ["support"],
}


def needed_skills(category: str | None) -> list[str]:
    return list(SKILL_MAP.get(str(category or "").lower(), ["support"]))


def teams() -> dict:
    return config.get("routing.teams", {}) or {}


def queue(name: str, fallback: str) -> str:
    return str(config.get(f"routing.{name}", fallback))


def _availability(team_config: dict) -> str:
    return str(team_config.get("availability", "business_hours")).lower()


def available(team_config: dict, at: datetime | None = None) -> bool:
    """Explicit switches win; otherwise read the availability window."""
    if team_config.get("available") is False:
        return False
    if team_config.get("available") is True:
        return True
    if _availability(team_config) == "24x7":
        return True
    return work_calendar.is_open(at)


def capacity(team_config: dict) -> int:
    return max(1, int(team_config.get("capacity", 5)))


def load(team_name: str) -> int:
    return len(store.open_tickets_by_team(team_name))


def _skill_score(team_skills: list[str], skills: list[str]) -> float:
    if "support" in skills:
        return 0.4  # wildcard: any team may take a generic complaint
    if not team_skills:
        return 0.0
    overlap = len(set(team_skills) & set(skills))
    return round(overlap / len(skills), 3)


def candidates(skills: list[str], at: datetime | None = None) -> list[dict]:
    """Every configured team with its current score, availability and load."""
    at = at or clock.now()
    rows: list[dict] = []
    for name, team_config in teams().items():
        if not isinstance(team_config, dict):
            continue
        team_skills = [str(skill) for skill in (team_config.get("skills") or [])]
        skill_score = _skill_score(team_skills, skills)
        if skill_score <= 0:
            continue
        cap = capacity(team_config)
        current = load(name)
        free_ratio = max(0.0, (cap - current) / cap)
        is_available = available(team_config, at) and current < cap
        score = (
            0.6 * skill_score
            + 0.25 * free_ratio
            + 0.15 * (1.0 if is_available else 0.0)
        )
        rows.append(
            {
                "team": name,
                "score": round(score, 3),
                "skills": team_skills,
                "skill_match": skill_score,
                "load": current,
                "capacity": cap,
                "available": is_available,
                "availability": _availability(team_config),
            }
        )
    return sorted(rows, key=lambda row: (row["available"], row["score"]), reverse=True)


def route(ticket: dict, at: datetime | None = None) -> dict:
    """Choose a team (or a waiting queue) and explain why."""
    at = at or clock.now()
    open_now = work_calendar.is_open(at)
    skills = needed_skills(ticket.get("category"))
    severity = str(ticket.get("severity") or "medium").lower()
    ranked = candidates(skills, at)
    wildcard = "support" in skills
    pick = next(
        (row for row in ranked if row["available"] and (open_now or not wildcard)),
        None,
    )

    if pick:
        return {
            "team": pick["team"],
            "reason": "skills+capacity" if open_now else "after_hours_24x7",
            "skills": skills,
            "scheduled_for": None,
            "open_now": open_now,
            "score": pick["score"],
            "candidates": ranked,
            "at": at.isoformat(),
        }

    if not open_now:
        if severity == "critical":
            return {
                "team": queue("on_call_team", "on_call_support"),
                "reason": "after_hours_on_call",
                "skills": skills,
                "scheduled_for": None,
                "open_now": open_now,
                "score": None,
                "candidates": ranked,
                "at": at.isoformat(),
            }
        return {
            "team": queue("next_day_team", "next_working_day"),
            "reason": "after_hours_next_day",
            "skills": skills,
            "scheduled_for": work_calendar.next_working_start(at).isoformat(),
            "open_now": open_now,
            "score": None,
            "candidates": ranked,
            "at": at.isoformat(),
        }

    reason = "no_capacity" if ranked else "no_skill_match"
    return {
        "team": queue("overflow_team", "supervisor_queue"),
        "reason": reason,
        "skills": skills,
        "scheduled_for": None,
        "open_now": open_now,
        "score": None,
        "candidates": ranked,
        "at": at.isoformat(),
    }


def apply(ticket: dict, at: datetime | None = None) -> dict:
    """Route the ticket and persist the outcome."""
    decision = route(ticket, at)
    store.update_ticket(
        str(ticket["id"]),
        team=decision["team"],
        route_reason=decision["reason"],
        scheduled_for=decision.get("scheduled_for"),
    )
    store.audit(
        "ticket.routed",
        {
            "ticket_id": ticket["id"],
            "team": decision["team"],
            "reason": decision["reason"],
        },
    )
    return decision
