"""Step 2 acceptance: metadata-aware policy retrieval (Task 4)."""

from __future__ import annotations

import pytest

from app.core import config, policy


# --------------------------------------------------------------------------- helpers


def ask(question: str, **kwargs) -> dict:
    return policy.answer(question, **kwargs)


def cited(result: dict) -> list[str]:
    return [citation["id"] for citation in result["citations"]]


# --------------------------------------------------------------------------- versions


def test_latest_applicable_version_wins():
    result = ask("What is the refund policy?")

    assert result["status"] == "answered"
    assert cited(result)[0] == "refund_policy_in_v2"
    assert "14" in result["answer"]
    assert "[1]" in result["answer"]


def test_expired_version_is_ignored_today():
    result = ask("What is the refund policy?")
    assert "refund_policy_in_v1" not in cited(result)


def test_historical_question_returns_the_version_active_thend():
    result = ask("What was the refund window in March 2025?")

    assert result["status"] == "answered"
    assert cited(result)[0] == "refund_policy_in_v1"
    assert "7" in result["answer"]


def test_explicit_as_of_date_overrides_the_question():
    old = ask("What is the refund policy?", as_of="2025-03-01")
    new = ask("What is the refund policy?", as_of="2026-11-19")

    assert cited(old)[0] == "refund_policy_in_v1"
    assert cited(new)[0] == "refund_policy_in_v2"


def test_future_policy_is_not_used_for_a_current_question():
    result = ask("What is the course fee?", as_of="2026-11-19")
    assert "course_fee_2027" not in cited(result)


def test_future_policy_applies_once_it_is_in_force():
    result = ask("What is the course fee?", as_of="2027-02-01")
    assert result["status"] == "answered"
    assert "course_fee_2027" in cited(result)


# --------------------------------------------------------------------------- access


def test_restricted_document_is_refused_for_a_customer():
    result = ask("What are the internal steps for a hacked account?")

    assert result["status"] == "refused"
    assert result["reason"] == "restricted"
    assert "account_security_internal_v1" not in cited(result)


def test_restricted_document_is_available_to_an_agent():
    result = ask("What are the internal steps for a hacked account?", requester="agent")

    assert result["status"] == "answered"
    assert cited(result)[0] == "account_security_internal_v1"


# --------------------------------------------------------------------------- safety


def test_injected_instructions_in_a_document_are_stripped():
    result = ask("When is support available?")

    assert result["status"] == "answered"
    assert "ignore_instructions" in result["blocked_instructions"]
    assert "administrator mode" not in result["answer"].lower()
    assert "approved instantly" not in result["answer"].lower()
    assert "09:30" in result["answer"] or "9:30" in result["answer"]


def test_no_evidence_refuses_instead_of_guessing():
    result = ask("What is the capital of France?")

    assert result["status"] == "refused"
    assert result["reason"] == "no_evidence"
    assert result["citations"] == []


def test_agent_answers_are_pii_masked_and_cited():
    result = ask(
        "My invoice shows a duplicate charge on a.b@example.com, what happens?"
    )
    assert result["status"] == "answered"
    assert result["citations"]
    assert "a.b@example.com" not in result["answer"]


# --------------------------------------------------------------------------- generation gate


def test_invented_number_is_rejected(monkeypatch):
    config.set_override("policy.mode", "auto")
    monkeypatch.setattr(policy.llm, "available", lambda: True)
    monkeypatch.setattr(
        policy.llm,
        "generate",
        lambda *a, **k: "You have 45 days to claim a refund [1].",
    )

    result = ask("How many days do I have to request a refund?")

    assert result["status"] == "refused"
    assert result["reason"] == "unsupported_claims"
    assert "45" in result["unsupported"]


def test_grounded_answer_passes_through(monkeypatch):
    config.set_override("policy.mode", "auto")
    monkeypatch.setattr(policy.llm, "available", lambda: True)
    monkeypatch.setattr(
        policy.llm,
        "generate",
        lambda *a, **k: "You have 14 days from payment to request a refund [1].",
    )

    result = ask("How many days do I have to request a refund?")

    assert result["status"] == "answered"
    assert cited(result) == ["refund_policy_in_v2"]


def test_uncited_answer_is_refused(monkeypatch):
    config.set_override("policy.mode", "auto")
    monkeypatch.setattr(policy.llm, "available", lambda: True)
    monkeypatch.setattr(
        policy.llm, "generate", lambda *a, **k: "You have 14 days to request a refund."
    )

    result = ask("How many days do I have to request a refund?")

    assert result["status"] == "refused"
    assert result["reason"] == "uncited_answer"


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "text,expected",
    [
        ("What was the refund window in March 2025?", "2025-03-01"),
        ("Policy on 2025-03-01 please", "2025-03-01"),
        ("What did it cost back in 2024?", "2024-01-01"),
        ("What is the refund policy?", None),
    ],
)
def test_reference_dates_are_extracted(text, expected):
    assert policy.date_from_text(text) == expected


# --------------------------------------------------------------------------- endpoint


def test_policy_ask_endpoint(client):
    response = client.post(
        "/policy/ask", json={"question": "What is the refund policy in India?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["citations"][0]["id"] == "refund_policy_in_v2"


def test_policy_region_metadata_is_respected():
    india = ask("What is the refund policy?")
    usa = ask("What is the refund policy in the USA?")
    assert cited(india)[0] == "refund_policy_in_v2"
    assert cited(usa)[0] == "refund_policy_us_v1"
