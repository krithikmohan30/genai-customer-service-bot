# Demo console

Run both commands from the project root in separate PowerShell terminals.

API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8139
```

UI:

```powershell
.\.venv\Scripts\python.exe -m streamlit run ui/app.py
```

Open http://localhost:8501. For another API address, set `$env:SUPPORT_API`
before starting Streamlit.

## Demo flow

1. **Chat:** send a complaint, then switch to Tamil or Hindi in the same session.
   Open the trace to inspect sentiment, escalation, ticket and retained references.
2. **Tickets:** inspect priority, SLA and routing; generate the masked handoff.
3. **Policy:** ask a policy question with optional date/product/region filters.
   Inspect the answer and citation metadata.
4. **Knowledge:** scan the configured incoming folder, run jobs/health checks,
   and inspect versions or request rollback.
5. **Uploads:** select an invoice or screenshot, enter a conflicting amount in
   the accompanying message, and inspect OCR fields and the cross-check.
6. **Ops:** advance the injected clock and run sweeps to demonstrate timers.

For a deterministic demo without model calls, use Ops to set `triage.mode` to
`rules` and `policy.mode` to `extractive` before chatting. These are runtime
overrides. Restart the API after editing `config.yaml`; restart Streamlit after
changing its environment variables.

The console is a local internship demo. Requester selectors demonstrate policy
filtering; they are not a login system.
