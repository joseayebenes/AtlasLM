"""The agents of the Markdown files as ADK agents, and running them.

    agent = build_agent("assistant", cfg)            # an ADK LlmAgent, for adk web or a runner
    res = run_agent("assistant", "Which tests verify SRS-0004?", cfg)
    res["answer"], res["calls"], res["sources"]

    chat = Conversation("assistant", cfg)            # several questions: each sees the earlier ones
    chat.ask("Which tests verify SRS-0004?")
    chat.ask("And which code do they call?")

An agent gets its model from [llm] (with the model, temperature and reasoning of its file or
of [agents]) through AtlasLlm, and its tools from the "tool" plugins with the names of its
file: the same functions that atlas serve offers. max_steps caps the calls to the model in
one answer, and the same tool call repeated REPEATS times in a row is not run again (see
loop_guard). With stall_steps, a run that goes that many tool calls without changing the code
or making a test pass ends there, `stalled` (see stall_guard): atlas develop starts it again
with a fresh conversation.

Every tool result goes to the model. With local-only sources (model = "none"), the tools of
an agent get a context of their own (`for_model`, see model_context) in which the document
and graph tools leave their text out; an agent with tools that read or compile the code is
refused when the code is local-only.

A Conversation makes the agent, its runner and its ADK session once: each ask() is one more
turn of the same session, with max_steps calls to the model, and keep_tool_results keeps what
goes back to the model short as the conversation grows. run_agent is a conversation of one
question. The answer of a turn has the citations of its tool results (`sources`, see sources);
`stopped` says that it is a partial answer.
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from atlas import plugins
from atlas.agents.definitions import AgentDef, load_definitions
from atlas.config import Config, ConfigError
from atlas.llm import ServiceConfig, from_config
from atlas.store import Store

__all__ = ["Conversation", "agent_service", "agent_tools", "ask", "build_agent", "context_for", "definition",
           "loop_guard", "model_context", "recorded", "run_agent", "sources", "stall_guard", "tool_functions"]

REPEATS = 3      # the same call this many times in a row is a loop
CHANGES = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")
KEEP_IN_CHAT = 6     # the large tool results that atlas chat keeps whole, if the agent does not say
APP, USER = "atlas", "atlas"     # the names of the ADK app and user of the sessions


def context_for(cfg: Config) -> SimpleNamespace:
    """What the tool plugins receive (cfg, store), with the plugins of the configuration."""
    plugins.load_plugins(cfg["plugins"]["modules"])
    return SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])


CODE_TOOLS = ("list_files", "grep", "read_file", "edit_file", "write_file", "replace_subprogram", "add_subprogram",
              "check_code", "run_test", "run_program", "find_definition", "find_references", "describe_symbol",
              "outline", "read_subprogram")      # they read the code as it is, outside the graph


def model_context(context: Any, cfg: Config) -> Any:
    """The context for the tools of an agent: the same one if every source may go to the
    model; otherwise a copy marked `for_model`, without the document, graph and agent tools
    of the original (an MCP server shares its context with ask_atlas), so that they are made
    again without the text of the local-only sources."""
    from atlas.outbound import LocalOnly
    if not LocalOnly.from_config(cfg):
        return context
    shared = {k: v for k, v in vars(context).items() if k not in ("_doc_tools", "_graph_tools", "_agent_tools")}
    return SimpleNamespace(**shared, for_model=True)


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
    code = [t for t in d.tools if t in CODE_TOOLS]
    if code and (context.cfg["sources"].get("code") or {}).get("model") == "none":
        raise ConfigError(f"agent {d.name}: its tools {code} send the code to the model, and [sources.code] "
                          f"model = \"none\" keeps it in the workstation")
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


def stall_guard(functions: list, limit: int, state: dict) -> list:
    """The tools of one agent, wrapped to count the calls since the code last moved: a change
    after which the file compiles, or has fewer errors than at its last check (a change with no
    compiler counts), or a test that passed. At `limit` such calls, state["stalled"] is set,
    and ask() ends the run after that turn. In the tanda 4 of atlas develop, the 6 runs that
    spent their 40 calls took 41 % of the time, most of it in loops of reading and editing
    badly; a run of the server rewrote its file 17 times with the same 2 errors. A fresh start
    with what is done goes further than a long conversation."""
    since, errors = {"calls": 0}, {}            # errors: file -> errors at its last check

    def count(check: dict) -> int:
        return 0 if check.get("ok") else max(1, len(check.get("errors", [])) + check.get("more_errors", 0))

    def moved(name: str, result: Any) -> bool:
        if not isinstance(result, dict) or "error" in result:
            return False
        if name == "run_test":
            return bool(result.get("passed"))
        if name == "check_code" and "ok" in result:
            errors[result.get("file", "")] = count(result)
            return False
        if name not in CHANGES or result.get("unchanged"):
            return False
        check = result.get("check")
        if not isinstance(check, dict):
            return True
        before, now = errors.get(result.get("file", "")), count(check)
        errors[result.get("file", "")] = now
        return before is None or now == 0 or now < before

    def wrap(f):
        @functools.wraps(f)
        def guarded(*args, **kwargs):
            result = f(*args, **kwargs)
            since["calls"] = 0 if moved(f.__name__, result) else since["calls"] + 1
            if since["calls"] >= limit:
                state["stalled"] = True
            return result
        return guarded
    return [wrap(f) for f in functions]


def _to_record(record: list):
    """A sink of plugins.instrumented that keeps each call in `record`."""
    def sink(event: dict) -> None:
        record.append({"name": event["name"], "args": event["args"], "result": event["result"],
                       "seconds": round(event["seconds"], 2), **({"error": event["error"]} if event["error"] else {})})
    return sink


def recorded(functions: list, record: list) -> list:
    """The tools, wrapped so that each call goes to `record` with its arguments, its result
    and its seconds, to study afterwards what an agent did."""
    return [plugins.instrumented(f, _to_record(record)) for f in functions]


def tool_functions(d: AgentDef, cfg: Config, context: Any, record: list | None = None) -> list:
    """The functions of the tools of an agent (agent_tools), each call of them in the usage
    log of the workspace (origin "agent", with the name of the agent; see atlas.usage) and,
    if `record` is a list, in it."""
    from atlas.usage import usage_sink
    sinks = [_to_record(record)] if record is not None else []
    log = usage_sink(cfg, "agent", d.name)
    if log is not None:
        sinks.append(log)
    functions = agent_tools(d, context)
    if not sinks:
        return functions

    def sink(event: dict) -> None:
        for s in sinks:
            s(event)
    return [plugins.instrumented(f, sink) for f in functions]


def build_agent(name: str, cfg: Config, context: Any = None, client: Any = None, record: list | None = None,
                state: dict | None = None, keep_tool_results: int | None = None):
    """The ADK LlmAgent of an agent file. `client` replaces the OpenAI client, for tests;
    `record`, a list, gets every tool call with its result (see tool_functions); `state`, a dict,
    gets "stalled" when the agent has stall_steps (see stall_guard); `keep_tool_results`
    replaces that of the agent."""
    try:
        from google.adk.agents import LlmAgent
    except ImportError as e:
        raise ImportError('the agents need ADK: pip install -e ".[agents]"') from e
    from atlas.agents.model import AtlasLlm
    d = definition(name, cfg)
    text = d.instruction
    tools = tool_functions(d, cfg, model_context(context or context_for(cfg), cfg), record)
    model = AtlasLlm.from_service(agent_service(d, cfg), client, top_p=d.top_p, top_k=d.top_k,
                                  keep_tool_results=d.keep_tool_results if keep_tool_results is None
                                  else keep_tool_results)
    guarded = loop_guard(tools)
    if d.stall_steps and state is not None and set(d.tools) & {*CHANGES, "run_test"}:
        guarded = stall_guard(guarded, d.stall_steps, state)    # one that only reads would always look stuck
    return LlmAgent(name=d.name, description=d.description, model=model, tools=guarded,
                    instruction=lambda _: text)   # a function: ADK does not fill {placeholders} in it


async def ask(agent, question: str, max_steps: int = 8, state: dict | None = None) -> dict:
    """One question to an ADK agent, in a session of its own: {"answer", "calls": [{"name",
    "args"}], "sources", "stopped", "tokens": {"prompt", "completion"}, and "cost" when the
    server says it (OpenRouter)}. `stopped` says that it reached max_steps calls to the model
    before answering, or that `state` (of stall_guard) says it stalled: then "stalled" is true
    too. `sources`: the citations of its tool results (see sources)."""
    from google.adk.runners import InMemoryRunner
    runner = InMemoryRunner(agent=agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    return await _turn(runner, session.id, question, max_steps, state)


async def _turn(runner, session_id: str, question: str, max_steps: int, state: dict | None) -> dict:
    """A question in a session of `runner` (see ask)."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError
    from google.adk.agents.run_config import RunConfig
    from google.genai import types
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls, results, answer, stopped = [], [], "", False
    tokens, cost = {"prompt": 0, "completion": 0}, None
    events = runner.run_async(user_id=USER, session_id=session_id, new_message=message,
                              run_config=RunConfig(max_llm_calls=max_steps))
    try:
        async for event in events:
            if event.usage_metadata:
                tokens["prompt"] += event.usage_metadata.prompt_token_count or 0
                tokens["completion"] += event.usage_metadata.candidates_token_count or 0
            if (event.custom_metadata or {}).get("cost") is not None:
                cost = (cost or 0.0) + float(event.custom_metadata["cost"])
            for part in (event.content.parts if event.content else None) or []:
                if part.function_call:
                    calls.append({"name": part.function_call.name, "args": dict(part.function_call.args or {})})
                elif part.function_response:
                    results.append({"name": part.function_response.name, "result": part.function_response.response})
                elif part.text and not part.thought and event.is_final_response():
                    answer += part.text
            if state is not None and state.get("stalled") and event.content and \
                    any(p.function_response for p in event.content.parts or []):
                stopped = True                    # no progress in stall_steps calls: the run ends here
                break
    except LlmCallsLimitExceededError:        # max_steps reached
        stopped = True
    finally:                                  # closed here, in this task: closed later by asyncio, from
        await events.aclose()                 # another context, OpenTelemetry logs errors for each span
    return {"answer": answer.strip(), "calls": calls, "sources": sources(results), "stopped": stopped,
            "tokens": tokens, **({"stalled": True} if state is not None and state.get("stalled") else {}),
            **({"cost": round(cost, 6)} if cost is not None else {})}


