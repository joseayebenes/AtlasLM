"""MCP server: the tools of atlas for OpenCode, VS Code or any other MCP client.

A single server with the tools of the two old ones (`doc_rag.py mcp` and
`ada_graph_mcp.py`). They are the "tool" plugins of atlas.index (DocTools), atlas.graph
(GraphTools) and atlas.code (CodeTools), with the same names ADK agents use.

    atlas serve atlas.toml        stdio, the transport of local MCP clients

[mcp] tools chooses the tools. When it is empty, the server offers those of the configured
sources: the document tools with [[sources.docs]] (with only [sources.doors],
get_requirement, and search when "index" is a step), the graph tools and read_file with
[sources.code] (and check_code when it has a check_command), and without code the
traceability tools of the graph (trace, matrix, gaps...) when "graph" is a step;
build_context with the graph. ask_atlas, verify_requirement, link_requirement and
check_compliance (which need the model, and ask_atlas also ADK) are only offered when listed.
So are the tools that write (edit_file, write_file, replace_subprogram, add_subprogram: atlas
provides knowledge and the editor edits) and run_test, which runs code.
[plugins] disabled removes any tool, and [mcp] enabled = false turns the server off.

The server answers from the workspace that `atlas run` fills. A run while the server is up
is seen at once: the graph and the vectors are read again after it.

    from atlas.mcp import create_server
    server = create_server(cfg)        # an MCPServer (mcp 2.x) or a FastMCP (mcp 1.x)
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from atlas import plugins
from atlas.config import Config, ConfigError
from atlas.store import Store

__all__ = ["create_server", "default_tools", "instructions", "serve", "tool_names"]

log = logging.getLogger(__name__)

WRITING_TOOLS = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")

INSTRUCTIONS = {
    "": "Engineering knowledge base of the project: documents, requirements and code. Answer from "
        "what the tools return, and say where it comes from.",
    "docs": "Documents (standards, specifications, manuals): `search` finds passages and gives their "
            "citation; `get_section` reads a whole section; `get_requirement` gives the exact text of a "
            "requirement ID, from DOORS and from the documents; `ask_docs` writes an answer with citations. "
            "Cite document and section.",
    "graph": "Graph of code, requirements and documents: code identifiers are `unit.name` in lower case. "
             "Usual flow: find_symbol -> get_node -> callers / callees / impact -> read_source. `trace` gives "
             "everything about a requirement (DOORS, documents, code, tests); `trace_code`, the requirements "
             "and tests of a code element; `impact`, what a change affects; `matrix` and `gaps`, the "
             "traceability of a DOORS module and what it lacks.",
    "context": "`build_context` gives in one call everything about a requirement, a code element or a unit "
               "(requirements, signatures, callers, tests, documents and source): use it before changing code.",
    "agents": "`ask_atlas` asks the assistant of atlas, which searches by itself and answers with citations "
              "(slower: use it for questions that need several searches); `verify_requirement` checks with the "
              "model whether a requirement and the documents agree; `link_requirement` proposes the code that "
              "may implement a requirement, without changing anything; `check_compliance` judges whether the code "
              "of a requirement does all it asks, obligation by obligation.",
    "files": "Code files: paths are relative to the folder of atlas.toml (the project), and only the code roots "
             "can be read.",
    "edit": "edit_file, write_file, replace_subprogram and add_subprogram only change a file after a read_file of it; "
            "check the diff they return. replace_subprogram takes a whole subprogram and needs nothing copied.",
    "build": "`check_code` compiles a file with the compiler of the project and returns its errors: use it after "
             "every change; `run_test` builds a test procedure and runs it; `library_spec` gives the real "
             "declarations of a package of the runtime or of the project.",
}


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
        except ImportError as e:            # networkx, of the [code] extra
            log.warning("no graph tools: %s", e)
    if code:
        names.append("read_file")
        if sources["code"].get("check_command"):          # a compiler: the errors the model cannot see
            names.append("check_code")
    return names


def tool_names(cfg: Config) -> list[str]:
    """The tools the server offers: [mcp] tools, or default_tools(cfg) if it is empty.
    The plugins must be loaded (atlas.plugins.load_plugins)."""
    available, disabled = set(plugins.available("tool")), set(cfg["plugins"]["disabled"])
    wanted = list(dict.fromkeys(cfg["mcp"]["tools"]))
    if not wanted:
        return [n for n in default_tools(cfg) if n in available and f"tool:{n}" not in disabled]
    unknown = [n for n in wanted if n not in available]
    if unknown:
        raise ConfigError(f"[mcp] tools: unknown tools {unknown} (available: {', '.join(sorted(available))})")
    off = [n for n in wanted if f"tool:{n}" in disabled]
    if off:
        raise ConfigError(f"[mcp] tools: {off} are disabled in [plugins] disabled")
    return wanted


def instructions(names: list[str]) -> str:
    """What the server tells the client about its tools."""
    from atlas.code.tools import TOOL_NAMES as FILE_TOOLS
    from atlas.index.tools import TOOL_NAMES as DOC_TOOLS
    groups = {"docs": DOC_TOOLS, "files": FILE_TOOLS, "edit": WRITING_TOOLS, "build": ("check_code", "run_test", "library_spec"),
              "context": ("build_context",),
              "agents": ("ask_atlas", "verify_requirement", "link_requirement", "check_compliance")}
    try:
        from atlas.graph.tools import TOOL_NAMES as GRAPH_TOOLS
        groups["graph"] = GRAPH_TOOLS
    except ImportError:
        pass
    parts = [INSTRUCTIONS[""]] + [INSTRUCTIONS[g] for g in ("docs", "graph", "context", "agents", "files", "edit")
                                  if g in groups and set(groups[g]) & set(names)]
    return "\n\n".join(parts)


def create_server(cfg: Config, context: Any = None) -> Any:
    """The MCP server with the tools of tool_names(cfg). `context` is what the tool plugins
    receive (cfg, store); by default, a new one with the plugins of the configuration."""
    if not cfg["mcp"]["enabled"]:
        raise ConfigError("the MCP server is turned off in this configuration ([mcp] enabled = false)")
    server_class = _server_class()
    from mcp.types import ToolAnnotations
    if context is None:
        plugins.load_plugins(cfg["plugins"]["modules"])
        context = SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])
    names = tool_names(cfg)
    server = server_class("atlas", instructions=instructions(names))
    for name in names:
        tool = plugins.get("tool", name, cfg["plugins"]["disabled"])(context)
        server.add_tool(tool.function(), name=name, description=tool.description or None,
                        annotations=ToolAnnotations(readOnlyHint=not tool.writes, destructiveHint=tool.writes))
    log.info("MCP server over %s with %d tools: %s", cfg.workspace, len(names), ", ".join(names))
    return server


def serve(cfg: Config, context: Any = None) -> None:
    """Serves over stdio until the client closes the connection. stdout is the protocol:
    logs go to stderr."""
    server = create_server(cfg, context)
    store = getattr(context, "store", None) or Store(cfg.workspace)
    counts = store.counts()
    if not any(counts.values()):
        log.warning("nothing processed yet in %s: run `atlas run %s` (it can run while the server is up)",
                    cfg.workspace, cfg.path.name)
    else:
        log.info("data: %s", counts)
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
