"""atlas probe: whether the chat model of [llm] works as an agent, with three small requests.

    atlas probe atlas.toml

1. A question that needs a tool: the model must call it, with the right arguments.
2. The result of the tool: the model must answer with it.
3. The same request without the reasoning of the first turn: if the server gives the token
   count of the prompt, the difference says whether it puts back the reasoning that atlas
   returns in [llm] reasoning_field. A chat template that drops it, or a server that does not
   read that field, leaves the prompt as it was.

The first two fail with a server whose chat template breaks tool calls (the first templates
of gemma 4 closed the turn in the middle of a tool call, and OpenCode fell into loops). The
model reasons with [llm] reasoning_effort, or "low" if it is empty or "none": the agents that
write code reason.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from atlas.llm.connection import ServiceConfig, create_client, describe, diagnose, note_usage, reasoning_of

__all__ = ["probe"]

TOOL = {"type": "function", "function": {
    "name": "word_length", "description": "The number of letters of a word.",
    "parameters": {"type": "object", "properties": {"word": {"type": "string", "description": "the word"}},
                   "required": ["word"]}}}
SYSTEM = "You answer with the tools you are given: call a tool when it can give the answer."
QUESTION = "How many letters has the word 'atlas'? Find it out with the tool word_length."
RESULT = {"word": "atlas", "letters": 5}


def probe(service: ServiceConfig, client: Any = None, out: Callable[[str], None] = print) -> int:
    """The three requests (see the module); prints what each one shows and returns 0 when the
    model calls the tool and answers with its result."""
    out(f"LLM: {describe(service)}")
    effort = service.reasoning_effort if service.reasoning_effort not in ("", "none") else "low"
    out(f"  reasoning_effort for the probe: {effort}; reasoning goes back in: "
        f"{service.reasoning_field or '(nothing: reasoning_field is empty)'}")
    if client is None:
        client, _ = create_client(service)
    base = {"model": service.model, "temperature": service.temperature, "tools": [TOOL], "reasoning_effort": effort,
            **({"max_tokens": service.max_tokens} if service.max_tokens else {}),
            **({"extra_body": service.extra_body} if service.extra_body else {})}
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": QUESTION}]

    def ask(msgs: list[dict]) -> tuple[Any, float]:
        t0 = time.time()
        response = client.chat.completions.create(messages=msgs, **base)
        note_usage(response)
        return response, time.time() - t0

    try:
        first, seconds = ask(messages)
    except Exception as e:
        out(f"  FAILED: {diagnose(e)}")
        return 1
    message = first.choices[0].message
    calls = message.tool_calls or []
    reasoning = reasoning_of(message)
    thought = f"; it reasoned ({len(reasoning)} characters)" if reasoning else "; no reasoning came back"
    call = next((c for c in calls if c.function.name == "word_length"), None)
    if call is None:
        out(f"1 FAILED ({seconds:.1f}s): the model did not call the tool{thought}. It wrote: "
            f"{(message.content or '').strip()[:200]!r}")
        return 1
    try:
        args = json.loads(call.function.arguments or "{}")
    except ValueError:
        args = {}
    good = str(args.get("word", "")).strip().lower() == "atlas"
    out(f"1 {'OK' if good else 'FAILED'} ({seconds:.1f}s): it called word_length({call.function.arguments})"
        f"{thought}")
    if not good:
        return 1

    assistant = {"role": "assistant", "content": message.content or None,
                 "tool_calls": [{"id": call.id, "type": "function",
                                 "function": {"name": "word_length", "arguments": call.function.arguments}}]}
    result = {"role": "tool", "tool_call_id": call.id, "content": json.dumps(RESULT)}
    with_reasoning = {**assistant, service.reasoning_field: reasoning} if reasoning and service.reasoning_field \
        else assistant
    try:
        second, seconds = ask(messages + [with_reasoning, result])
    except Exception as e:
        out(f"2 FAILED: {diagnose(e)}")
        return 1
    answer = (second.choices[0].message.content or "").strip()
    answered = "5" in answer or "five" in answer.lower()
    out(f"2 {'OK' if answered else 'FAILED'} ({seconds:.1f}s): it answered {answer[:120]!r}")

    if not (reasoning and service.reasoning_field):
        out("3 skipped: " + ("no reasoning came back to give back" if not reasoning else "reasoning_field is empty"))
        return 0 if answered else 1
    try:
        third, _ = ask(messages + [assistant, result])
    except Exception as e:
        out(f"3 FAILED: {diagnose(e)}")
        return 0 if answered else 1
    used, bare = (getattr(getattr(r, "usage", None), "prompt_tokens", None) for r in (second, third))
    if not used or not bare:
        out("3 cannot tell: the server gives no prompt_tokens")
    elif used - bare >= max(8, len(reasoning) // 16):
        out(f"3 OK: the server puts the reasoning back in the prompt ({used} tokens with it, {bare} without)")
    else:
        other = "reasoning_content" if service.reasoning_field == "reasoning" else "reasoning"
        out(f"3 NOT USED: the prompt has {used} tokens with the reasoning and {bare} without: the chat template "
            f"drops it or the server does not read '{service.reasoning_field}'. Try [llm] reasoning_field = "
            f"\"{other}\", or \"\" to stop sending it")
    return 0 if answered else 1
