"""Run the same tool-calling loop against Anthropic or OpenAI.

The tools, the schema and the grounding rules are identical either way — only
the wire format for declaring tools and reading tool calls differs. Keeping
that difference in one file means adding a third provider later touches nothing
else, and the safety rules can't drift between vendors.

Both loops are written by hand rather than using each SDK's runner helper: the
two runners have different iteration semantics, and one explicit loop that
behaves identically on both is easier to reason about than two that almost do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_ITERATIONS = 12
MAX_TOKENS = 8000

ANTHROPIC_MODEL = "claude-opus-4-8"
OPENAI_MODEL = "gpt-5"


@dataclass
class Result:
    text: str
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    queries: list[str] = field(default_factory=list)


class ProviderError(RuntimeError):
    """Anything the user needs to act on — bad key, no credit, wrong provider."""


def _friendly(exc: Exception) -> ProviderError:
    msg = str(exc)
    low = msg.lower()
    if "authentication" in low or "invalid_api_key" in low or "401" in low:
        return ProviderError("That API key was rejected. Check it in Settings.")
    if "insufficient_quota" in low or "credit balance" in low or "billing" in low:
        return ProviderError("The provider reports no available credit on that key.")
    if "rate limit" in low or "429" in low:
        return ProviderError("Rate limited by the provider. Try again shortly.")
    if "model" in low and "not found" in low:
        return ProviderError("That model isn't available on your account's plan.")
    return ProviderError(f"{type(exc).__name__}: {msg[:300]}")


# --- Anthropic -------------------------------------------------------------

def _run_anthropic(
    api_key: str, system: str, messages: list[dict], specs: list[dict],
    dispatch: Callable[[str, dict], str],
) -> Result:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    tools = [{
        "name": t["name"], "description": t["description"],
        "input_schema": t["parameters"],
    } for t in specs]

    convo = list(messages)
    used: list[str] = []
    queries: list[str] = []

    for i in range(MAX_ITERATIONS):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": "xhigh"},
                tools=tools, messages=convo,
            )
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc) from exc

        if resp.stop_reason == "refusal":
            return Result(text="The provider declined to answer that.", iterations=i + 1)

        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            return Result(text=text, tools_used=used, iterations=i + 1, queries=queries)

        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:
            used.append(call.name)
            if call.name == "query_data":
                queries.append(str(call.input.get("sql", "")))
            results.append({
                "type": "tool_result", "tool_use_id": call.id,
                "content": dispatch(call.name, call.input),
            })
        convo.append({"role": "user", "content": results})

    return Result(text=_ran_out(), tools_used=used, iterations=MAX_ITERATIONS,
                  queries=queries)


# --- OpenAI ----------------------------------------------------------------

def _run_openai(
    api_key: str, system: str, messages: list[dict], specs: list[dict],
    dispatch: Callable[[str, dict], str],
) -> Result:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["parameters"],
    }} for t in specs]

    convo: list[dict[str, Any]] = [{"role": "system", "content": system}]
    convo += [{"role": m["role"], "content": m["content"]} for m in messages]
    used: list[str] = []
    queries: list[str] = []

    for i in range(MAX_ITERATIONS):
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL, messages=convo, tools=tools,
                max_completion_tokens=MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc) from exc

        msg = resp.choices[0].message
        if not msg.tool_calls:
            return Result(text=(msg.content or "").strip(), tools_used=used,
                          iterations=i + 1, queries=queries)

        convo.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [{
                "id": c.id, "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            } for c in msg.tool_calls],
        })
        for call in msg.tool_calls:
            used.append(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if call.function.name == "query_data":
                queries.append(str(args.get("sql", "")))
            convo.append({
                "role": "tool", "tool_call_id": call.id,
                "content": dispatch(call.function.name, args),
            })

    return Result(text=_ran_out(), tools_used=used, iterations=MAX_ITERATIONS,
                  queries=queries)


def _ran_out() -> str:
    return ("I couldn't finish that one — it needed more steps than allowed. "
            "Try asking something narrower.")


RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}


def run(provider: str, api_key: str, system: str, messages: list[dict],
        specs: list[dict], dispatch: Callable[[str, dict], str]) -> Result:
    runner = RUNNERS.get(provider)
    if runner is None:
        raise ProviderError(f"Unknown provider '{provider}'.")
    return runner(api_key, system, messages, specs, dispatch)
