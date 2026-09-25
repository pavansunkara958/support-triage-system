"""Provider configuration.

Two providers, one agent graph:

  openai    Real OpenAI. Uses the RESPONSES API (the SDK default), which the
            Responses-API workflow in responses_workflow.py requires, and which
            is what uploads traces to the OpenAI dashboard.
  gateway   Any OpenAI-compatible endpoint (Groq, Google AI Studio, SharedLLM).
            Uses CHAT COMPLETIONS, because gateways implement
            /v1/chat/completions and generally not /v1/responses.

The agents, handoffs, guardrails and sessions are identical either way — only
the model client changes. Provider choice is infrastructure, not application
logic, and keeping that boundary sharp is what lets the same code be graded
against a free endpoint and run against a paid one.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("PROVIDER", "openai").lower()

MODEL_ROUTER = os.getenv("MODEL_ROUTER", "gpt-5-mini")
MODEL_SPECIALIST = os.getenv("MODEL_SPECIALIST", "gpt-5-mini")
MODEL_GUARDRAIL = os.getenv("MODEL_GUARDRAIL", "gpt-5-nano")

GATEWAY_BASE_URL = os.getenv("GATEWAY_BASE_URL", "")
GATEWAY_AUTH_MODE = os.getenv("GATEWAY_AUTH_MODE", "bearer").lower()
GATEWAY_HEADER_NAME = os.getenv("GATEWAY_HEADER_NAME", "X-Api-Key")
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "")
GATEWAY_MODEL = os.getenv("GATEWAY_MODEL", "")

MAX_TURNS = int(os.getenv("MAX_TURNS", "12"))
SESSION_DB = os.getenv("SESSION_DB", "data/conversations.db")

# Dashboard tracing needs a real OpenAI key. A gateway key cannot upload, so it
# is disabled there to avoid a stream of 401s from the tracing exporter.
TRACE_TO_OPENAI = PROVIDER == "openai" and os.getenv("DISABLE_TRACING") != "1"


def _gateway_client():
    """Build an AsyncOpenAI pointed at a gateway, with auth sorted out.

    A gateway that authenticates on a CUSTOM HEADER may forward the client's
    `Authorization` header verbatim to the upstream provider, which then rejects
    it — producing a 401 that reads as if your gateway key were wrong when the
    gateway actually authenticated you fine. The tell is the error body: it
    names platform.openai.com rather than the gateway.

    The OpenAI client requires a non-empty `api_key` and always turns it into a
    bearer header, so there is no constructor flag that suppresses it. It has to
    be removed on the way out.
    """
    from openai import AsyncOpenAI

    if GATEWAY_AUTH_MODE == "header":
        import httpx

        async def _strip_authorization(request: "httpx.Request") -> None:
            request.headers.pop("authorization", None)

        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            event_hooks={"request": [_strip_authorization]},
        )
        return AsyncOpenAI(
            base_url=GATEWAY_BASE_URL,
            api_key="unused",                       # stripped before the wire
            default_headers={
                GATEWAY_HEADER_NAME: GATEWAY_KEY,
                # Brotli responses have broken this client before. Ask for none.
                "Accept-Encoding": "identity",
            },
            http_client=http_client,
        )

    return AsyncOpenAI(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_KEY)


def configure_sdk() -> str:
    """Wire the SDK to the chosen provider. Returns the effective model name."""
    from agents import (set_default_openai_api, set_default_openai_client,
                        set_tracing_disabled)

    if PROVIDER == "gateway":
        if not GATEWAY_BASE_URL or not GATEWAY_KEY:
            raise SystemExit(
                "\nPROVIDER=gateway needs GATEWAY_BASE_URL and GATEWAY_KEY.\n"
                "See .env.example, or run:  python -m scripts.preflight\n")
        set_default_openai_client(_gateway_client(), use_for_tracing=False)
        set_default_openai_api("chat_completions")
        set_tracing_disabled(True)
        return GATEWAY_MODEL or MODEL_SPECIALIST

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "\nMissing OPENAI_API_KEY.\n"
            "Either set it in .env, or use a free gateway:\n"
            "  PROVIDER=gateway  (see .env.example)\n")

    if not TRACE_TO_OPENAI:
        set_tracing_disabled(True)
    return MODEL_ROUTER


def specialist_model() -> str:
    return GATEWAY_MODEL or MODEL_SPECIALIST if PROVIDER == "gateway" \
        else MODEL_SPECIALIST


def guardrail_model() -> str:
    return GATEWAY_MODEL or MODEL_GUARDRAIL if PROVIDER == "gateway" \
        else MODEL_GUARDRAIL


def describe() -> str:
    target = GATEWAY_MODEL if PROVIDER == "gateway" else MODEL_SPECIALIST
    return (f"provider={PROVIDER}  model={target}  "
            f"dashboard_tracing={'on' if TRACE_TO_OPENAI else 'off'}")
