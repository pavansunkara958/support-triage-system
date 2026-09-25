# Multi-Agent Customer Support Triage

Built with the **OpenAI Agents SDK**. A router agent classifies customer intent
and **hands off** to one of three specialists. Three guardrail layers, stateful
conversations, a direct Responses API workflow, and full tracing of decisions,
handoffs and tool calls.

```
53/53 offline checks passed — no API key, no network
```

| Required component | Implementation |
|---|---|
| Router, billing, technical, account agents | [`src/agents_def.py`](src/agents_def.py) |
| Handoffs based on customer intent | Router → specialist, **and specialist → Router** (cyclic) |
| Input, output **and tool** guardrails | [`src/guardrails.py`](src/guardrails.py) + `tools.issue_refund` |
| Guardrail demonstration | `--guardrail-demo` — 4 attacks plus a false-positive check |
| Stateful conversation management | `SQLiteSession`, persisted, resumable across restarts |
| A workflow using the Responses API | [`src/responses_workflow.py`](src/responses_workflow.py) — direct `responses.create` |
| Tracing of decisions, handoffs, tools, responses | SDK → dashboard, plus local JSONL per conversation |

Docs: [design](docs/design.md) · [architecture](docs/architecture.md) ·
[sample interactions](docs/sample-interactions.md) ·
[tracing screenshots](docs/screenshots/)

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your key
python -m src.seed_data       # create and seed the support database
```

## Run

```bash
python -m scripts.mock_test          # 53 offline checks, zero API calls
python -m scripts.preflight          # can this endpoint run the lab?
python -m src.main --demo            # 4-turn conversation, 3 handoffs
python -m src.main --guardrail-demo  # all three guardrails, attacked
python -m src.main --review          # Responses API workflow (OpenAI only)
python -m src.main --chat ticket-42  # interactive; rerun to resume
python -m scripts.build_docs         # regenerate docs/sample-interactions.md
```

**Run `mock_test` first.** Guardrail logic, tool behaviour and graph structure
are deterministic and fully verified offline — so a failure *after* that point
is a routing or infrastructure problem, not a logic one. That split is what
makes live failures quick to isolate.

**Then run `preflight`.** It checks, for a fraction of a cent, whether the
endpoint answers, supports **tool calling** (handoffs in the Agents SDK *are*
tool calls, so an endpoint that strips `tools` cannot route at all), accepts
strict `json_schema`, and serves `/v1/responses`. It reads its config from
`src/config.py`, so preflight and the real run share one auth path.

### Providers

| | Provider | `--review` | Dashboard tracing | Cost |
|---|---|---|---|---|
| **A** | OpenAI (`PROVIDER=openai`) | yes | yes | ~$0.004/turn, ~$0.05 total |
| **B** | Any OpenAI-compatible gateway | **no** | **no** | the gateway's |

The agent graph, handoffs, guardrails and sessions are identical either way —
only the model client changes. **Option A is required for the full deliverable**:
gateways implement `/v1/chat/completions` but not `/v1/responses`, and a gateway
key cannot upload traces to the dashboard, so the screenshots need OpenAI.

Free gateways (Groq, Google AI Studio) run six of the seven components. See
`.env.example`.

---

## What the demo shows

One conversation, four turns, the subject changing twice:

```
TURN 1  "charged twice on my March invoice"        Router -> Billing Agent
TURN 2  "can you refund the duplicate?"            (no invoice number given)
TURN 3  "different problem, CSV exports failing"   Billing -> Router -> Technical
TURN 4  "we want to move to enterprise"            Technical -> Router -> Account
```

Turn 2 is the session-state proof: *"the duplicate"* resolves only because turn 1
is in the session. Turns 3 and 4 are the handoff proof — the specialist hands
back and the router re-routes.

## Guardrails

| Layer | Blocks | Bypassable by prompt? |
|---|---|---|
| Input | injection, card numbers, CVV, SSN, off-topic, oversized | No — runs first |
| Output | PII echo, unqualified guarantees, invented fix dates | No — runs last |
| **Tool** | **refunds above $200** | **No — it is a Python `if`** |

The tool guardrail is the one that matters. Input and output guardrails reduce
the chance of a mistake; the tool guardrail makes one impossible:

```
[PASS] refund above the $200 ceiling refused
[PASS] refusal names the escalation path
[PASS] refund above the invoice total refused
[PASS] refusal returns the maximum refundable amount
[PASS] legitimate in-policy refund succeeds       refund_id=REF-5BC35910
[PASS] double refund refused
```

**Luhn validation prevents the obvious false positive:**

```
[PASS] blocks a real card number (Luhn-valid)
[PASS] does NOT block an order id of similar length
[PASS] legitimate message passes cleanly
```

A guardrail is only credible if you test what it lets through, not just what it
blocks — which is why `--guardrail-demo` ends with a legitimate request that
must **not** be blocked.

## Tool scope is the security boundary

| Agent | Can refund? | Can reset passwords? |
|---|---|---|
| Router | No — **it has no tools at all** | No |
| Billing | Yes, ≤ $200 | **No** |
| Technical | **No** | **No** |
| Account | **No** | Yes |

Enforced by which tools each agent is constructed with, not by instruction:

```
[PASS] technical cannot issue refunds
       ['check_service_status', 'lookup_customer', 'search_known_issues']
