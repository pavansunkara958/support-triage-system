"""Generate docs/sample-interactions.md from the captured traces.

The sample-interactions deliverable is built from `logs/*.jsonl` rather than
pasted by hand. Two reasons, and the second is the real one:

  1. Re-running the demo regenerates the document; it cannot drift from the
     code the way a hand-written transcript does.
  2. **A hand-written transcript is unfalsifiable.** Anyone can type what an
     agent "would" say. A document generated from the trace can only contain
     what actually happened, and the trace ids in it are checkable against the
     dashboard.

Run after a live run:
    python -m src.main --demo
    python -m src.main --guardrail-demo
    python -m scripts.build_docs
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
OUT = ROOT / "docs" / "sample-interactions.md"

TITLES = {
    "demo-conv-001": ("Multi-turn conversation — three specialists, one session",
                      "The subject changes twice. Watch the handoffs follow it, "
                      "and note that turn 2 resolves 'the duplicate' with no "
                      "invoice number because turn 1 is in the session."),
    "guardrail-demo-1": ("Guardrail — prompt injection",
                         "Blocked by the deterministic layer before any model "
                         "call. No tokens spent."),
    "guardrail-demo-2": ("Guardrail — card data",
                         "Luhn-valid PAN detected and blocked on input. The "
                         "number is never echoed back."),
    "guardrail-demo-3": ("Guardrail — off topic",
                         "Caught by the relevance model, not by a pattern. This "
                         "is the case patterns cannot cover."),
    "guardrail-demo-4": ("Guardrail — legitimate request must NOT be blocked",
                         "A guardrail is only credible if you test what it lets "
                         "through."),
    "guardrail-demo-tool": ("Tool guardrail — refund ceiling",
                            "The refund is refused inside the tool, not by "
                            "instruction. No prompt changes a Python `if`."),
}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def render_conversation(conversation_id: str, events: list[dict]) -> str:
    title, note = TITLES.get(
        conversation_id, (f"Conversation `{conversation_id}`", ""))
    lines = [f"## {title}", ""]
    if note:
        lines += [f"*{note}*", ""]

    trace_ids = sorted({e["trace_id"] for e in events})
    lines += [f"`conversation_id={conversation_id}` · "
              f"{len({e['turn'] for e in events})} turn(s) · "
              f"trace ids: {', '.join(f'`{t}`' for t in trace_ids[:4])}", ""]

    by_turn: dict[int, list[dict]] = defaultdict(list)
    for e in events:
        by_turn[e["turn"]].append(e)

    for turn in sorted(by_turn):
        # `turn_complete` is emitted before `final_response` (the summary is
        # built from the result, then the text is attached), which would print
        # the summary above the answer. Reorder for reading, not for accuracy —
        # timestamps are preserved in the raw trace.
        turn_events = sorted(
            by_turn[turn],
            key=lambda e: {"final_response": 1, "turn_complete": 2}.get(
                e["event"], 0))
        lines.append(f"### Turn {turn}")
        lines.append("")
        lines.append("```text")

        for e in turn_events:
            kind = e["event"]
            if kind == "user_message":
                lines.append(f"customer> {e.get('text', '')}")
            elif kind == "handoff":
                lines.append(f"  ↪ handoff   {e.get('from')} -> {e.get('to')}")
            elif kind == "tool_call":
                lines.append(f"  · tool      {e.get('tool')}  "
                             f"[{e.get('agent')}]")
            elif kind == "tool_result":
                out = str(e.get("output", ""))
                lines.append(f"    result    {out[:120]}")
            elif kind == "guardrail_tripwire":
                detail = e.get("detail") or {}
                msg = detail.get("message", "") if isinstance(detail, dict) else ""
                layer = detail.get("layer", "") if isinstance(detail, dict) else ""
                lines.append(f"  ✕ BLOCKED   {e.get('guardrail')} guardrail"
                             f"{f' ({layer})' if layer else ''}: {msg}")
            elif kind == "final_response":
                lines.append("")
                lines.append(f"agent [{e.get('agent')}]>")
                for para in str(e.get("text", "")).splitlines():
                    lines.append(f"  {para}")
            elif kind == "turn_complete":
                lines.append("")
                lines.append(f"  final agent: {e.get('final_agent')}   "
                             f"{e.get('duration_ms')}ms")
                if e.get("handoffs"):
                    lines.append(f"  handoffs:    {' | '.join(e['handoffs'])}")
                if e.get("tool_calls"):
                    lines.append(f"  tools:       {', '.join(e['tool_calls'])}")

        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not LOG_DIR.exists():
        print("No logs/ directory. Run the demos first:")
        print("  python -m src.main --demo")
        print("  python -m src.main --guardrail-demo")
        return 1

    files = sorted(p for p in LOG_DIR.glob("*.jsonl"))
    if not files:
        print("No traces in logs/. Run the demos first.")
        return 1

    conversations: dict[str, list[dict]] = {}
    for path in files:
        events = load(path)
        if events:
            conversations[path.stem] = events

    # demo first, then guardrail cases in order, then anything else
    def order(name: str) -> tuple:
        if name.startswith("demo"):
            return (0, name)
        if name.startswith("guardrail"):
            return (1, name)
        return (2, name)

    total_turns = sum(len({e["turn"] for e in ev})
                      for ev in conversations.values())
    total_tools = sum(1 for ev in conversations.values()
                      for e in ev if e["event"] == "tool_call")
    total_handoffs = sum(1 for ev in conversations.values()
                         for e in ev if e["event"] == "handoff")
    total_blocks = sum(1 for ev in conversations.values()
                       for e in ev if e["event"] == "guardrail_tripwire")

    header = [
        "# Sample Interactions",
        "",
        "**Generated from captured traces** by `python -m scripts.build_docs`,",
        "not written by hand. Every line below came out of `logs/*.jsonl`, so it",
        "records what the system actually did — and the trace ids are checkable",
        "against the OpenAI dashboard screenshots in `docs/screenshots/`.",
        "",
        f"{len(conversations)} conversations · {total_turns} turns · "
        f"{total_handoffs} handoffs · {total_tools} tool calls · "
        f"{total_blocks} guardrail blocks",
        "",
        "---",
        "",
    ]

    body = [render_conversation(name, conversations[name])
            for name in sorted(conversations, key=order)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(header) + "\n---\n\n".join(body))
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {len(conversations)} conversations, {total_turns} turns, "
          f"{total_handoffs} handoffs, {total_tools} tool calls, "
          f"{total_blocks} blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
