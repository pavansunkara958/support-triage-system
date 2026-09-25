"""Tools, and the tool guardrail.

Each tool is a **plain function** with a `function_tool` wrapper beside it. The
SDK wrapper is not directly callable — it expects a run context — so keeping the
implementation separate is what lets `scripts/mock_test.py` exercise the refund
ceiling, the double-refund guard and the invoice-total rule with no API calls at
all. Business logic you can only test through a live agent is business logic you
will not test.

**The refund ceiling lives here, not in a prompt.** Input and output guardrails
reduce the chance the model does something wrong; a check inside the tool makes
it impossible. A refund above $200 is refused by a Python `if`, and no amount of
persuasion changes a Python `if`.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from agents import function_tool

from .seed_data import connect

SELF_SERVICE_REFUND_CEILING = 200.00

# Every tool call, recorded for the demo output and the trace.
TOOL_LOG: list[dict[str, Any]] = []


def _log(tool: str, outcome: str, **extra: Any) -> None:
    TOOL_LOG.append({"tool": tool, "outcome": outcome, **extra})


def _dumps(payload: Any) -> str:
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
def lookup_customer(email: str) -> str:
    """Look up a customer account by email address."""
    with connect() as conn:
        row = conn.execute(
            "SELECT customer_id, email, name, plan, seats FROM customers "
            "WHERE lower(email) = lower(?)", (email.strip(),)).fetchone()
    if row is None:
        _log("lookup_customer", "not_found", email=email)
        return _dumps({"found": False,
                       "error": "No account found for that address."})
    _log("lookup_customer", "ok", customer_id=row["customer_id"])
    return _dumps({"found": True, **dict(row)})


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
def get_invoices(customer_id: str, limit: int = 10) -> str:
    """List recent invoices for a customer, most recent first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT invoice_id, amount_usd, issued_on, description, refunded "
            "FROM invoices WHERE customer_id = ? ORDER BY issued_on DESC "
            "LIMIT ?", (customer_id, limit)).fetchall()
    _log("get_invoices", "ok", count=len(rows))
    return _dumps([dict(r) for r in rows])


def issue_refund(invoice_id: str, amount_usd: float, reason: str) -> str:
    """Refund an invoice. Self-service limit is $200; above that, escalate.

    Four refusals, in order of how badly each would fail in production:

      1. above the self-service ceiling — the authority limit
      2. above the invoice total — refunding more than was charged
      3. unknown invoice — refunding against nothing
      4. already refunded — the double-refund, guarded by a UNIQUE constraint
         rather than a read-then-write, because a retry can interleave

    Every refusal returns the escalation path, so the agent has something
    concrete to tell the customer. A refusal without a next step reads as a
    dead end and generates a second contact.
    """
    if amount_usd <= 0:
        _log("issue_refund", "refused", reason="non-positive")
        return _dumps({"refunded": False, "error": "Refund amount must be positive."})

    if amount_usd > SELF_SERVICE_REFUND_CEILING:
        _log("issue_refund", "refused", reason="above-ceiling", amount=amount_usd)
        return _dumps({
            "refunded": False,
            "error": f"${amount_usd:.2f} exceeds the "
                     f"${SELF_SERVICE_REFUND_CEILING:.2f} self-service limit. "
                     f"A supervisor must approve this refund.",
            "escalation": "billing-supervisors queue",
        })

    with connect() as conn:
        row = conn.execute(
            "SELECT amount_usd, refunded FROM invoices WHERE invoice_id = ?",
            (invoice_id,)).fetchone()

        if row is None:
            _log("issue_refund", "refused", reason="unknown-invoice")
            return _dumps({"refunded": False,
                           "error": f"No invoice {invoice_id} exists."})

        if row["refunded"]:
            _log("issue_refund", "refused", reason="already-refunded")
            return _dumps({"refunded": False,
                           "error": f"{invoice_id} has already been refunded."})

        if amount_usd > row["amount_usd"] + 1e-9:
            _log("issue_refund", "refused", reason="exceeds-invoice-total")
            return _dumps({
                "refunded": False,
                "error": f"${amount_usd:.2f} exceeds the invoice total of "
                         f"${row['amount_usd']:.2f}.",
                "max_refundable_usd": row["amount_usd"],
            })

        refund_id = f"REF-{uuid.uuid4().hex[:8].upper()}"
        try:
            conn.execute(
                "INSERT INTO refunds (refund_id, invoice_id, amount_usd, reason)"
                " VALUES (?,?,?,?)", (refund_id, invoice_id, amount_usd, reason))
        except sqlite3.IntegrityError:
            # The UNIQUE constraint on invoice_id is the real guard. The read
            # above is an optimisation; this is what makes a retry safe.
            _log("issue_refund", "refused", reason="already-refunded-race")
            return _dumps({"refunded": False,
                           "error": f"{invoice_id} has already been refunded."})
        conn.execute("UPDATE invoices SET refunded = 1 WHERE invoice_id = ?",
                     (invoice_id,))
        conn.commit()

    _log("issue_refund", "ok", refund_id=refund_id, amount=amount_usd)
    return _dumps({"refunded": True, "refund_id": refund_id,
                   "amount_usd": amount_usd,
                   "note": "Funds appear in 3-5 business days."})


