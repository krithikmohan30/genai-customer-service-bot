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

## 2026-11-20 — Step 3: sentiment escalation (Task 1)

**Added**

| File | Purpose |
|---|---|
| `app/core/calendar.py` | Working days, holidays, business minutes, next-working-start |
| `app/core/escalation.py` | Five triggers, escalation records, masked conversation summary, 15-minute sweep |
| `app/core/response.py` | Tone-adjusted replies in 5 languages, policy text left byte-identical |
| `POST /admin/sweep`, `GET /admin/calendar` | Timer evaluation and calendar inspection |
| `tests/test_step3_escalation.py` | 25 tests covering every hidden-evaluation hint |
| `scripts/smoke_step3.py` | Live multilingual smoke test |

**Escalation triggers, in priority order**

1. High risk (account compromise, duplicate payment, legal threat, fraud) → `trust_and_safety`
2. Urgent outside business hours → `on_call_support`
3. Three negative turns in a row → `supervisor_queue`
4. Complaint outside business hours → `next_working_day`, scheduled for the next working 09:30
5. Negative conversation open for 15 minutes → `supervisor_queue`

Every record stores reason, condition string, sentiment, masked summary, queue and (where relevant) the scheduled callback time.

**Verified**

- `python -m pytest tests -q` → **56 passed in 26s**.
- After-hours urgent at 22:15 Friday → `after_hours_urgent` → `on_call_support`.
- After-hours complaint at 22:00 Friday → `after_hours_complaint`, `scheduled_for` = Monday 09:30.
- Repeat negative during hours → `repeated_negative` on the third turn only.
- Negative at 11:00, clock advanced 16 minutes, `POST /admin/sweep` → `unresolved_15m`, condition `unresolved_minutes>=15`.
- Resolved conversation (customer says "it works now") is not swept.
- Weekend arithmetic: Friday 18:00 → Monday 10:30 = 90 business minutes.
- Multilingual live: Hindi complaint → `negative`, reply in Hindi; Tamil → `urgent`, reply in Tamil; Hinglish → `frustrated`, reply in Hinglish; Tanglish detected.

**Two model defects found and fixed**

1. The model labelled *"I can't access my account"* as account compromise, which sent an after-hours urgent to the security queue. A risk category is now accepted only when the message contains evidence for it (`triage._merge`). High risk with no evidenced category is demoted to medium, so the urgent/after-hours rule wins.
2. The model labelled a Hindi complaint as neutral. Rules now win when an explicit complaint keyword is present and the model returns positive or neutral. The model can add detail; it cannot overrule its own safety net.

Both cases are covered by unit tests that stub the model output.

**Next:** Step 4 — business calendar SLA engine: 75% warning and breach escalation (Task 2a).

## 2026-11-21 — Step 4: SLA clocks (Task 2a)

**Added**

| File | Purpose |
|---|---|
| `app/core/sla.py` | Business-minute SLA per severity, 75% warning, breach escalation, idempotent sweep |
| `tickets` store helpers | `create_ticket`, `get_ticket`, `open_tickets`, `update_ticket`; `sla_breached_at` column |
| `POST /tickets`, `GET /tickets`, `GET /tickets/{id}` | Raise and inspect tickets with live SLA position |
| `POST /admin/sweep` (extended) | Now returns `sla_warnings` and `sla_breaches` alongside escalation timers |
| `tests/test_step4_sla.py` | 6 tests: weekend, holiday, 75% warning, single breach, runtime change, endpoints |
| `scripts/smoke_step4.py` | Live server check with simulated time |

**How the clock works**

- Allowed time is measured in business minutes, so weekends and holidays cost nothing.
- Target by severity: critical 60, high 240, medium 480, low 1440 minutes.
- Due time = creation + allowed business minutes; warning at 75% consumed (both configurable).
- Warning is recorded once on the ticket (`sla_warned_at`); a breach sets `sla_breached_at`, marks the ticket `escalated`, and files one `sla_breach` escalation to `supervisor_queue` with the reason, condition and masked subject.
- Everything reads configuration on each call, so a runtime change applies to tickets already open.

**Verified**

