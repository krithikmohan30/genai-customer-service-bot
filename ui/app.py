"""Streamlit console for the ElevanceSkills support-bot project.

One screen per task, all talking to the same FastAPI service:

* Chat      - sentiment, escalation, policy answer, ticket, language (T1/T2/T4/T6)
* Tickets   - priority, routing, SLA, handoff (T2)
* Knowledge - scan, gate, versions, rollback, health (T3)
* Uploads   - OCR, cross-check, masking, queue, retention (T5)
* Ops       - injected clock, sweep, config overrides, audit

Run the API first:

    uvicorn app.main:app --port 8139

then:

    streamlit run ui/app.py
"""

from __future__ import annotations

import json
import os

import requests
import streamlit as st

API = os.environ.get("SUPPORT_API", "http://127.0.0.1:8139")
TIMEOUT = 120

st.set_page_config(page_title="Elevance Support Bot", layout="wide")


# --------------------------------------------------------------------------- api


def api(method: str, path: str, **kwargs):
    """Call the service; return parsed JSON or an {'error': ...} dict."""
    try:
        response = requests.request(method, API + path, timeout=TIMEOUT, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            return {"error": detail, "status": response.status_code}
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"error": str(exc), "status": None}


def show_error(payload) -> bool:
    if isinstance(payload, dict) and payload.get("error") is not None:
        st.error(f"{payload.get('status')}: {payload['error']}")
        return True
    return False


def pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- state


def init_state() -> None:
    st.session_state.setdefault("session_id", None)
    st.session_state.setdefault("customer_id", "demo-customer")
    st.session_state.setdefault("log", [])


init_state()

health = api("GET", "/health")

with st.sidebar:
    st.title("Support bot")
    if health and "error" not in health:
        st.success(f"API ok · {health.get('triage_mode')} triage")
        st.caption(f"clock {health.get('time')}")
    else:
        st.error("API not reachable")
        st.caption(f"expected at {API}")
        st.stop()

    st.session_state["customer_id"] = st.text_input(
        "Customer id", st.session_state["customer_id"]
    )
    st.session_state["session_display"] = st.session_state["session_id"] or ""
    st.text_input(
        "Session id",
        key="session_display",
        disabled=True,
    )

    col_a, col_b = st.columns(2)
    if col_a.button("New session", use_container_width=True):
        st.session_state["session_id"] = None
        st.session_state["log"] = []
        st.rerun()
    if col_b.button("Close session", use_container_width=True):
        if st.session_state["session_id"]:
            result = api(
                "POST", f"/sessions/{st.session_state['session_id']}/close", json={}
            )
            if not show_error(result):
                st.session_state["session_id"] = None
                st.session_state["log"] = []
                st.rerun()

    st.divider()
    st.caption("Tasks")
    st.caption("T1 sentiment · T2 tickets/SLA · T3 KB")
    st.caption("T4 policy RAG · T5 uploads · T6 languages")

    st.divider()
    st.caption("Simulated clock")
    if st.button("Night shift 22:30", use_container_width=True):
        api("POST", "/admin/time", json={"iso": "2026-11-23T22:30:00"})
        st.rerun()
    if st.button("Next morning 09:00", use_container_width=True):
        api("POST", "/admin/time", json={"iso": "2026-11-24T09:00:00"})
        st.rerun()
    if st.button("+30 minutes", use_container_width=True):
        api("POST", "/admin/time", json={"advance_minutes": 30})
        st.rerun()
    if st.button("Reset clock", use_container_width=True):
        api("POST", "/admin/time", json={"reset": True})
        st.rerun()


chat_tab, ticket_tab, policy_tab, kb_tab, upload_tab, ops_tab = st.tabs(
    ["Chat", "Tickets", "Policy", "Knowledge", "Uploads", "Ops"]
)


# --------------------------------------------------------------------------- chat