# ---------------------------------------------------------------------------
# Technical
# ---------------------------------------------------------------------------
def search_known_issues(query: str) -> str:
    """Search known issues by symptom or error text."""
    terms = [t for t in query.lower().split() if len(t) > 2]
    with connect() as conn:
        rows = conn.execute(
            "SELECT issue_id, title, workaround, status, keywords "
            "FROM known_issues").fetchall()
    scored = []
    for r in rows:
        kw = set(r["keywords"].split())
        hits = sum(1 for t in terms if t in kw or any(t in k for k in kw))
        if hits:
            d = dict(r)
            d.pop("keywords", None)
            scored.append((hits, d))
    scored.sort(key=lambda x: -x[0])
    _log("search_known_issues", "ok", matches=len(scored))
    return _dumps([d for _, d in scored[:3]])


def check_service_status(component: str = "") -> str:
    """Current status of a platform component, or all components."""
    with connect() as conn:
        if component:
            rows = conn.execute(
                "SELECT * FROM service_status WHERE component = ?",
                (component.strip().lower(),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM service_status").fetchall()
    _log("check_service_status", "ok", component=component or "all")
    return _dumps([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
VALID_PLANS = ("starter", "pro", "enterprise")


def update_plan(customer_id: str, new_plan: str) -> str:
    """Change a customer's subscription plan."""
    plan = new_plan.strip().lower()
    if plan not in VALID_PLANS:
        _log("update_plan", "refused", reason="invalid-plan")
        return _dumps({"updated": False,
                       "error": f"{new_plan!r} is not a plan. "
                                f"Valid plans: {', '.join(VALID_PLANS)}."})
    with connect() as conn:
        row = conn.execute("SELECT plan FROM customers WHERE customer_id = ?",
                           (customer_id,)).fetchone()
        if row is None:
            _log("update_plan", "refused", reason="unknown-customer")
            return _dumps({"updated": False, "error": "Unknown customer."})
        conn.execute("UPDATE customers SET plan = ? WHERE customer_id = ?",
                     (plan, customer_id))
        conn.commit()
    _log("update_plan", "ok", previous=row["plan"], new=plan)
    return _dumps({"updated": True, "previous_plan": row["plan"],
                   "new_plan": plan})


def send_password_reset(email: str) -> str:
    """Send a password reset link.

    The response is identical whether or not the account exists. Confirming
    which addresses have accounts turns this into an enumeration oracle, so the
    phrasing is part of the control and is asserted in the test suite.
    """
    _log("send_password_reset", "ok")
    return _dumps({
        "sent": True,
        "note": "If an account exists for that address, a reset link has been "
                "sent. The link is valid for one hour.",
    })


# ---------------------------------------------------------------------------
# SDK wrappers and per-agent scopes
# ---------------------------------------------------------------------------
lookup_customer_tool = function_tool(lookup_customer)
get_invoices_tool = function_tool(get_invoices)
issue_refund_tool = function_tool(issue_refund)
search_known_issues_tool = function_tool(search_known_issues)
check_service_status_tool = function_tool(check_service_status)
update_plan_tool = function_tool(update_plan)
send_password_reset_tool = function_tool(send_password_reset)

# Tool scope is the security boundary. The technical agent cannot refund
# because it was never constructed with a refund tool — no prompt can reach a
# tool that is not in the list.
BILLING_TOOLS = [lookup_customer_tool, get_invoices_tool, issue_refund_tool]
TECHNICAL_TOOLS = [lookup_customer_tool, search_known_issues_tool,
                   check_service_status_tool]
ACCOUNT_TOOLS = [lookup_customer_tool, update_plan_tool,
                 send_password_reset_tool]
