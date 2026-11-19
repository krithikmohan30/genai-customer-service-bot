# Progress log

Daily record of what was built, what was verified, and what is next. Doubles as
the internship daily report.

## 2026-11-18 — Step 1: foundations

Built the shared layer that four of the six internship tasks depend on.

**Added**

| File | Purpose |
|---|---|
| `config.yaml` | All tunables: hours, holidays, SLA, routing, retention, session timings |
| `app/core/config.py` | Dotted-key reads plus runtime overrides (no restart) |
| `app/core/clock.py` | Injectable clock: `now`, `set_time`, `advance`, `reset` |
| `app/core/security.py` | PII detection and masking (card, Aadhaar, PAN, UPI, email, OTP, phone) |
| `app/core/llm.py` | `google-genai` wrapper: `generate`, `generate_json`, fence stripping |
| `app/core/triage.py` | One classification call: sentiment, risk, language, entities, intent |
| `app/db.py` | SQLite schema for sessions, messages, escalations, tickets, docs, uploads, jobs, audit |
| `app/store.py` | All SQL for sessions, messages, escalations, audit |
| `app/main.py` | `/health`, `/chat`, `/sessions/{id}`, `/escalations`, `/admin/time`, `/admin/config`, `/admin/audit` |
| `tests/` | 10 acceptance tests, deterministic (rules triage, temp DB, pinned clock) |

**Verified**

- `python -m pytest tests -q` → 10 passed in 0.78s.
- `GET /health` → clock in Asia/Kolkata, DB path, triage mode.
- Live LLM triage (`source: llm+rules` in ~2–3s):
  - *"Great, another delay on my refund. Third time I am asking."* → `sarcastic`, tone `calm_professional`, not escalated.
  - *"No rush, but someone changed my password and it wasn't me."* → `neutral` sentiment, `high` risk, escalated to `trust_and_safety` with reason `account_compromise`.
- Simulated time: froze to 2026-11-18 22:30, advanced 3h → 2026-11-19 01:30, reset to live.

**Decisions**

- Sentiment and risk are separate fields. Escalation reads risk, so a calm high-risk message still escalates.
- Rules always run; the model only refines. Sarcasm detected by rules cannot be downgraded by the model.
- Every timestamp comes from `clock.now()`. No `datetime.now()` inside `app/`.
- Fixed a deadlock found during testing: `clock.state()` re-entered a non-reentrant lock.

**Next:** Step 2 — policy RAG with metadata, dates, access control, citations (Task 4).

## 2026-11-19 — Step 2: policy retrieval (Task 4)

**Added**

| File | Purpose |
|---|---|
| `scripts/build_policies.py` | Builds `data/policies/index.json` from the training CSV plus curated policy files |
| `data/policies/manual/*.md` | 10 policies with front matter: version, product, region, access level, effective, expiry |
| `app/core/policy.py` | Access filtering, date filtering, version resolution, citations, refusal, grounding gate |
| `app/core/injection.py` | Instruction detection and sentence-level stripping |
| `app/core/embeddings.py` | Shared MiniLM encoder (same model as the training project) |
| `scripts/policy_scores.py` | Reproducible score table in `results/policy_scores.txt` |
| `tests/test_step2_policy.py` | 20 tests, one per hidden-evaluation hint |
| `GET /policy/stats`, `POST /policy/search`, `POST /policy/ask` | New endpoints |

**Index:** 86 documents — 76 FAQ records from `dataset/dataset.csv` plus 10 policies
(refund IN v1/v2, refund US, EMI, certificate, course fee from 2027, course access,
billing dispute, support hours, internal account-security runbook).

**Verified**

- `python -m pytest tests -q` → 31 passed.
- Good matches score 0.39–0.76; noise scores 0.10–0.17. Floor set to **0.28** from that gap.
- Live LLM answers (`policy.mode=auto`):
  - "What is the refund policy in India?" → 14-day policy, cited `refund_policy_in_v2` v2.
  - "What was the refund window in March 2025?" → 7-day policy, cited `refund_policy_in_v1` v1, `as_of=2025-03-01` inferred from text.
  - "What are the internal steps for a hacked account?" as a customer → **refused, reason `restricted`**; as an agent → answered from the agent-only runbook.
  - "When is support available?" → answered from `support_hours_v1` with three injected instructions stripped (`ignore_instructions`, `role_override`, `citation_suppression`); none reached the answer.
  - "Who won the 2011 cricket world cup?" → refused, `no_evidence`.
- Grounding gate tested with a stubbed model: an invented "45 days" is refused as `unsupported_claims`; an uncited sentence is refused as `uncited_answer`.

**Decisions**

- Rules and dates are applied before ranking, so an expired or future policy cannot win on similarity.
- Highest version per topic wins; a policy outranks an FAQ row when the two tie.
- Region defaults to IN, but "in the USA" in the question switches retrieval to the US policy.
- Extractive mode exists so tests never need the network; the LLM path is exercised live and by stubbed tests.
- Fixed a real config bug found here: `config.get("policy.access_levels")` returned `{}` because nested mappings were flattened. Added `get_section()` and a section fallback in `get()`.

**Correction:** the dataset is **76 FAQ records** spread over 253 lines, not 253 rows. Confirmed from the CSV reader and the FAISS docstore (76 documents). README corrected.

**Next:** Step 3 — sentiment escalation, on-call queue, 15-minute timer (Task 1).
