"""MCP server: the tools of atlas for OpenCode, VS Code or any other MCP client.

A single server with the tools of the two old ones (`doc_rag.py mcp` and
`ada_graph_mcp.py`). They are the "tool" plugins of atlas.index (DocTools), atlas.graph
(GraphTools) and atlas.code (CodeTools), with the same names ADK agents use.

    atlas serve atlas.toml        stdio, the transport of local MCP clients

[mcp] tools chooses the tools. When it is empty, [mcp] profile does:
    editor   what the agent of an editor needs to know before changing code (search,
             get_requirement, get_section, list_documents, trace, trace_code, impact,
             build_context, context_at, similar_requirements, find_symbol, read_source,
             read_file); the default with [sources.code]
    trace    the traceability of DOORS and the documents (trace, matrix, gaps,
             list_requirements, neighbors, get_requirement, search)
    all      the tools of the configured sources; the default without code
The tools of the configured sources (default_tools) are the document tools with
[[sources.docs]] (with only [sources.doors], get_requirement, and search when "index" is a
step), the graph tools and read_file with [sources.code], and without code the traceability tools of the graph (trace, matrix, gaps...)
when "graph" is a step; build_context with the graph, and context_at and
similar_requirements with the graph of the code. A profile only keeps some of them.
ask_atlas, verify_requirement, link_requirement and check_compliance (which need the model,
and ask_atlas also ADK) are only offered when listed. atlas only reads the code: a tool that
writes files or runs commands of the project (edit_file, check_code, run_test, judge_changes...:
hephesto's) is never offered, even when listed; `hephesto serve` offers them, over the same
create_server. [plugins] disabled removes any tool, and [mcp] enabled = false turns the server off.
Each call goes to the usage log of the workspace ([mcp] usage_log; see atlas.usage).

Every tool has a title ("Trace code") and annotations: those that write are destructive, those
that only run commands of the project are not read-only; the others are read-only, and those
that do not ask the model are also idempotent and closed-world (they only read the workspace
and the code).

The server answers from the workspace that `atlas run` fills. A run while the server is up
is seen at once: the graph and the vectors are read again after it.

    from atlas.mcp import create_server
    server = create_server(cfg)        # an MCPServer (mcp 2.x) or a FastMCP (mcp 1.x)
    create_server(cfg, names=[...], title="hephesto", extra=[(tools, paragraph)])   # another server
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from atlas import plugins
from atlas.config import Config, ConfigError
from atlas.store import Store

__all__ = ["create_server", "default_tools", "graph_instructions", "instructions", "profile", "profile_tools", "serve",
           "tool_groups", "tool_names", "traceability"]

log = logging.getLogger(__name__)

MODEL_TOOLS = ("ask_docs", "ask_atlas", "verify_requirement", "link_requirement", "check_compliance")
EDITOR_TOOLS = ("search", "get_requirement", "get_section", "list_documents", "trace", "trace_code", "impact",
                "build_context", "context_at", "similar_requirements", "find_symbol", "read_source", "read_file")

INSTRUCTIONS = {
    "": "Engineering knowledge base of the project: documents, requirements and code. Answer from "
        "what the tools return, and say where it comes from.",
    "docs": "Documents (standards, specifications, manuals): `search` finds passages and gives their "
            "citation; narrow it, rather than searching again with other words, with `document`, `module` (a "
            "DOORS module), `chapter` (its number or title), `kind` (e.g. requirement) and `attribute` "
            "(\"status=Approved;verification=Test\": the DOORS attributes each result shows in its `meta`); "
            "`get_section` reads a whole section; `get_requirement` gives the exact text of a "
            "requirement ID, from DOORS and from the documents; `ask_docs` writes an answer with citations. "
            "Cite document and section.",
    "graph": "Graph of code, requirements and documents: code identifiers are `unit.name` in lower case.",
    "context": "`build_context` gives in one call everything about a requirement, a code element or a unit "
               "(requirements, signatures, callers, tests, documents and source): use it before changing code. "
               "`context_at` gives the same for a file and a line, those the editor has open; the tools that "
               "take a code element also take \"file:line\". `similar_requirements` gives requirements like "
               "one that already have code, with that code and its `Req:` tag: follow them.",
    "agents": "`ask_atlas` asks the assistant of atlas, which searches by itself and answers with citations "
              "(slower: use it for questions that need several searches); `verify_requirement` checks with the "
              "model whether a requirement and the documents agree; `link_requirement` proposes the code that "
              "may implement a requirement, without changing anything; `check_compliance` judges whether the code "
              "of a requirement does all it asks, obligation by obligation.",
    "files": "Code files: paths are relative to the folder of atlas.toml (the project), and only the code roots "
             "can be read.",
    "library": "`library_spec` gives the real declarations of a package of the runtime or of the project: look "
               "there before using a subprogram; `search_reference` searches the manuals of the compiler and the "
               "declarations of the library by words.",
    "lsp": "`find_definition`, `find_references` and `describe_symbol` ask the language server about a name written "
           "in a line of a file: where it is declared, where it is used, and what it is (a profile, a type).",
}
GROUP_ORDER = ("docs", "graph", "context", "agents", "files", "library", "lsp")
GRAPH_FLOW = (("find_symbol",), ("get_node",), ("callers", "callees", "trace_code", "impact"), ("read_source",))
GRAPH_CLAUSES = (                           # the first one whose tools are all offered and not said yet
    (("trace",), "everything about a requirement (DOORS, documents, code, tests)"),
    (("trace_code",), "the requirements and tests of a code element"),
    (("impact",), "what a change affects"),
    (("matrix", "gaps"), "the traceability of a DOORS module and what it lacks"),
    (("matrix",), "the traceability of a DOORS module"),
    (("gaps",), "what a DOORS module lacks"),
)


def _server_class() -> type:
    try:
        from mcp.server.mcpserver import MCPServer          # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as MCPServer   # mcp 1.x
        except ImportError as e:
            raise ImportError('the MCP server needs the mcp package: pip install -e ".[mcp]"') from e
    return MCPServer


def _enabled(source: dict | None) -> bool:
    return bool(source) and source.get("enabled", True)


def default_tools(cfg: Config) -> list[str]:
    """The tools of the configured sources: what an empty [mcp] tools means."""
    from atlas.index.tools import TOOL_NAMES as DOC_TOOLS
    sources = cfg["sources"]
    names: list[str] = []
    if any(_enabled(d) for d in sources.get("docs", [])):
        names += DOC_TOOLS
    elif _enabled(sources.get("doors")):     # its requirements are in the index too
        names += ["search", "get_requirement"] if "index" in cfg["pipeline"]["steps"] else ["get_requirement"]
    code = _enabled(sources.get("code"))
    if code or ("graph" in cfg["pipeline"]["steps"] and (_enabled(sources.get("doors")) or
                                                          any(_enabled(d) for d in sources.get("docs", [])))):
        try:
            from atlas.graph.tools import TOOL_NAMES as GRAPH_TOOLS, TRACE_TOOLS
            names += [n for n in (GRAPH_TOOLS if code else TRACE_TOOLS) if n not in names] + ["build_context"]
            if code:
                names += ["context_at", "similar_requirements"]
        except ImportError as e:            # networkx, of the [code] extra
            log.warning("no graph tools: %s", e)
    if code:
        names.append("read_file")
    return names


def profile(cfg: Config) -> str:
    """[mcp] profile, or the one of the sources: editor with code, all without."""
    return cfg["mcp"]["profile"] or ("editor" if _enabled(cfg["sources"].get("code")) else "all")


def profile_tools(cfg: Config) -> list[str]:
    """The tools of the configured sources that the profile keeps: what an empty [mcp]
    tools means."""
    names, name = default_tools(cfg), profile(cfg)
    if name == "all":
        return names
    if name == "editor":
        wanted = EDITOR_TOOLS
    else:
        try:
            from atlas.graph.tools import TRACE_TOOLS
        except ImportError:                 # networkx: default_tools has said it
            TRACE_TOOLS = ()
        wanted = (*TRACE_TOOLS, "get_requirement", "search")
    return [n for n in names if n in wanted]


def tool_names(cfg: Config) -> list[str]:
    """The tools the server offers: [mcp] tools, or profile_tools(cfg) if it is empty.
    The plugins must be loaded (atlas.plugins.load_plugins)."""
    available = {n for n in plugins.available("tool") if not _acts(n)}
    disabled = set(cfg["plugins"]["disabled"])
    wanted = list(dict.fromkeys(cfg["mcp"]["tools"]))
    if not wanted:
        return [n for n in profile_tools(cfg) if n in available and f"tool:{n}" not in disabled]
    acting = [n for n in wanted if n in plugins.available("tool") and _acts(n)]
    if acting:
        raise ConfigError(f"[mcp] tools: {acting} write or run the code, and atlas only reads it: `hephesto serve` "
                          "offers them")
    unknown = [n for n in wanted if n not in available]
    if unknown:
        raise ConfigError(f"[mcp] tools: unknown tools {unknown} (available: {', '.join(sorted(available))})")
    off = [n for n in wanted if f"tool:{n}" in disabled]
    if off:
        raise ConfigError(f"[mcp] tools: {off} are disabled in [plugins] disabled")
    return wanted


def _acts(name: str) -> bool:
    """Whether a tool writes files or runs commands of the project: atlas never offers it."""
    tool = plugins.get("tool", name)
    return bool(tool.writes or tool.runs)


def tool_groups() -> dict[str, tuple[str, ...]]:
    """Group of INSTRUCTIONS -> its tools: the paragraph of a group goes in the instructions
    when the server offers any of them."""
    from atlas.code.lsp import LSP_TOOL_NAMES
    from atlas.code.files import READ_TOOL_NAMES
    from atlas.code.toolchain import TOOLCHAIN_TOOL_NAMES
    from atlas.index.tools import TOOL_NAMES as DOC_TOOLS
    groups = {"docs": DOC_TOOLS, "files": READ_TOOL_NAMES, "library": TOOLCHAIN_TOOL_NAMES, "lsp": LSP_TOOL_NAMES}
    try:                                    # networkx, of the [code] extra
        from atlas.agents.tools import TOOL_NAMES as AGENT_TOOLS
        from atlas.graph.tools import TOOL_NAMES as GRAPH_TOOLS
    except ImportError:
        return groups
    return {**groups, "graph": GRAPH_TOOLS, "context": tuple(n for n in AGENT_TOOLS if n not in MODEL_TOOLS),
            "agents": tuple(n for n in AGENT_TOOLS if n in MODEL_TOOLS)}


def instructions(names: list[str], cfg: Config | None = None, store: Store | None = None,
                 extra: list[tuple[tuple[str, ...], str]] = ()) -> str:
    """What the server tells the client about its tools and, with `cfg`, about the
    traceability of the project (traceability). `extra`: the paragraphs of other tools
    (those of hephesto), each with its tools, after those of atlas."""
    groups = tool_groups()
    parts = [INSTRUCTIONS[""]] + [graph_instructions(names) if g == "graph" else INSTRUCTIONS[g]
                                  for g in GROUP_ORDER if g in groups and set(groups[g]) & set(names)]
    parts += [text for tools, text in extra if set(tools) & set(names)]
    if cfg is not None:
        parts.append(traceability(cfg, names, store))
    return "\n\n".join(p for p in parts if p)


def graph_instructions(names: list[str]) -> str:
    """The paragraph of the graph, naming only the tools offered: the usual flow, when two of
    its steps are offered, and what trace, trace_code, impact, matrix and gaps give."""
    offered = set(names)
    steps = [s for s in (" / ".join(t for t in step if t in offered) for step in GRAPH_FLOW) if s]
    said: set[str] = set()
    clauses: list[str] = []
    for tools, what in GRAPH_CLAUSES:
        if offered.issuperset(tools) and not said.intersection(tools):
            verb = ", " if clauses else " give " if len(tools) > 1 else " gives "     # "`a` gives x; `b`, y"
            clauses.append(" and ".join(f"`{t}`" for t in tools) + verb + what)
            said.update(tools)
    text = INSTRUCTIONS["graph"]
    if len(steps) > 1:
        text += " Usual flow: " + " -> ".join(steps) + "."
    return text + (" " + "; ".join(clauses) + "." if clauses else "")


def traceability(cfg: Config, names: list[str], store: Store | None = None) -> str:
    """The paragraph "Traceability of this project": how the code cites the requirements
    ([sources.code] req_tag or req_regex), what their IDs look like ([sources.doors.convention]
    id_pattern), the DOORS modules read (`store`), and the steps of a change with the tools
    offered. "" without code or DOORS."""
    sources, said = cfg["sources"], []
    code = sources.get("code") if _enabled(sources.get("code")) else None
    doors = sources.get("doors") if _enabled(sources.get("doors")) else None
    try:
        modules = store.modules() if store is not None and doors else []
        example = (store.requirement_ids("", 1) if modules else []) or ["<ID>"]
    except Exception:                       # a workspace that atlas run has not filled
        modules, example = [], ["<ID>"]
    if code:
        if code.get("req_regex"):
            said.append(f"The code cites the requirements it implements in comments that match `{code['req_regex']}`, "
                        "and a test the requirements it verifies.")
        else:
            tag = (code.get("req_tag") or "Req:").strip()
            mark = "#" if (code.get("languages") or ["ada"])[0] == "python" else "--"
            said.append(f"The code cites the requirements it implements in a comment with the tag `{tag}` "
                        f"(`{mark} {tag} {example[0]}`) by the subprogram or type, or by its declaration in a spec; a "
                        "test cites those it verifies the same way. Add the tag to the code that implements a "
                        "requirement.")
    if doors:
        pattern = (doors.get("convention") or {}).get("id_pattern")
        if pattern:
            said.append(f"Requirement IDs match `{pattern}`.")
        if modules:
            said.append(f"DOORS modules: {', '.join(modules)}.")
    if code:
        offered = set(names)
        context = [f"`{n}`" for n in ("context_at", "build_context") if n in offered] or \
            (["`trace_code`"] if "trace_code" in offered else [])
        steps = [" or ".join(context) + " of the code to change" if context else "read the code to change", "edit it",
                 "`check_code` the file until it has no errors" if "check_code" in offered else "compile it",
                 ("`run_test` " if "run_test" in offered else "run ") +
                 ("the tests that `trace` gives for its requirements" if "trace" in offered else "its tests")]
        said.append("A change: " + " -> ".join(steps) + ".")
    return "Traceability of this project: " + " ".join(said) if said else ""


def create_server(cfg: Config, context: Any = None, names: list[str] | None = None, title: str = "atlas",
                  extra: list[tuple[tuple[str, ...], str]] = ()) -> Any:
    """The MCP server with the tools of tool_names(cfg), or `names` (hephesto serve: its own,
    with the paragraphs `extra` of instructions). `context` is what the tool plugins receive
    (cfg, store); by default, a new one with the plugins of the configuration."""
    if not cfg["mcp"]["enabled"]:
        raise ConfigError("the MCP server is turned off in this configuration ([mcp] enabled = false)")
    server_class = _server_class()
    from mcp.types import ToolAnnotations
    if context is None:
        plugins.load_plugins(cfg["plugins"]["modules"])
        context = SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])
    from atlas.usage import usage_sink
    names = tool_names(cfg) if names is None else names
    server = server_class(title, instructions=instructions(names, cfg, getattr(context, "store", None), extra))
    usage_log = usage_sink(cfg, "mcp")          # each call, a row of the usage log ([mcp] usage_log)
    for name in names:
        tool = plugins.get("tool", name, cfg["plugins"]["disabled"])(context)
        function = tool.function()
        if usage_log is not None:
            function = plugins.instrumented(function, usage_log, name=name)
        server.add_tool(function, name=name, title=name.replace("_", " ").capitalize(),
                        description=tool.description or None, annotations=_annotations(ToolAnnotations, name, tool))
    log.info("MCP server over %s with %d tools (%s): %s", cfg.workspace, len(names),
             f"those of {title}" if title != "atlas" else f"profile {profile(cfg)}" if not cfg["mcp"]["tools"]
             else "[mcp] tools", ", ".join(names))
    return server


def _annotations(annotations: type, name: str, tool: Any) -> Any:
    """What a tool does, for the client: one that writes is destructive; one that only runs
    the commands of the project (check_code) is not read-only (it leaves what it builds), but
    destroys nothing; one that only reads the workspace and the code, without the model, gives
    the same answer to the same call and reaches nothing outside."""
    if tool.writes:
        return annotations(readOnlyHint=False, destructiveHint=True)
    if tool.runs:
        return annotations(readOnlyHint=False, destructiveHint=False)
    if name in MODEL_TOOLS:
        return annotations(readOnlyHint=True, destructiveHint=False)
    return annotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def serve(cfg: Config, context: Any = None, **options: Any) -> None:
    """Serves over stdio until the client closes the connection. stdout is the protocol:
    logs go to stderr. `options`: the arguments of create_server (names, title, extra)."""
    server = create_server(cfg, context, **options)
    store = getattr(context, "store", None) or Store(cfg.workspace)
    counts = store.counts()
    if not any(counts.values()):
        log.warning("nothing processed yet in %s: run `atlas run %s` (it can run while the server is up)",
                    cfg.workspace, cfg.path.name)
    else:
        log.info("data: %s", counts)
    from atlas.doors import warn_stale_exports
    warn_stale_exports(cfg["sources"].get("doors"), store.modules_info())
    _log_connection(cfg)
    server.run("stdio")


def _log_connection(cfg: Config) -> None:
    """How the server reaches the model, and the variables of the configuration that the
    client did not pass: it starts atlas with its own environment, not that of a terminal."""
    from atlas.config import unset_hint
    from atlas.llm import effective_proxy, from_config, mask_credentials
    conn = from_config(cfg)
    for name, service in (("model", conn.llm), ("embeddings", conn.embeddings)):
        proxy = effective_proxy(service)
        log.info("%s: %s %s", name, mask_credentials(service.base_url),
                 f"through the proxy {mask_credentials(proxy)}" if proxy else "without proxy")
    if cfg.unset:
        log.warning(unset_hint(cfg))