[PASS] router has no tools of its own — it routes, it does not solve
```

## Tracing

Two sinks. The SDK uploads to the OpenAI dashboard (screenshots in
`docs/screenshots/`), and `src/tracing.py` writes structured JSONL to
`logs/<conversation_id>.jsonl` so the repo carries machine-readable evidence
that does not depend on a dashboard login:

```json
{"event":"user_message","conversation_id":"demo-conv-001","turn":1}
{"event":"handoff","from":"Router Agent","to":"Billing Agent"}
{"event":"tool_call","tool":"lookup_customer","agent":"Billing Agent"}
{"event":"tool_call","tool":"get_invoices","agent":"Billing Agent"}
{"event":"final_response","agent":"Billing Agent","text":"..."}
{"event":"turn_complete","final_agent":"Billing Agent","duration_ms":3142.7}
```

`trace(workflow_name=..., group_id=conversation_id)` groups each turn, so a
multi-turn conversation is inspectable as one unit.

**`docs/sample-interactions.md` is generated from those traces** by
`scripts/build_docs.py`, not written by hand. A hand-written transcript is
unfalsifiable — anyone can type what an agent *would* say. A document generated
from the trace can only contain what actually happened.

## Three defects this build fixes by construction

Found during an earlier live run of the same design:

| Defect | Symptom | Fix |
|---|---|---|
| Guardrail scanned the whole session | A message blocked on turn 1 re-tripped on every later turn; the reported finding named the *wrong* message | `latest_user_text()` — judge only the newest message |
| `ModelSettings(temperature=...)` | 400 from the gpt-5 family, which are reasoning models | removed entirely; determinism from instructions and tool scope |
| Gateway 401 that was really an *upstream* 401 | The client's `Authorization` header was forwarded verbatim; the error named `platform.openai.com`, not the gateway | strip it in an httpx request hook (`config.py`) |

The first is the interesting one. A guardrail that permanently bricks a
conversation is worse than one that misses, and because it reported the wrong
message it actively misled diagnosis.

## Layout

```
src/
  agents_def.py         Router + 3 specialists, cyclic handoff graph
  guardrails.py         Input/output guardrails, Luhn, newest-message scope
  tools.py              Per-agent tools; refund ceiling enforced in the tool
  main.py               CLI: demo, guardrail-demo, review, chat
  responses_workflow.py Direct Responses API, previous_response_id threading
  tracing.py            Local structured traces
  config.py             Provider switch, models, gateway auth handling
  seed_data.py          The support database
scripts/
  mock_test.py          53 offline checks
  preflight.py          endpoint capability check before spending
  build_docs.py         generates sample-interactions.md from traces
docs/                   design, architecture, sample interactions, screenshots
logs/                   per-conversation JSONL traces
```

## Run environment

Run against Groq (`openai/gpt-oss-120b`) via `PROVIDER=gateway`. Preflight
confirmed all four capabilities including `/v1/responses`, so **six of the
seven components are exercised live**: router, handoffs, all three guardrail
layers, stateful sessions, the Responses API workflow, and tracing.

The one gap is **OpenAI dashboard screenshots** — a gateway key cannot upload
traces. The tracing evidence for this run is the structured JSONL in `logs/`,
which `scripts/build_docs.py` renders into `docs/sample-interactions.md`.
