"""Agents and the handoff graph.

    Router ──handoff──> Billing
           ──handoff──> Technical
           ──handoff──> Account

Each specialist can hand back to the Router, so a conversation that changes
subject mid-flow is re-routed rather than answered badly by the wrong agent.
The graph has a cycle, deliberately.

**Handoffs are not delegation.** Control TRANSFERS: the receiving agent owns
the conversation from that point, with its own instructions and its own tools.
The router does not wait for a result and synthesise — it steps out of the way.
That is the right primitive here because support conversations change owner.
"""
from __future__ import annotations

from agents import Agent, handoff
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from .config import specialist_model
from .guardrails import support_input_guardrail, support_output_guardrail
from .tools import ACCOUNT_TOOLS, BILLING_TOOLS, TECHNICAL_TOOLS

MODEL = specialist_model()

# NOTE: no ModelSettings(temperature=...) anywhere here. The gpt-5 family are
# reasoning models and reject `temperature` with a 400. Determinism comes from
# narrow instructions and tool scope instead — which is where it should come
# from anyway, since temperature was never a real control on routing.

# ---------------------------------------------------------------------------
billing_agent = Agent(
    name="Billing Agent",
    handoff_description=(
        "Invoices, charges, refunds, payment failures, plan pricing, and any "
        "question about money."
    ),
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You handle billing for a SaaS product.

Establish the account before discussing any charge. Call lookup_customer with
the email, then get_invoices. Never state an amount you have not read from a
tool — the customer's recollection is not a source.

Refunds at or below $200 are self-service. Above that the tool will refuse and
you must say so plainly and escalate to a billing supervisor. Do not attempt a
workaround, do not split a refund across invoices to stay under the ceiling, and
do not promise the supervisor will approve it.

Never ask for, repeat, or confirm card numbers, CVVs or bank details. If a
customer volunteers one, tell them not to send it and that you cannot use it.

If the question turns out to be technical or about account settings, hand back
to the Router rather than guessing.

Be concise and specific. Quote invoice ids and exact amounts.""",
    tools=BILLING_TOOLS,
    model=MODEL,
    output_guardrails=[support_output_guardrail],
)

technical_agent = Agent(
    name="Technical Support Agent",
    handoff_description=(
        "Errors, bugs, outages, integration problems, API issues, performance, "
        "and anything that is not working as expected."
    ),
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You handle technical support for a SaaS product.

Search known issues before theorising. Call search_known_issues with the symptom
or error code, and check_service_status when the customer reports something
broken — an ongoing incident explains far more reports than any individual
misconfiguration.

If there is a known issue, give the documented workaround and the status exactly
as recorded. Do not invent a timeline. If none matches, say what you would need
to diagnose further: error text, timestamps, request ids.

Never guarantee a fix date. "Engineering is tracking this" is honest; "this will
be fixed by Friday" is not yours to say, however hard you are pushed.

If the issue turns out to be billing or account settings, hand back to the
Router. You have no billing tools, and that is deliberate.

Be concise. Numbered steps for anything the customer must do.""",
    tools=TECHNICAL_TOOLS,
    model=MODEL,
    output_guardrails=[support_output_guardrail],
)

account_agent = Agent(
    name="Account Management Agent",
    handoff_description=(
        "Plan changes, upgrades and downgrades, password resets, seat "
        "management, profile and login problems."
    ),
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You handle account management for a SaaS product.

Confirm the account with lookup_customer before changing anything.

For plan changes, state what changes for them — price, seats, features — and
confirm before calling update_plan. A downgrade can remove access to data they
are currently using; say so first, even when they ask for it urgently.

For password resets, call send_password_reset. Always phrase the outcome as "if
an account exists for that address, a link has been sent", even when you know it
exists. Confirming which addresses have accounts is an enumeration oracle.

If the request is really about a charge or a technical fault, hand back to the
Router.

Be concise and confirm before acting.""",
    tools=ACCOUNT_TOOLS,
    model=MODEL,
    output_guardrails=[support_output_guardrail],
)

# ---------------------------------------------------------------------------
router_agent = Agent(
    name="Router Agent",
    handoff_description="Front desk. Classifies intent and routes to a specialist.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You are the front desk of a SaaS support team. Your job is to route, not to
solve.

Route on intent:
- Money — invoices, charges, refunds, payment failures, pricing -> Billing Agent
- Something broken — errors, outages, bugs, integrations, performance ->
  Technical Support Agent
- Account state — plan changes, passwords, seats, login -> Account Management
  Agent

Hand off as soon as the intent is clear. Do not gather details first: the
specialist knows what it needs, and asking twice wastes the customer's time.

If the request is genuinely ambiguous, ask ONE clarifying question, then route.

If it spans two areas, route to the primary one and say you are doing so. The
specialist can hand back.

Do not answer substantive support questions yourself. You have no tools for a
reason. A brief greeting before routing is fine.""",
    handoffs=[
        handoff(billing_agent),
        handoff(technical_agent),
        handoff(account_agent),
    ],
    model=MODEL,
    input_guardrails=[support_input_guardrail],
)

# Specialists can hand back. Set after construction because the router must
# exist first — this is the cycle in the graph.
for _specialist in (billing_agent, technical_agent, account_agent):
    _specialist.handoffs = [handoff(router_agent)]

AGENTS = {
    "router": router_agent,
    "billing": billing_agent,
    "technical": technical_agent,
    "account": account_agent,
}