def sources(results: list[dict]) -> list[dict]:
    """The citations in the results of some tool calls ([{"name", "result"}]), without repeats:
    [{"cite", "file"}] ("file" can be empty, or "path:line" for code). From search, its
    passages; from ask_docs, its sources; from get_section, the section; from trace, the
    requirement, the sections that state it and the code that implements it."""
    found: dict[str, dict] = {}

    def add(cite: Any, file: Any = "") -> None:
        if cite and str(cite) not in found:
            found[str(cite)] = {"cite": str(cite), "file": str(file or "")}
        elif cite and file and not found[str(cite)]["file"]:
            found[str(cite)]["file"] = str(file)

    section = lambda d: f"{d.get('document')} › {d.get('title') or d.get('section')}"
    for call in results:
        res = call.get("result")
        if not isinstance(res, dict) or "error" in res:
            continue
        if call.get("name") == "search":
            for hit in res.get("results") or []:
                add(hit.get("cite"))
        elif call.get("name") == "ask_docs":
            for s in res.get("sources") or []:
                add(s.get("cite"), s.get("file"))
        elif call.get("name") == "get_section" and res.get("document"):
            add(section(res), res.get("file"))
        elif call.get("name") == "trace" and res.get("id"):
            add(f"DOORS {res['module']} › {res['id']}" if res.get("module") else res["id"])
            for d in res.get("documents") or []:
                add(section(d))
            for c in (res.get("implemented_by") or {}).get("items") or []:
                line = re.match(r"\d+", str(c.get("lines") or c.get("line") or ""))
                add(c.get("id"), f"{c['file']}:{line.group()}" if c.get("file") and line else c.get("file"))
    return list(found.values())


