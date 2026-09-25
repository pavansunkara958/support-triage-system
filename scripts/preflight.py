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


PLACEHOLDER_MARKERS = ("paste", "replace_me", "replace", "your_key",
                       "yours_here", "xxxx", "changeme", "<", "sk-proj-REPLACE")


def _looks_like_placeholder(key: str) -> bool:
    """Catch an unedited .env before spending a request on it.

    This is the single most common way a run fails, and the error it produces —
    a plain 401 — is indistinguishable from a revoked key, a wrong endpoint or
    a quota problem. Checking the string first turns twenty minutes of
    diagnosis into one line.
    """
    low = key.lower()
    return (not key
            or any(m in low for m in PLACEHOLDER_MARKERS)
            or len(key) < 24)


def _explain(msg: str) -> None:
    """Read the failure, so the next step is obvious."""
    # Only blame header forwarding when the error actually came from the
    # UPSTREAM provider. Matching on `invalid_api_key` alone was wrong: Groq
    # and most gateways return that same code for an ordinary bad key, so this
    # branch confidently sent people to change an auth mode that was already
    # correct. The domain in the message is the discriminator, not the code.
    if "platform.openai.com" in msg:
        print("\n  That 401 came from OPENAI, not the gateway — so the gateway")
        print("  authenticated you and forwarded a competing Authorization")
        print("  header upstream. Set GATEWAY_AUTH_MODE=header in .env.")
    elif "invalid api key" in msg.lower() or "invalid_api_key" in msg:
        print("\n  The endpoint rejected the key itself. In order of likelihood:")
        print("    1. .env still holds a placeholder, or was edited but not saved")
        print("    2. the key was revoked or belongs to a different project")
        print("    3. the key is for a different provider than GATEWAY_BASE_URL")
        print("  Check the key fingerprint printed above against your console.")
    elif "insufficient_quota" in msg or "credit" in msg.lower():
        print("\n  The key is valid; the account has no credit. Either add some")
        print("  (~$0.05 runs this lab) or switch to PROVIDER=gateway.")
    elif "invalid or inactive" in msg.lower():
        print("\n  That 401 came from the GATEWAY — the key is wrong, revoked,")
        print("  or the .env edit was never saved.")
    elif "no active" in msg.lower() or "402" in msg:
        print("\n  Authentication worked; the endpoint has no upstream key for")
        print("  this model. Check which models the gateway actually serves.")
    elif "model_not_found" in msg or "does not exist" in msg.lower():
        print("\n  The model does not exist on this endpoint — usually because")
        print("  the provider retired it. List what is actually served:")
        print("      python -m scripts.preflight --models")
        print("  Then set GATEWAY_MODEL in .env to one that supports tools.")
    elif "model" in msg.lower():
        print("\n  The model name may not exist on this endpoint.")


async def list_models() -> int:
    """Ask the endpoint which models it actually serves.

    Providers retire models with little notice, and the resulting 404 reads
    like a configuration error rather than "this no longer exists". Asking the
    endpoint is faster and more reliable than any list that ships in a repo,
    which is stale the moment it is written.
    """
    client, _ = _client_and_model()
    print("=" * 74)
    print(f"MODELS SERVED BY {C.GATEWAY_BASE_URL or 'OpenAI'}")
    print("=" * 74)
    try:
        page = await client.models.list()
    except Exception as exc:
        print(f"  could not list models: {type(exc).__name__}: {str(exc)[:160]}")
        return 1

    ids = sorted(m.id for m in page.data)
    for mid in ids:
        print(f"  {mid}")
    print(f"\n  {len(ids)} model(s).")
    print("\n  Pick one that supports TOOL CALLING — handoffs in the Agents SDK")
    print("  are function calls, so a model without it cannot route at all.")
    print("  Set it as GATEWAY_MODEL in .env, then run preflight again.")
    return 0


async def main() -> int:
    if "--models" in sys.argv:
        return await list_models()

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

    # -- 0: is the key even real? -----------------------------------------
    key = C.GATEWAY_KEY if C.PROVIDER == "gateway" \
        else os.getenv("OPENAI_API_KEY", "")
    if _looks_like_placeholder(key):
        shown = f"{key[:10]}...{key[-4:]}" if len(key) > 14 else repr(key)
        print(f"\n  [FAIL] the configured key looks like a placeholder: {shown}")
        print(f"         ({len(key)} characters)")
        print("\n  .env has not been edited, or was edited and not saved.")
        print("  Open it and replace the key, then save:")
        print("      nano .env          # or: open -e .env")
        print("  Verify with:")
        print("      grep -o '^[A-Z_]*KEY=.\\{0,8\\}' .env")
        return 1

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