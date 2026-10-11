"""The agents of the Markdown files as ADK agents, and running them.

    agent = build_agent("assistant", cfg)            # an ADK LlmAgent, for adk web or a runner
    res = run_agent("assistant", "Which tests verify SRS-0004?", cfg)
    res["answer"], res["calls"]

An agent gets its model from [llm] (with the model, temperature and reasoning of its file or
of [agents]) through AtlasLlm, and its tools from the "tool" plugins with the names of its
file: the same functions that atlas serve offers. max_steps caps the calls to the model in
one answer.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from atlas import plugins
from atlas.agents.definitions import AgentDef, load_definitions
from atlas.config import Config, ConfigError
from atlas.llm import ServiceConfig, from_config
from atlas.store import Store

__all__ = ["agent_service", "agent_tools", "ask", "build_agent", "context_for", "definition", "run_agent"]


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


def build_agent(name: str, cfg: Config, context: Any = None, client: Any = None):
    """The ADK LlmAgent of an agent file. `client` replaces the OpenAI client, for tests."""
    try:
        from google.adk.agents import LlmAgent
    except ImportError as e:
        raise ImportError('the agents need ADK: pip install -e ".[agents]"') from e
    from atlas.agents.model import AtlasLlm
    d = definition(name, cfg)
    text = d.instruction
    return LlmAgent(name=d.name, description=d.description, model=AtlasLlm.from_service(agent_service(d, cfg), client),
                    tools=agent_tools(d, context or context_for(cfg)),
                    instruction=lambda _: text)   # a function: ADK does not fill {placeholders} in it


async def ask(agent, question: str, max_steps: int = 8) -> dict:
    """One question to an ADK agent: {"answer", "calls": [{"name", "args"}], "stopped"}.
    `stopped` says that it reached max_steps calls to the model before answering."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    runner = InMemoryRunner(agent=agent, app_name="atlas")
    session = await runner.session_service.create_session(app_name="atlas", user_id="atlas")
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls, answer, stopped = [], "", False
    try:
        async for event in runner.run_async(user_id="atlas", session_id=session.id, new_message=message,
                                            run_config=RunConfig(max_llm_calls=max_steps)):
            for part in (event.content.parts if event.content else None) or []:
                if part.function_call:
                    calls.append({"name": part.function_call.name, "args": dict(part.function_call.args or {})})
                elif part.text and not part.thought and event.is_final_response():
                    answer += part.text
    except LlmCallsLimitExceededError:        # max_steps reached
        stopped = True
    return {"answer": answer.strip(), "calls": calls, "stopped": stopped}


def run_agent(name: str, question: str, cfg: Config, context: Any = None, client: Any = None) -> dict:
    """Builds the agent `name` and asks it `question` (see ask)."""
    agent = build_agent(name, cfg, context, client)
    return asyncio.run(ask(agent, question, definition(name, cfg).max_steps))