class Conversation:
    """A conversation with an agent: the agent, its runner and its ADK session are made once,
    and each ask() is a turn of that session, which sees the earlier ones (see the module).
    Nothing of the conversation is kept outside the session: history() reads it from there.
    `record` and `client` as in build_agent; `keep_tool_results` replaces that of the agent
    (atlas chat: KEEP_IN_CHAT if the agent has none)."""

    def __init__(self, name: str, cfg: Config, context: Any = None, client: Any = None,
                 record: list | None = None, keep_tool_results: int | None = None):
        self.name, self.cfg, self.definition = name, cfg, definition(name, cfg)
        self.context = context or context_for(cfg)
        self.state: dict = {}                    # of stall_guard
        self.agent = build_agent(name, cfg, self.context, client, record, self.state, keep_tool_results)
        self.runner: Any = None                  # made with the session, in the first turn
        self.session_id = ""

    async def ask_async(self, question: str) -> dict:
        """A turn of the conversation (see ask)."""
        if self.runner is None:
            from google.adk.runners import InMemoryRunner
            self.runner = InMemoryRunner(agent=self.agent, app_name=APP)
            self.session_id = (await self.runner.session_service.create_session(app_name=APP, user_id=USER)).id
        self.state.pop("stalled", None)          # each turn has its own stall_steps
        from atlas.outbound import record_sent
        record_sent(self.cfg.workspace / "atlas.db", agent_service(self.definition, self.cfg).model,
                    {f"agent:{self.name}": 1}, "runs")
        return await _turn(self.runner, self.session_id, question, self.definition.max_steps, self.state)

    def ask(self, question: str) -> dict:
        """A turn of the conversation: the result of ask(), for one more question of the same
        session."""
        return asyncio.run(self.ask_async(question))

    def history(self) -> list[dict]:
        """The turns so far, from the events of the session: [{"question", "answer", "calls",
        "sources"}]."""
        if self.runner is None:
            return []
        session = asyncio.run(self.runner.session_service.get_session(app_name=APP, user_id=USER,
                                                                      session_id=self.session_id))
        turns: list[dict] = []
        for event in session.events if session else []:
            parts = (event.content.parts if event.content else None) or []
            if event.author == "user" and any(p.text for p in parts):
                turns.append({"question": "".join(p.text or "" for p in parts), "answer": "", "calls": [],
                              "results": []})
                continue
            if not turns:
                continue
            turn = turns[-1]
            for part in parts:
                if part.function_call:
                    turn["calls"].append({"name": part.function_call.name, "args": dict(part.function_call.args or {})})
                elif part.function_response:
                    turn["results"].append({"name": part.function_response.name,
                                            "result": part.function_response.response})
                elif part.text and not part.thought and event.is_final_response():
                    turn["answer"] += part.text
        return [{"question": t["question"], "answer": t["answer"].strip(), "calls": t["calls"],
                 "sources": sources(t["results"])} for t in turns]

    def markdown(self) -> str:
        """The conversation so far, with the citations of each answer: for a report."""
        out = [f"# atlas chat with {self.name}", "", f"Written {time.strftime('%Y-%m-%d %H:%M:%S')}.", ""]
        for i, t in enumerate(self.history(), 1):
            out += [f"## {i}", "", *[f"> {line}" for line in t["question"].splitlines()], "",
                    t["answer"] or "(no answer)", ""]
            if t["sources"]:
                out += ["Sources:", ""] + [f"- {s['cite']}" + (f" (`{s['file']}`)" if s["file"] else "")
                                           for s in t["sources"]] + [""]
            if t["calls"]:
                out += ["Tools: " + ", ".join(f"`{c['name']}({json.dumps(c['args'], ensure_ascii=False)})`"
                                              for c in t["calls"]), ""]
        return "\n".join(out)


def run_agent(name: str, question: str, cfg: Config, context: Any = None, client: Any = None,
              record: list | None = None) -> dict:
    """Builds the agent `name` and asks it `question`: a conversation of one turn (see ask);
    `record` gets its tool calls with their results."""
    return Conversation(name, cfg, context, client, record).ask(question)