- `python -m pytest tests -q` → **62 passed in 62s**.
- Friday 17:00, medium (480 min): 90 minutes Friday, 390 Monday → due Monday 16:00; all weekend minutes cost zero.
- 13:59 Monday = 359/480 (`ok`); 14:00 = 360/480 → `warning`.
- 16:01 Monday → `breached`, one escalation created; a second sweep creates nothing.
- 25 Dec holiday: Thursday 17:00 ticket is due Monday 16:00, not Friday.
- Runtime `sla.by_severity.high: 240 → 60` and `warn_ratio: 0.75 → 0.5` re-dated an open ticket from Monday 12:00 to Friday 18:00, warning at 17:30.
- Live server (temp DB): ticket due Monday 12:00 → override to 60 min → due Friday 18:00 → at 18:05 sweep reported the breach, status `escalated`, one `sla_breach` record in `supervisor_queue`.

**Decisions**

- SLA uses the same calendar module as after-hours routing, so there is one source of truth for working time.
- Hidden tests may move the clock backwards; `business_minutes_between` returns 0 when end <= start.
- Tickets exposed now with explicit fields; Step 5 fills them from the conversation (extraction, priority scoring, routing, dedupe).

**Next:** Step 5 — ticket extraction, priority score, skill routing, dedupe and SLA handoff summary (Task 2b).

## 2026-11-22 — Step 5: structured tickets (Task 2b)

**Added**

| File | Purpose |
|---|---|
| `app/core/tickets.py` | Extraction, weighted priority, dedupe/grouping, masked handoff |
| `app/core/routing.py` | Skill match, availability, workload and business-hours routing |
| Ticket columns | `customer_id`, `category`, `impact`, `group_id`, `duplicate_of`, `route_reason`, `scheduled_for` |
| `POST /tickets` (extended) | Accepts `message` and extracts the ticket, or takes explicit fields |
| `GET /tickets` filters, `POST /tickets/{id}/route`, `POST /tickets/{id}/close`, `GET /tickets/{id}/handoff` | Ticket lifecycle |
| `/chat` | Now creates or merges a ticket when the intent needs follow-up work |
| `tests/test_step5_tickets.py` | 13 tests: extraction, priority, routing, dedupe, handoff, chat, API |
| `scripts/smoke_step5.py` | Live server check for the whole ticket path |

**Structured ticket**

Subject, masked issue, category, severity, impact, priority, team, route reason, scheduled time and the SLA clock. Severity comes from risk first, then urgency: high risk → critical, urgent → high, negative tone → medium, calm informational → low.

**Priority (0–100)**

Weighted components, all runtime-configurable under `priority.weights`: severity 40, sentiment 20, impact 20, wait 10, SLA pressure 10. Impact is organisation / team / individual, detected from scope words. SLA pressure rises as the clock is consumed, so wait time and nearing breach both lift the score.

**Routing**

- Candidates must match the category's skills (or be a legitimate wildcard for complaints).
- A team outside its availability window, switched off, or at capacity is not eligible.
- Best score wins: 60% skill match, 25% spare capacity, 15% availability.
- After hours: a specific 24x7 match still routes (trust & safety); critical work goes to on-call; everything else is queued for the next working 09:30.
- No eligible team during hours → `supervisor_queue` with reason `no_capacity` or `no_skill_match`. The ticket is never dropped.

**Dedupe and grouping**

Identity is `category + order id/error code/amount/product`. Without an identifier the normalised subject is used, and only within a 30-minute window. A duplicate is merged into the open primary ticket: occurrence count rises, the highest priority is kept, and both share one `group_id`. Distinct order ids stay distinct.

**Verified**

- `python -m pytest tests -q` → **75 passed in 50s**.
- Live server (temp DB): charged-twice message → `billing_dispute`, critical, priority 66, team billing, SLA due 12:00 within the hour; identical message again → duplicate, same ticket, occurrences 2.
- Handoff: phone masked to `[PHONE]`, order id preserved, next action names the team and the due time.
- Friday 22:00 invoice → `next_working_day`, scheduled Monday 09:30; charged-twice after hours → `on_call_support`; billing switched off during hours → `supervisor_queue` / `no_capacity`.
- Calm "What is the refund policy?" creates no ticket; "Why is my refund delayed?" does.

**Model defects found and fixed**

1. The model's generic intent ("complaint") was flattening evidence-based intents (`billing_dispute`, `account_security`). Rules now keep a specific intent; the model may only fill in when rules see nothing beyond `general_question`.
2. Order ids were also being reported as error codes. `extract_entities` no longer duplicates the same token under both keys.
3. A calm policy question was classified `negative` because the word "refund" is in the negative lexicon. Ticket creation now requires a complaint signal in an informational question.
4. After hours, the wildcard complaint path could land on the 24x7 security desk. A 24x7 match now requires a specific skill match; generic complaints wait for the working day.

