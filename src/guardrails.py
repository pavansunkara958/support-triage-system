"""Input and output guardrails.

Three layers total, each catching what the others cannot:

  INPUT   before the agent. Cheap, deterministic, blocks the request.
  OUTPUT  on the final response. Catches what the agent produced.
  TOOL    inside `tools.issue_refund`. The only one a prompt cannot argue with,
          because it is code on the execution path.

Deterministic patterns run before the model check. Pattern matching is free and
never hallucinates; the relevance model runs only when the patterns are
inconclusive. A guardrail that costs as much as the request it guards will be
switched off within a quarter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from agents import (Agent, GuardrailFunctionOutput, RunContextWrapper, Runner,
                    input_guardrail, output_guardrail)
from pydantic import BaseModel, Field

from .config import guardrail_model

# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
     "instruction-override"),
    (r"(you\s+are\s+now|from\s+now\s+on\s+you)\b", "role-reassignment"),
    (r"\b(system\s*prompt|initial\s+instructions|your\s+instructions)\b",
     "system-prompt-probe"),
    (r"<\|(im_start|im_end|system)\|>", "chat-template-injection"),
    (r"\b(refund|credit)\s+(me\s+)?(everything|all|the\s+full)", "unbounded-refund"),
    (r"\bapprove\s+(any|all|every)\b", "blanket-approval"),
]

CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
CVV_RE = re.compile(r"\bcvv[:\s]*\d{3,4}\b", re.I)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

MAX_INPUT_CHARS = 4000


def _luhn(number: str) -> bool:
    """Distinguishes a real card number from an order id of similar length.

    Without this, a naive 13–19 digit regex blocks order numbers and tracking
    ids, and the guardrail's false-positive rate makes it the first thing
    someone disables.
    """
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


@dataclass
class Finding:
    category: str
    detail: str


def scan_text(text: str) -> list[Finding]:
    """Deterministic scan. Returns every finding, not just the first."""
    findings: list[Finding] = []
    lowered = text.lower()

    for pattern, category in INJECTION_PATTERNS:
        if m := re.search(pattern, lowered):
            findings.append(Finding(category, m.group(0)[:60]))

    for m in CARD_RE.finditer(text):
        if _luhn(m.group(0)):
            findings.append(Finding("card-number", "PAN detected"))
            break
    if CVV_RE.search(text):
        findings.append(Finding("cvv", "CVV detected"))
    if SSN_RE.search(text):
        findings.append(Finding("ssn", "SSN detected"))
    if len(text) > MAX_INPUT_CHARS:
        findings.append(Finding("oversized", f"{len(text)} chars"))

    return findings


# ---------------------------------------------------------------------------
# INPUT GUARDRAIL
# ---------------------------------------------------------------------------
class RelevanceCheck(BaseModel):
    on_topic: bool = Field(description="Is this a customer-support request?")
    reasoning: str = Field(description="One sentence")


_relevance_agent = Agent(
    name="relevance-check",
    instructions=(
        "Decide whether a message is a customer-support request for a SaaS "
        "product: billing, technical problems, account management, or general "
        "product help. Follow-ups, clarifications and social pleasantries "
        "within a support conversation are on-topic. Requests to write code, "
        "essays or poetry, or to discuss unrelated subjects, are off-topic."
    ),
    output_type=RelevanceCheck,
    model=guardrail_model(),
)


def latest_user_text(user_input) -> str:
    """Extract only the NEWEST user message.

    With a session, the SDK passes the full accumulated history to the
    guardrail. Scanning all of it means a message blocked on turn 1 stays in the
    session and re-trips the guardrail on every later turn — the conversation
    becomes permanently stuck, and the reported finding points at the wrong
    message, so the symptom actively misleads diagnosis.

    A guardrail that permanently bricks a conversation is worse than one that
    misses. Its job is to judge what the user *just said*.
    """
    if isinstance(user_input, str):
        return user_input

    if isinstance(user_input, list):
        for item in reversed(user_input):
            if isinstance(item, dict):
                if item.get("role") != "user":
                    continue
                content = item.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("text")]
                    if parts:
                        return " ".join(parts)
            else:
                if getattr(item, "role", None) == "user":
                    content = getattr(item, "content", "")
                    if isinstance(content, str):
                        return content
        return ""

    return str(user_input)


@input_guardrail(name="support_input_guardrail")
async def support_input_guardrail(
    ctx: RunContextWrapper[None], agent: Agent, user_input
) -> GuardrailFunctionOutput:
    """Block prompt injection, card data and off-topic requests."""
    text = latest_user_text(user_input)

    findings = scan_text(text)
    if findings:
        return GuardrailFunctionOutput(
            output_info={
                "layer": "deterministic",
                "findings": [{"category": f.category, "detail": f.detail}
                             for f in findings],
                "message": "; ".join(f.category for f in findings),
            },
            tripwire_triggered=True,
        )

    result = await Runner.run(_relevance_agent, text, context=ctx.context)
    check = result.final_output_as(RelevanceCheck)
    return GuardrailFunctionOutput(
        output_info={"layer": "model", "reasoning": check.reasoning,
                     "message": "off-topic request"},
        tripwire_triggered=not check.on_topic,
    )


# ---------------------------------------------------------------------------
# OUTPUT GUARDRAIL
# ---------------------------------------------------------------------------
COMMITMENT_PATTERNS = [
    (r"\b(guarantee|guaranteed|i promise|we promise)\b", "unqualified-promise"),
    (r"\b(unlimited|lifetime)\s+(refund|credit|discount)", "unbounded-commitment"),
    (r"\bwill\s+definitely\s+be\s+(fixed|resolved|refunded)\s+by\b",
     "unqualified-deadline"),
    (r"\b(fixed|resolved)\s+by\s+(monday|tuesday|wednesday|thursday|friday|"
     r"next week|tomorrow)\b", "invented-fix-date"),
    (r"\b(full|complete)\s+refund\s+of\s+\$?\d{3,}", "large-refund-promise"),
]


@output_guardrail(name="support_output_guardrail")
async def support_output_guardrail(
    ctx: RunContextWrapper[None], agent: Agent, agent_output
) -> GuardrailFunctionOutput:
    """Block responses leaking PII or making commitments we cannot honour.

    Support answers become contractual in customers' minds. A promise the agent
    is not authorised to make is a liability whether or not it was a
    hallucination, and "it will be fixed by Friday" is the one a model will
    produce under pressure unless code stops it.
    """
    text = agent_output if isinstance(agent_output, str) else str(agent_output)
    findings: list[dict[str, str]] = []

    for m in CARD_RE.finditer(text):
        if _luhn(m.group(0)):
            findings.append({"category": "card-number-in-output",
                             "detail": "PAN would be echoed to the customer"})
            break
    if SSN_RE.search(text):
        findings.append({"category": "ssn-in-output", "detail": "SSN in response"})

    lowered = text.lower()
    for pattern, category in COMMITMENT_PATTERNS:
        if m := re.search(pattern, lowered):
            findings.append({"category": category, "detail": m.group(0)[:60]})

    return GuardrailFunctionOutput(
        output_info={"findings": findings,
                     "message": "; ".join(f["category"] for f in findings)},
        tripwire_triggered=bool(findings),
    )
