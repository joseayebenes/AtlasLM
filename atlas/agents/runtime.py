"""The agents of the Markdown files as ADK agents, and running them.

    agent = build_agent("assistant", cfg)            # an ADK LlmAgent, for adk web or a runner
    res = run_agent("assistant", "Which tests verify SRS-0004?", cfg)
    res["answer"], res["calls"]

An agent gets its model from [llm] (with the model, temperature and reasoning of its file or
of [agents]) through AtlasLlm, and its tools from the "tool" plugins with the names of its
file: the same functions that atlas serve offers. max_steps caps the calls to the model in
one answer, and the same tool call repeated REPEATS times in a row is not run again (see
loop_guard).
"""
from __future__ import annotations

import asyncio
import functools
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from atlas import plugins
from atlas.agents.definitions import AgentDef, load_definitions
from atlas.config import Config, ConfigError
from atlas.llm import ServiceConfig, from_config
from atlas.store import Store

__all__ = ["agent_service", "agent_tools", "ask", "build_agent", "context_for", "definition", "loop_guard",
           "recorded", "run_agent"]

REPEATS = 3      # the same call this many times in a row is a loop


def context_for(cfg: Config) -> SimpleNamespace:
    """What the tool plugins receive (cfg, store), with the plugins of the configuration."""
    plugins.load_plugins(cfg["plugins"]["modules"])
    return SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])


def definition(name: str, cfg: Config) -> AgentDef:
    found = load_definitions(cfg)
    if name not in found:
        raise ConfigError(f"there is no agent '{name}' (agents: {', '.join(sorted(found))})")
    return found[name]


def agent_service(d: AgentDef, cfg: Config) -> ServiceConfig:
    """The [llm] connection with the model, temperature and reasoning of the agent."""
    llm = from_config(cfg).llm
    return replace(llm, model=d.model or cfg["agents"]["model"] or llm.model,
                   temperature=llm.temperature if d.temperature is None else d.temperature,
                   reasoning_effort=d.reasoning_effort or llm.reasoning_effort)


def agent_tools(d: AgentDef, context: Any) -> list:
    """The functions of the tools of an agent (the methods behind the "tool" plugins)."""
    available, disabled = set(plugins.available("tool")), set(context.disabled)
    unknown = [t for t in d.tools if t not in available]
    if unknown:
        raise ConfigError(f"agent {d.name}: unknown tools {unknown} (available: {', '.join(sorted(available))})")
    off = [t for t in d.tools if f"tool:{t}" in disabled]
    if off:
        raise ConfigError(f"agent {d.name}: the tools {off} are disabled in [plugins] disabled")
    return [plugins.get("tool", t, context.disabled)(context).function() for t in d.tools]


def loop_guard(functions: list) -> list:
    """The tools of one agent, wrapped so that the same call with the same arguments, the
    REPEATS-th time in a row, is not run: the model gets an error that tells it to do something
    else. Small models fall into these loops (gemma-4-26b wrote a file 17 times with the same
    content after an error it did not understand) and spend every step of the agent."""
    last: dict = {"call": None, "times": 0}

    def wrap(f):
        @functools.wraps(f)
        def guarded(*args, **kwargs):
            call = (f.__name__, json.dumps([args, kwargs], sort_keys=True, ensure_ascii=False, default=str))
            last["times"] = last["times"] + 1 if call == last["call"] else 1
            last["call"] = call
            if last["times"] >= REPEATS:
                return {"error": f"this is the same {f.__name__} call, with the same arguments, {last['times']} "
                                 "times in a row: it gives the same result every time. Do something different: if "
                                 "a tool reported an error, change what causes it; if the work is done, answer."}
            return f(*args, **kwargs)
        return guarded
    return [wrap(f) for f in functions]


def recorded(functions: list, record: list) -> list:
    """The tools, wrapped so that each call goes to `record` with its arguments, its result
    and its seconds, to study afterwards what an agent did."""
    def wrap(f):
        @functools.wraps(f)
        def logged(*args, **kwargs):
            t0 = time.time()
            result = f(*args, **kwargs)
            record.append({"name": f.__name__, "args": kwargs, "result": result,
                           "seconds": round(time.time() - t0, 2)})
            return result
        return logged
    return [wrap(f) for f in functions]


def build_agent(name: str, cfg: Config, context: Any = None, client: Any = None, record: list | None = None):
    """The ADK LlmAgent of an agent file. `client` replaces the OpenAI client, for tests;
    `record`, a list, gets every tool call with its result (see recorded)."""
    try:
        from google.adk.agents import LlmAgent
    except ImportError as e:
        raise ImportError('the agents need ADK: pip install -e ".[agents]"') from e
    from atlas.agents.model import AtlasLlm
    d = definition(name, cfg)
    text = d.instruction
    tools = agent_tools(d, context or context_for(cfg))
    model = AtlasLlm.from_service(agent_service(d, cfg), client, top_p=d.top_p, top_k=d.top_k)
    return LlmAgent(name=d.name, description=d.description, model=model,
                    tools=loop_guard(recorded(tools, record) if record is not None else tools),
                    instruction=lambda _: text)   # a function: ADK does not fill {placeholders} in it


async def ask(agent, question: str, max_steps: int = 8) -> dict:
    """One question to an ADK agent: {"answer", "calls": [{"name", "args"}], "stopped",
    "tokens": {"prompt", "completion"}, and "cost" when the server says it (OpenRouter)}.
    `stopped` says that it reached max_steps calls to the model before answering."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    runner = InMemoryRunner(agent=agent, app_name="atlas")
    session = await runner.session_service.create_session(app_name="atlas", user_id="atlas")
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls, answer, stopped = [], "", False
    tokens, cost = {"prompt": 0, "completion": 0}, None
    try:
        async for event in runner.run_async(user_id="atlas", session_id=session.id, new_message=message,
                                            run_config=RunConfig(max_llm_calls=max_steps)):
            if event.usage_metadata:
                tokens["prompt"] += event.usage_metadata.prompt_token_count or 0
                tokens["completion"] += event.usage_metadata.candidates_token_count or 0
            if (event.custom_metadata or {}).get("cost") is not None:
                cost = (cost or 0.0) + float(event.custom_metadata["cost"])
            for part in (event.content.parts if event.content else None) or []:
                if part.function_call:
                    calls.append({"name": part.function_call.name, "args": dict(part.function_call.args or {})})
                elif part.text and not part.thought and event.is_final_response():
                    answer += part.text
    except LlmCallsLimitExceededError:        # max_steps reached
        stopped = True
    return {"answer": answer.strip(), "calls": calls, "stopped": stopped, "tokens": tokens,
            **({"cost": round(cost, 6)} if cost is not None else {})}


def run_agent(name: str, question: str, cfg: Config, context: Any = None, client: Any = None,
              record: list | None = None) -> dict:
    """Builds the agent `name` and asks it `question` (see ask); `record` gets its tool calls
    with their results."""
    agent = build_agent(name, cfg, context, client, record)
    return asyncio.run(ask(agent, question, definition(name, cfg).max_steps))