with chat_tab:
    st.subheader("Conversation")
    st.caption(
        "Language is detected per turn: English, Hindi, Tamil, Hinglish, Tanglish. "
        "A new session expires after 30 idle minutes and can be restored for 24 hours."
    )

    for entry in st.session_state["log"]:
        with st.chat_message(entry["role"]):
            st.markdown(entry["content"])
            meta = entry.get("meta")
            if meta:
                with st.expander("trace", expanded=False):
                    st.json(meta)

    prompt = st.chat_input("Ask about an order, invoice, refund or policy")
    if prompt:
        if not st.session_state["session_id"]:
            # let the service mint one; identity is sent with every turn
            payload = api(
                "POST",
                "/chat",
                json={
                    "message": prompt,
                    "customer_id": st.session_state["customer_id"],
                },
            )
        else:
            payload = api(
                "POST",
                "/chat",
                json={
                    "message": prompt,
                    "session_id": st.session_state["session_id"],
                    "customer_id": st.session_state["customer_id"],
                },
            )

        st.session_state["log"].append({"role": "user", "content": prompt})
        if payload and "error" not in payload:
            st.session_state["session_id"] = payload["session_id"]
            st.session_state["log"].append(
                {"role": "assistant", "content": payload["reply"], "meta": payload}
            )
        else:
            st.session_state["log"].append(
                {
                    "role": "assistant",
                    "content": "The service returned an error; check the API window.",
                }
            )
            show_error(payload)
        st.rerun()

    if st.session_state["log"]:
        last = st.session_state["log"][-1]
        meta = last.get("meta") or {}
        if meta:
            st.divider()
            columns = st.columns(5)
            sentiment = meta.get("sentiment") or {}
            risk = meta.get("risk") or {}
            columns[0].metric("Language", meta.get("language", "?"))
            columns[1].metric("Sentiment", sentiment.get("label", "?"))
            columns[2].metric("Risk", risk.get("level", "?"))
            columns[3].metric("Intent", meta.get("intent", "?"))
            columns[4].metric("Escalated", "yes" if meta.get("escalated") else "no")
            if meta.get("ticket"):
                ticket = meta["ticket"]
                st.info(
                    f"Ticket {ticket['id']} · {ticket.get('severity')} · "
                    f"priority {ticket.get('priority')} · team {ticket.get('team')}"
                )
            if meta.get("escalation"):
                st.warning(pretty(meta["escalation"]))

        with st.expander("Session context", expanded=False):
            if st.session_state["session_id"]:
                context = api(
                    "GET", f"/sessions/{st.session_state['session_id']}/context"
                )
                show_error(context)
                if context and "messages" in context:
                    st.write(f"References: {context.get('references')}")
                    st.json(context.get("messages"))

    with st.expander("Demo prompts", expanded=False):
        st.markdown(
            "- `My refund for ORD-10231 has not arrived and I am angry`\n"
            "- `Someone changed my password, I did not do it` — high risk, calm tone\n"
            "- `What is the refund policy?` — policy answer with citations\n"
            "- `என் order ORD-10231 refund ஆகல` — mixed Tamil and English\n"
            "- `mera paisa wapas karo, bahut pareshan hoon` — Hinglish\n"
            "- Same session, 31 minutes later — context restored"
        )


# --------------------------------------------------------------------------- tickets