Also added a 20s HTTP timeout to the Gemini client so a hung model call falls back to the rules classifier instead of blocking the request.

**Next:** Step 6 — knowledge-base ingestion pipeline: change detection, dedupe, quarantine, versions and rollback (Task 3).

## 2026-11-23 — Step 6: knowledge-base pipeline (Task 3)

**Added**

| File | Purpose |
|---|---|
| `app/core/knowledge.py` | Scan, parse, gate, version, activate, roll back, health-check, retry |
| `documents` / `document_versions` / `jobs` | Extended with titles, quality, access, PII flag, health state, job errors |
| `store.py` | Job queue helpers and document/version storage |
| `POST /kb/scan`, `POST /kb/sweep`, `GET /kb/stats` | Pipeline control and monitoring |
| `GET /kb/docs`, `GET /kb/docs/{id}`, `GET /kb/docs/{id}/versions`, `GET /kb/search` | Access-filtered reads |
| `POST /kb/docs/{id}/activate`, `POST /kb/docs/{id}/rollback` | Window-aware activation and rollback |
| `tests/test_step6_knowledge.py` | 12 tests covering every hidden-evaluation hint |
| `scripts/smoke_step6.py` | Live pipeline run with a temporary incoming folder |

**Pipeline**

1. **Scan** the incoming directory, rejecting unknown file types.
2. **Parse** YAML front matter; the document id comes from the file, never from guesswork.
3. **Quarantine** oversized files, empty files, unknown access levels, prompt injection, and anything below the quality gate. The file is moved to the quarantine folder and the rejection is versioned.
4. **Mask PII** before the content hash is taken, so the stored hash matches what is stored.
5. **Deduplicate** by content hash: an identical body under another filename is skipped and reported against the document that already owns it.
6. **Version**: a changed body becomes the next pending version while the old version keeps serving.
7. **Activate** only inside the maintenance window (01:00–04:00); outside it the activation is scheduled as a job for the next window.
8. **Retry** failed activations after 15, then 30, then 60 minutes, then mark the job failed with the error.
9. **Health check** every 5 minutes: hash, length and broken-marker checks; failure auto-rolls back to the previous version, and with no previous version the document is retired.

Access level is stored on the document and every version, and reads run through the same `policy.access_levels` mapping as policy RAG. Activation jobs also run from `/admin/sweep`, so a simulated time jump exercises the whole pipeline.

**Quality gate**

Deterministic score out of 1.0: title 0.15, at least 200 characters 0.2, structure 0.15, product/region/effective metadata 0.2, no TODO/TBD placeholders 0.15, readable sentence length 0.15. The threshold is `knowledge.quality_gate_min_score` and can be changed at runtime.

**Verified**

- `python -m pytest tests -q` → **87 passed in 67s**.
- Live run (temp DB, temp folder): 3 files scanned → 1 new, 2 quarantined (injection and quality), quarantined files moved out of the incoming folder; sweep activated v1.
- Editing the file produced v2; v1 stayed active until activation, then became `superseded`.
- A third version containing `[[broken]]` activated, then the 6-minute health check rolled it back to v2 automatically.
- Manual rollback moved v2 → v1; version history showed `superseded / active / rolled_back`.
- Access: a public document visible to `customer`; an internal document visible only to `admin`; direct read as customer returned 404.
- PII in a document body stored as `[EMAIL]` / `[PHONE]`, with `pii_masked = 1`.
- Failed activation retried at 02:15, 02:45, 03:45 and then failed with `unknown document`.

**Decisions**

- Quarantine is the default for injection; sanitising is available by configuration. A knowledge base should not silently keep a partially cleaned hostile document.
- The content hash is computed after masking so the checksum, storage and health check all agree.
- A bad update never interrupts service: the previous active version keeps answering until a good replacement is activated.
- Health rollback considers only versions older than the failing one, so it can never "roll forward" to the broken document.

**Next:** Step 7 — Task 5: invoice/screenshot/PDF uploads, OCR, cross-check against the message, background queue and retention.


## 2026-11-26 - Step 7: upload OCR and cross-check (Task 5)

**Added**

