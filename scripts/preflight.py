"""Endpoint preflight — can this provider actually run the lab?

Four questions, answered for a fraction of a cent before committing to a full
run. Ordered so the first failure tells you the most.

  1. Does the endpoint respond at all?
  2. Does it support TOOL CALLING? Handoffs in the Agents SDK *are* tool calls,
     so an endpoint that strips `tools` cannot route. This decides the lab.
  3. Does it accept a strict json_schema response format? The relevance
     guardrail compiles to exactly that.
  4. Does it serve /v1/responses? Required for `--review`.

Reads its configuration from src.config, so preflight and the real run share one
auth path. A preflight with its own client can pass while the real run fails —
or, as happened once, fail for a reason the real run would never have hit.

Run:  python -m scripts.preflight
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as C                                    # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    if detail:
        print(f"         {detail}")


def _client_and_model():
    if C.PROVIDER == "gateway":
        if not C.GATEWAY_BASE_URL or not C.GATEWAY_KEY:
            raise SystemExit("\nPROVIDER=gateway needs GATEWAY_BASE_URL and "
                             "GATEWAY_KEY in .env\n")
        return C._gateway_client(), (C.GATEWAY_MODEL or C.MODEL_SPECIALIST)

    from openai import AsyncOpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("\nPROVIDER=openai needs OPENAI_API_KEY in .env\n")
    return AsyncOpenAI(), C.MODEL_SPECIALIST


def _explain(msg: str) -> None:
    """Read the failure, so the next step is obvious."""
    if "platform.openai.com" in msg or "invalid_api_key" in msg:
        print("\n  That 401 came from OPENAI, not the gateway — so the gateway")
        print("  authenticated you and forwarded a competing Authorization")
        print("  header upstream. Set GATEWAY_AUTH_MODE=header in .env.")
    elif "insufficient_quota" in msg or "credit" in msg.lower():
        print("\n  The key is valid; the account has no credit. Either add some")
        print("  (~$0.05 runs this lab) or switch to PROVIDER=gateway.")
    elif "invalid or inactive" in msg.lower():
        print("\n  That 401 came from the GATEWAY — the key is wrong, revoked,")
        print("  or the .env edit was never saved.")
    elif "no active" in msg.lower() or "402" in msg:
        print("\n  Authentication worked; the endpoint has no upstream key for")
        print("  this model. Check which models the gateway actually serves.")
    elif "model" in msg.lower():
        print("\n  The model name may not exist on this endpoint.")


async def main() -> int:
    print("=" * 74)
    print("PREFLIGHT")
    print("=" * 74)
    print(f"  {C.describe()}")
    if C.PROVIDER == "gateway":
        key = C.GATEWAY_KEY
        print(f"  base_url : {C.GATEWAY_BASE_URL}")
        print(f"  auth     : {C.GATEWAY_AUTH_MODE}"
              f"{'  (Authorization stripped)' if C.GATEWAY_AUTH_MODE == 'header' else ''}")
        print(f"  key      : {key[:10]}...{key[-4:]}  ({len(key)} chars)")

    client, model = _client_and_model()

    # -- 1 ---------------------------------------------------------------
    print("\n[1] BASIC COMPLETION")
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with one word: ok"}])
        text = (r.choices[0].message.content or "").strip()
        check("endpoint answers", bool(text), text[:60])
    except Exception as exc:
        msg = str(exc)
        check("endpoint answers", False, f"{type(exc).__name__}: {msg[:180]}")
        _explain(msg)
        print("\n  Stop here. Nothing else can pass if this does not.")
        return 1

    # -- 2 ---------------------------------------------------------------
    print("\n[2] TOOL CALLING  (handoffs are tool calls; this decides the lab)")
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": "Look up the customer dana.k@northwind.example"}],
            tools=[{"type": "function", "function": {
                "name": "lookup_customer",
                "description": "Look up a customer by email address.",
                "parameters": {"type": "object",
                               "properties": {"email": {"type": "string"}},
                               "required": ["email"]}}}])
        calls = r.choices[0].message.tool_calls
        check("model emits a tool call", bool(calls),
              str(calls[0].function)[:110] if calls
              else "returned prose instead of a tool call — handoffs will not work")
    except Exception as exc:
        check("model emits a tool call", False,
              f"{type(exc).__name__}: {str(exc)[:180]}")

    # -- 3 ---------------------------------------------------------------
    print("\n[3] STRICT JSON SCHEMA  (the relevance guardrail needs this)")
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": "Is 'my invoice is wrong' a support request?"}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "RelevanceCheck", "strict": True,
                "schema": {"type": "object",
                           "properties": {"on_topic": {"type": "boolean"},
                                          "reasoning": {"type": "string"}},
                           "required": ["on_topic", "reasoning"],
                           "additionalProperties": False}}})
        body = (r.choices[0].message.content or "").strip()
        check("strict json_schema accepted", body.startswith("{"), body[:70])
    except Exception as exc:
        check("strict json_schema accepted", False,
              f"{type(exc).__name__}: {str(exc)[:150]}")

    # -- 4 ---------------------------------------------------------------
    print("\n[4] RESPONSES API  (needed for --review)")
    try:
        r = await client.responses.create(model=model, input="Reply: ok")
        check("/v1/responses served",
              bool(getattr(r, "output_text", "")), "")
    except Exception as exc:
        check("/v1/responses served", False,
              f"{type(exc).__name__}: {str(exc)[:110]}")

    # -- verdict ----------------------------------------------------------
    passed = dict(RESULTS)
    print("\n" + "=" * 74)
    for name, ok in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 74)

    if not passed.get("model emits a tool call"):
        print("\nVERDICT: this endpoint cannot run the lab.")
        print("Without tool calling there are no handoffs and no tools.")
        return 1

    missing = [n for n in ("strict json_schema accepted", "/v1/responses served")
               if not passed.get(n)]
    if missing:
        print("\nVERDICT: usable, with gaps.")
        for n in missing:
            if n.startswith("strict"):
                print("  - The relevance guardrail needs a plain-text fallback.")
                print("    The deterministic guardrail layer is unaffected.")
            else:
                print("  - `--review` will not run. Six of seven requirements")
                print("    still work; note the gap in the README.")
        return 0

    print("\nVERDICT: good to run.")
    print("  python -m src.main --demo")
    print("  python -m src.main --guardrail-demo")
    print("  python -m src.main --review")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
