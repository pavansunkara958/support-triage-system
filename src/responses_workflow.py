"""Responses API workflow — post-conversation QA review.

Calls `client.responses.create` **directly**, outside the Agents SDK.
Deliberately a different shape from the agent loop:

| | Agent loop | This workflow |
|---|---|---|
| Framework | Agents SDK | direct API call |
| State | `SQLiteSession`, client-side | `previous_response_id`, **server-side** |
| Tools | yes | none |
| Handoffs | yes | none |
| Output | free text | `json_schema`, strict |

Reviewing a completed transcript is single-shot analysis with no tools and no
handoffs. Routing it through the agent framework would add machinery for
nothing. **Use the framework where its features earn their cost** — and showing
where they do not is the point of including this.

`previous_response_id` is the interesting part: each call after the first sends
only the new transcript, and the API retains the prior context server-side. The
reviewer notices patterns across conversations without re-uploading everything
each time. That is the clearest contrast with the client-side session the agent
loop uses.
"""
from __future__ import annotations

import json
from typing import Any

from .config import MODEL_SPECIALIST

SYSTEM = """You are a QA reviewer for a customer-support team.

Review the transcript against these standards:
- Was the customer routed to the right specialist?
- Were claims grounded in tool output rather than asserted?
- Was policy respected — refund limits, escalation, no PII echoed, no invented
  fix dates?
- Was the customer left with a clear next step?

You are reviewing conversations from the SAME support system in sequence. If you
notice a pattern repeating across reviews, say so in `summary`.

Be exact. A correct refusal is good work, not a failure."""

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resolved": {"type": "boolean"},
        "routing_correct": {"type": "boolean"},
        "quality_score": {"type": "integer", "minimum": 0, "maximum": 10},
        "sentiment": {"type": "string",
                      "enum": ["positive", "neutral", "frustrated", "angry"]},
        "escalation_needed": {"type": "boolean"},
        "issues_found": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["resolved", "routing_correct", "quality_score", "sentiment",
                 "escalation_needed", "issues_found", "summary"],
    "additionalProperties": False,
}


async def review_batch(transcripts: list[str],
                       model: str | None = None) -> list[dict]:
    """Review transcripts in sequence, threading state server-side."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    model = model or MODEL_SPECIALIST

    reviews: list[dict] = []
    previous_id: str | None = None

    for index, transcript in enumerate(transcripts, start=1):
        kwargs: dict[str, Any] = {
            "model": model,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "conversation_review",
                    "strict": True,
                    "schema": REVIEW_SCHEMA,
                }
            },
        }

        if previous_id is None:
            # First call carries the instructions and the first transcript.
            kwargs["instructions"] = SYSTEM
            kwargs["input"] = f"Conversation 1:\n{transcript}"
        else:
            # Subsequent calls send ONLY the new transcript. The instructions
            # and every prior review are retained server-side.
            kwargs["previous_response_id"] = previous_id
            kwargs["input"] = f"Conversation {index}:\n{transcript}"

        response = await client.responses.create(**kwargs)
        previous_id = response.id

        text = getattr(response, "output_text", "") or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"error": "reviewer returned non-JSON", "raw": text[:300]}

        payload["conversation_index"] = index
        payload["response_id"] = response.id
        reviews.append(payload)

    return reviews