| File | Purpose |
|---|---|
| `app/core/uploads.py` | Sniffing, OCR, injection strip, PII mask, field extraction, cross-check, queue, retention |
| `uploads` / `jobs` | Store every file with sha256, masked message, status, timings and a delete time |
| `POST /uploads` | Multipart receive: process now, or queue when the work looks slow |
| `GET /uploads`, `GET /uploads/{id}`, `POST /uploads/{id}/process` | Inspect an upload, retry it synchronously |
| `POST /uploads/sweep` | Process queued jobs and delete expired files; also runs inside `POST /admin/sweep` |
| `tests/test_step7_uploads.py` | 10 tests: OCR, mismatch, no invention, masking, unsafe files, size, queue, retry, retention, PDF |
| `scripts/smoke_step7.py` | Live run against a real server with a temporary upload folder |

**Pipeline**

1. **Sniff the bytes**, not the extension. Executables and archives (`MZ`, `PK`, ELF, shebang), unknown content and declared-type mismatches are rejected before anything is written.
2. **Reject unsafe PDFs**: `/JavaScript`, `/OpenAction`, `/Launch` and `/EmbeddedFile` markers are refused, so a PDF cannot run code or drop a file.
3. **Enforce limits**: allowed extensions, non-empty files, `uploads.max_mb`.
4. **OCR**: images through Tesseract (`uploads.tesseract_cmd`, `uploads.ocr_languages`), PDFs through the text layer first and Tesseract on pages with no usable text.
5. **Treat the document as evidence, not instructions**: extracted text runs through the injection sanitiser, then PII masking, before storage.
6. **Extract only what is there**: invoice id, order id, amount, date. When several amounts exist and there is no total line, `amount` stays `null` and the candidates are listed.
7. **Cross-check against the message**: matches, mismatches, only-in-document and only-in-message. A mismatch reports both values; the document value is never overwritten and the message value is never averaged in.
8. **Queue slow work**: work estimated above `uploads.processing_timeout_seconds` (30s) becomes a `upload.process` job, retried after 5, 15 and 30 minutes, then marked failed.
9. **Retention**: after `uploads.retention_hours` (24) the file is deleted from disk, the extracted text is cleared and the row stays as a tombstone with `status=deleted` and `reason=retention_expired`.

**Verified**

- `python -m pytest tests -q` -> **97 passed in 72s** (87 existing + 10 new).
- Live run: a generated invoice image returned `INV-99887`, `1200.0` and `2026-11-21`, with invoice id and amount matched against the message.
- Message claiming Rs 5000 against a Rs 1200 invoice produced one mismatch carrying both values; the document value stayed `1200.0`.
- A document with an email and a phone stored `[EMAIL]` and `[PHONE]` and reported `pii = [EMAIL, PHONE]`.
- Executable, `.exe` and hostile PDF uploads were rejected with `unsafe_content`, `unsupported_type` and `unsafe_pdf`.
- With `uploads.force_queue=true` the upload returned `queued`, the sweep processed the job, and the stored row became `processed` with the right amount.
- After a 2-hour clock jump the file was unlinked, the row became `deleted` and `extracted` was `None`.
- A missing file retried at 5, 15 and 30 minutes and then failed with the last error kept on the job.

**Decisions**

- OCR output is masked before it is stored, so the database never holds the raw email or phone even for an admin.
- A single stray amount is still an amount; ambiguity is only declared when there is more than one candidate and no total line. Guessing was the failure mode to avoid.
- The invoice id is recognised as the reference it is: `INV-40001` in a message is not also reported as a missing order id.
- Uploads are rejected before storage when unsafe, but the rejection is still recorded as an audit row so a repeated attacker is visible.
- PDF text layer first: it is exact and cheap; OCR is the fallback for scans, not the default.


## 2026-11-26 - Step 8: multilingual sessions (Task 6)

**Added**

| File | Purpose |
|---|---|
| `app/core/sessions.py` | Language detection per turn, session lifecycle, context, references, carry-over |
| `sessions` columns | `restore_until`, `expired_at`, `restored_at`, `closed_at` |
| `config.yaml` | `sessions.languages`, `sessions.bind_customer` |
| `GET /sessions`, `GET /sessions/settings`, `GET /sessions/{id}/context` | Inspect sessions and the configured windows |
| `POST /sessions/{id}/close`, `POST /sessions/sweep` | Close a conversation, expire/archive idle ones |
| `/admin/sweep` | Now also expires and archives sessions |
| `tests/test_step8_sessions.py` | 10 tests covering switches, mixed messages, 10-message context, isolation, expiry, restore, config, sweep, close |
| `scripts/smoke_step8.py` | Live run with ASCII-only output |

