# Sample Interactions

*Not yet generated.* This document is built from captured traces, not written
by hand — run the demos and then the generator:

```bash
python -m src.main --demo
python -m src.main --guardrail-demo
python -m scripts.build_docs
```

`scripts/build_docs.py` reads `logs/*.jsonl` and renders every turn: the
customer message, each handoff, each tool call and its result, any guardrail
trip, and the final response with the agent that produced it.

It works this way because **a hand-written transcript is unfalsifiable** —
anyone can type what an agent *would* say. A document generated from the trace
can only contain what actually happened, and the conversation ids in it line up
with the dashboard screenshots in `docs/screenshots/`.
