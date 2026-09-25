# Tracing screenshots

Capture these after a live run with `PROVIDER=openai` (dashboard upload needs a
real OpenAI key — a gateway key cannot upload traces).

1. Run the demos:
   ```bash
   python -m src.main --demo
   python -m src.main --guardrail-demo
   ```
2. Open <https://platform.openai.com/traces> and find workflow
   **`support-triage`**.
3. Capture three screenshots into this directory:

| File | Show |
|---|---|
| `01-trace-list.png` | the trace list, grouped by `group_id` (one row per conversation) |
| `02-handoff-trace.png` | a demo turn expanded: Router → specialist handoff, the tool spans beneath it |
| `03-guardrail-trace.png` | a guardrail-demo turn where the input tripwire fired |

`trace(workflow_name="support-triage", group_id=conversation_id)` in
`src/main.py` is what groups a multi-turn conversation into one inspectable
unit, so screenshot 1 should show conversations rather than loose runs.

The trace ids in `docs/sample-interactions.md` are local ids, not the
dashboard's — the two are correlated by conversation id and timestamp.