**Language handling**

1. **Script first**: Devanagari is Hindi, Tamil script is Tamil.
2. **Transliteration**: loanword lists separate Hinglish from Tanglish; both present is `mixed`.
3. **Mixed messages**: Latin words beside an Indian script are recorded as two languages (`ta` + `en`, dominant script). The reply uses the dominant language, so a Tamil sentence with English nouns is answered in Tamil.
4. **Rules beat the model on language.** Live testing showed the LLM calling Tanglish "Hinglish"; the lexical/script detection now wins whenever it has a signal, and the model only refines `en` turns.
5. **Every turn keeps its language**, so switching mid-conversation is visible in `/sessions/{id}/context` and in the audit log (`session.language_switched`).

**Session lifecycle**

- `active` -> after 30 idle minutes -> `expired`; the row keeps its history.
- Returning within 24 hours restores the same session id, history and language (`restored: true`, `resumed_within_restore_window`).
- Returning after 24 hours archives the old session and starts a new one (`restore_window_passed`) with no carried references.
- `close` and `archived` sessions are never resumed; a new session starts (`previous_closed`).
- Both windows and the language list are runtime overrides; `/sessions/settings` reports them.
- Context is never less than ten messages (`sessions.context_messages` has a floor of 10).

**Reference preservation**

Ids (`ORD-`, `INV-`, `TKT-`, `ORDER-`, `INVOICE-`, `TICKET-`), error codes (`E-402`, `ERR-...`), amounts (`Rs 4,500`) and dates (`12/11/2026`) are read from recent customer turns. When a new turn omits them, they are merged into the turn's entities and reported in `carried_entities`, so the ticket and handoff keep the reference after a language switch. Nothing is rewritten or translated.

**Verified**

- `python -m pytest -q` -> **107 passed in 44s** (97 existing + 10 new).
- Live: English turn `ORD-55551`, then a Tamil turn without the id -> reply in Tamil, `switched_from=en`, order id carried into the entities.
- Live: Tanglish + English returned `tanglish` with `languages=[tanglish, en]` and `mixed=true`; Tamil script + English returned `ta`; Hinglish and Hindi both detected.
- Live: invoice, date, amount and error code from message 1 were still carried after five filler turns and a switch to Tamil.
- Live: a second customer presenting the same session id got `403 session belongs to another customer`.
- Live: 31-minute gap -> same session id restored with the order id; 26-hour gap -> new session, old one `archived`.
- Live: close -> returning creates a new session with lifecycle `previous_closed`.
- Sweep expired idle sessions and archived those past the restore deadline.

**Decisions**

- Expiry is checked on both read and sweep, so a customer returning after an hour is restored with correct history even if nobody ran the sweep.
- The 24-hour restore window starts when the session expires, not when it was last used.
- Customer binding is enforced only when the request names a customer: a session with an owner cannot be taken over, but anonymous continuation still works.
- Assistant replies now store their language too, so the context endpoint and UI show a real conversation history.

## 2026-11-26 - Step 9: Streamlit demo console

**Added**

- `ui/app.py`: Chat, Tickets, Policy, Knowledge, Uploads and Ops tabs using the
  existing FastAPI service.
- Policy controls expose historical date, product, region and requester filters,
  with the answer and citation metadata shown together.
- Ticket controls expose routing, resolution and the complete masked handoff.
- Upload controls submit multipart evidence and show extracted fields,
  mismatches, masking and queue/retention results.
- `ui/README.md`: PowerShell startup commands and a six-part demo walkthrough.
- `tests/test_step9_ui.py`: Streamlit AppTest exercises the real FastAPI handlers
  through TestClient, with deterministic triage and no model calls.

**Fixed during verification**

- Session-id display now follows chat creation and session closure across reruns.
- Missing KB version history displays an API error instead of crashing the UI.
- Failed configuration updates no longer display success.
- Ticket team filters are URL-encoded and labelled as exact matches.
- KB metric values use the actual nested statistics response.

**Verified**

- `python -m pytest -q`: **110 passed in 37.13s**.
- UI checks cover chat creation, session display/closure, missing-document errors,
  runtime configuration and the combined maintenance sweep.
- Verification used Streamlit's simulated UI runtime, not a browser recording.

**Next:** prepare the final project README and demo-video script, then record
and check the submission links.