with ticket_tab:
    st.subheader("Tickets, priority, routing and SLA")
    filters = st.columns(3)
    status = filters[0].selectbox(
        "Status", ["all", "open", "routed", "escalated", "resolved"]
    )
    team = filters[1].text_input("Team (exact name)", "")
    limit = filters[2].number_input("Limit", 10, 500, 100, step=10)

    query = [f"limit={int(limit)}"]
    if status != "all":
        query.append(f"status={status}")
    if team:
        query.append(f"team={requests.utils.quote(team, safe='')}")
    listing = api("GET", "/tickets?" + "&".join(query))
    if listing and "items" in listing:
        rows = [
            {
                "id": item["id"],
                "status": item["status"],
                "category": item.get("category"),
                "severity": item.get("severity"),
                "priority": round(float(item.get("priority") or 0), 1),
                "team": item.get("team"),
                "sla_due": item.get("sla_due_at"),
                "subject": (item.get("subject") or "")[:60],
            }
            for item in listing["items"]
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.caption(f"{listing['count']} tickets")

        ticket_id = st.selectbox(
            "Ticket", [row["id"] for row in rows] if rows else ["(none)"]
        )
        if rows and ticket_id != "(none)":
            detail = api("GET", f"/tickets/{ticket_id}")
            left, right = st.columns([2, 1])
            with left:
                st.json(detail)
            with right:
                if st.button("Re-route now", use_container_width=True):
                    verdict = api("POST", f"/tickets/{ticket_id}/route")
                    show_error(verdict)
                    if verdict and "routing" in verdict:
                        st.info(pretty(verdict["routing"]))
                if st.button("Close ticket", use_container_width=True):
                    result = api("POST", f"/tickets/{ticket_id}/close")
                    if not show_error(result):
                        st.rerun()
                if st.button("Handoff summary", use_container_width=True):
                    handoff = api("GET", f"/tickets/{ticket_id}/handoff")
                    show_error(handoff)
                    if handoff:
                        st.text_area(
                            "Masked handoff",
                            pretty(handoff),
                            height=320,
                        )
    else:
        show_error(listing)


# --------------------------------------------------------------------------- policy

with policy_tab:
    st.subheader("Policy answers and citations")
    with st.form("policy_query"):
        question = st.text_input("Policy question", "What is the refund policy?")
        policy_role = st.selectbox("Policy requester", ["customer", "agent", "admin"])
        as_of = st.text_input("As-of date (optional YYYY-MM-DD)", "")
        product = st.text_input("Product (optional)", "")
        region = st.text_input("Region", "IN")
        submitted = st.form_submit_button("Ask policy")
    if submitted:
        result = api(
            "POST",
            "/policy/ask",
            json={
                "question": question,
                "requester": policy_role,
                "as_of": as_of or None,
                "product": product or None,
                "region": region or None,
            },
        )
        if not show_error(result):
            st.session_state["policy_result"] = result
        else:
            st.session_state.pop("policy_result", None)
    if st.session_state.get("policy_result"):
        result = st.session_state["policy_result"]
        st.write(f"Status: {result.get('status')}")
        if result.get("answer"):
            st.markdown(result["answer"])
        st.json(result)


# --------------------------------------------------------------------------- knowledge

with kb_tab:
    st.subheader("Knowledge base pipeline")
    stats = api("GET", "/kb/stats")
    if stats and "error" not in stats:
        documents = stats.get("documents") or {}
        by_status = documents.get("by_status") or {}
        jobs = stats.get("jobs") or {}
        columns = st.columns(4)
        columns[0].metric("Documents", documents.get("total", 0))
        columns[1].metric("Active", by_status.get("active", 0))
        columns[2].metric("Quarantined", by_status.get("quarantined", 0))
        columns[3].metric("Pending jobs", jobs.get("pending", 0))
    else:
        show_error(stats)

    actions = st.columns(4)
    requester = actions[0].selectbox("Requester", ["customer", "agent", "admin"])
    if actions[1].button("Scan folder", use_container_width=True):
        result = api("POST", "/kb/scan")
        show_error(result)
        if result:
            st.json(result)
    if actions[2].button("Run jobs + health", use_container_width=True):
        result = api("POST", "/kb/sweep")
        show_error(result)
        if result:
            st.json(result["jobs"]["processed"] if "jobs" in result else result)
    if actions[3].button("Refresh", use_container_width=True):
        st.rerun()

    docs = api("GET", f"/kb/docs?requester={requester}")
    if docs and "items" in docs:
        st.dataframe(
            [
                {
                    "id": item["id"],
                    "version": item.get("version"),
                    "title": item.get("title"),
                    "access": item.get("access_level"),
                    "effective": item.get("effective_at"),
                    "status": item.get("status"),
                }
                for item in docs["items"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"{docs['count']} documents visible to {requester}")

    st.divider()
    search_col, doc_col = st.columns(2)
    with search_col:
        st.markdown("**Search**")
        term = st.text_input("Query", "refund timeline")
        if st.button("Search knowledge"):
            results = api(
                "GET",
                f"/kb/search?q={requests.utils.quote(term)}&requester={requester}",
            )
            show_error(results)
            if results and "items" in results:
                st.json(results["items"])
    with doc_col:
        st.markdown("**Version control**")
        document_id = st.text_input("Document id", "refund_policy_v3")
        version = st.number_input("Version (0 = latest older)", 0, 999, 0)
        if st.button("Activate version"):
            body = {"version": int(version)} if version else {}
            result = api("POST", f"/kb/docs/{document_id}/activate", json=body)
            show_error(result)
            if result:
                st.json(result)
        if st.button("Roll back"):
            body = {"version": int(version), "reason": "ui"} if version else {}
            result = api("POST", f"/kb/docs/{document_id}/rollback", json=body)
            show_error(result)
            if result:
                st.json(result)
        if st.button("Show versions"):
            versions = api("GET", f"/kb/docs/{document_id}/versions?requester=admin")
            show_error(versions)
            if versions and "items" in versions:
                st.json(versions["items"])


# --------------------------------------------------------------------------- uploads

with upload_tab:
    st.subheader("Invoice, screenshot and PDF checks")
    st.caption(
        "The document is evidence: values come from OCR, PII is masked, and the "
        "message is cross-checked rather than overwritten. Files are deleted after 24 hours."
    )

    message = st.text_input(
        "Message that came with the file",
        "I was charged Rs 1200 on invoice INV-99887",
    )
    uploaded = st.file_uploader(
        "PNG, JPG, WEBP or PDF", type=["png", "jpg", "jpeg", "webp", "pdf"]
    )
    if uploaded is not None and st.button("Upload and inspect"):
        files = {
            "file": (
                uploaded.name,
                uploaded.getvalue(),
                uploaded.type or "application/octet-stream",
            )
        }
        data = {"message": message}
        if st.session_state["session_id"]:
            data["session_id"] = st.session_state["session_id"]
        result = api("POST", "/uploads", files=files, data=data)
        if show_error(result):
            pass
        else:
            st.success(
                f"{result['upload_id']} · {result['status']} · queued={result['queued']} "
                f"· delete_at={result.get('delete_at')}"
            )
            extracted = result.get("extracted") or {}
            if extracted:
                columns = st.columns(4)
                columns[0].metric("Pages", extracted.get("pages"))
                columns[1].metric("Engine", extracted.get("engine"))
                columns[2].metric(
                    "PII found", ", ".join(extracted.get("pii") or []) or "none"
                )
                columns[3].metric(
                    "Conflict", "yes" if extracted["cross_check"]["conflict"] else "no"
                )
                st.json(extracted.get("fields"))
                st.markdown("**Cross-check**")
                st.json(extracted.get("cross_check"))
                st.text_area(
                    "OCR text (masked)", extracted.get("text") or "", height=200
                )

    if st.button("Process queue and retention sweep"):
        result = api("POST", "/uploads/sweep")
        show_error(result)
        if result:
            st.json(result)

    uploads = api("GET", "/uploads")
    if uploads and "items" in uploads:
        st.dataframe(
            [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "file": item.get("filename"),
                    "bytes": item.get("size_bytes"),
                    "ms": item.get("processing_ms"),
                    "delete_at": item.get("delete_at"),
                    "reason": item.get("reason"),
                }
                for item in uploads["items"]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        show_error(uploads)


# --------------------------------------------------------------------------- ops

with ops_tab:
    st.subheader("Operations")
    left, right = st.columns(2)

    with left:
        st.markdown("**Clock and timers**")
        current = api("GET", "/admin/time")
        if current and "error" not in current:
            st.write(current)
        iso = st.text_input("Set clock (ISO)", "2026-11-23T22:30:00")
        if st.button("Set clock"):
            result = api("POST", "/admin/time", json={"iso": iso})
            show_error(result)
            if result:
                st.json(result)
        if st.button("Run every sweep"):
            result = api("POST", "/admin/sweep")
            show_error(result)
            if result:
                st.json(result)

        st.markdown("**Working calendar**")
        if st.button("Show calendar"):
            result = api("GET", "/admin/calendar")
            show_error(result)
            if result:
                st.json(result)

    with right:
        st.markdown("**Runtime configuration**")
        key = st.text_input("Key", "sla.by_severity.high")
        raw = st.text_input("Value (JSON or text)", "120")
        if st.button("Apply override"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            result = api("POST", "/admin/config", json={"key": key, "value": value})
            if not show_error(result):
                st.success("override applied")
        if st.button("Clear overrides"):
            result = api("DELETE", "/admin/config")
            if not show_error(result):
                st.success("overrides cleared")
        overrides = api("GET", "/admin/config")
        if overrides and "overrides" in overrides:
            st.json(overrides["overrides"])

        st.markdown("**Language and session settings**")
        settings = api("GET", "/sessions/settings")
        if settings and "error" not in settings:
            st.json(settings)

    st.divider()
    st.markdown("**Audit tail**")
    audit = api("GET", "/admin/audit?limit=100")
    if audit and "items" in audit:
        st.dataframe(audit["items"], use_container_width=True, hide_index=True)
    else:
        show_error(audit)
