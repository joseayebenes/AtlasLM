"""Code graph tools for agents and the MCP server (the MCP of legacy/ada_graph_mcp.py).

They are the queries of KnowledgeGraph, with the names agents see:

    graph_stats        size of the graph and statistics of its last build
    find_symbol        elements by name                 get_node      everything about one
    search_code_docs   elements by their comment        read_source   its numbered source
    callers / callees / impact / call_chain             calls, direct and transitive
    unit_info / type_users / variable_users             what a unit defines, who uses a type or global
    trace              the code that cites a requirement
    list_requirements / untraced                        requirements cited in the code, code without any

`search_docs` and `requirement` of ada_graph_mcp.py are `search_code_docs` and `trace` here,
so as not to be confused with the document tools. Its `read_file` is not here: the one of
atlas.code.CodeTools only reads inside the code roots.

The graph is read from atlas.db and read again when `atlas run` rebuilds it: a long-lived
process (atlas serve) always answers with the last build. They are registered as "tool"
plugins with these names, for [agents.*] tools and [mcp] tools.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from atlas.graph.knowledge import MAX_LIST, KnowledgeGraph
from atlas.graph.store import GraphStore
from atlas.plugins import register_methods

__all__ = ["GraphTools", "TOOL_NAMES"]

TOOL_NAMES = ("graph_stats", "find_symbol", "search_code_docs", "get_node", "callers", "callees", "impact",
              "call_chain", "read_source", "unit_info", "type_users", "variable_users", "trace",
              "list_requirements", "untraced")

NO_GRAPH = "there is no graph yet: run `atlas run` with \"graph\" in [pipeline] steps"


class GraphTools:
    def __init__(self, db: str | Path, root: str | Path = "."):
        self.store = GraphStore(db)
        self.root = root
        self._graph: KnowledgeGraph | None = None
        self._builds: dict | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg) -> "GraphTools":
        """Over the atlas.db of the workspace; file paths from the current directory, as the
        code roots of atlas.toml."""
        return cls(cfg.workspace / "atlas.db")

    @classmethod
    def for_context(cls, context: Any) -> "GraphTools":
        """One instance per context, shared by every tool plugin."""
        tools = getattr(context, "_graph_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg)
            setattr(context, "_graph_tools", tools)
        return tools

    def graph(self) -> KnowledgeGraph | None:
        """The last build of the graph, or None if there is none yet."""
        builds = self.store.builds()
        with self._lock:
            if builds != self._builds:
                self._graph = KnowledgeGraph(self.store.load(), self.root, builds) if builds else None
                self._builds = builds
            return self._graph

    def _ask(self, query: Callable[[KnowledgeGraph], dict]) -> dict:
        g = self.graph()
        return query(g) if g is not None else {"error": NO_GRAPH}

    # --- tools ----------------------------------------------------------------
    def graph_stats(self) -> dict:
        """Size of the code graph by kind of element, and the statistics of its last build."""
        return self._ask(lambda g: g.stats())

    def find_symbol(self, query: str, kind: str = "", limit: int = 20) -> dict:
        """Code elements whose identifier contains `query` (any case). kind: subprogram |
        type | variable | constant | unit | requirement | external. Start here: the other
        tools take the identifier (tracks.update), or a simple name (Update) if only one
        element has it."""
        return self._ask(lambda g: g.find_symbol(query, kind, limit))

    def search_code_docs(self, text: str, limit: int = 20) -> dict:
        """Code elements whose header comment or declaration contains `text`: to find
        functionality by its description rather than by its name."""
        return self._ask(lambda g: g.search_docs(text, limit))

    def get_node(self, name: str) -> dict:
        """Everything about a code element: kind, file, lines, comment, requirements, the
        declaration of a type or variable, how many callers and callees, and its unit."""
        return self._ask(lambda g: g.node(name))

    def callers(self, name: str, limit: int = MAX_LIST) -> dict:
        """Subprograms that call this subprogram directly."""
        return self._ask(lambda g: g.callers(name, limit))

    def callees(self, name: str, limit: int = MAX_LIST) -> dict:
        """What this subprogram calls, which types it converts to and which globals it uses."""
        return self._ask(lambda g: g.callees(name, limit))

    def impact(self, name: str, depth: int = 3, limit: int = MAX_LIST) -> dict:
        """Every subprogram that calls `name`, directly or transitively, up to `depth`
        levels, nearest first: what to review before changing it."""
        return self._ask(lambda g: g.impact(name, depth, limit))

    def call_chain(self, source: str, target: str) -> dict:
        """The shortest chain of calls from one subprogram to another, if there is one."""
        return self._ask(lambda g: g.call_chain(source, target))

    def read_source(self, name: str, context: int = 0) -> dict:
        """Numbered source of a subprogram (with `context` more lines around it), or the
        declaration of a type or variable."""
        return self._ask(lambda g: g.read_source(name, context))

    def unit_info(self, name: str) -> dict:
        """What a unit (package or file) defines and which units it imports with `with`."""
        return self._ask(lambda g: g.unit_info(name))

    def type_users(self, name: str, limit: int = MAX_LIST) -> dict:
        """Subprograms that convert to this type, and its declaration."""
        return self._ask(lambda g: g.type_users(name, limit))

    def variable_users(self, name: str, limit: int = MAX_LIST) -> dict:
        """Subprograms that access this package-level variable or constant."""
        return self._ask(lambda g: g.variable_users(name, limit))

    def trace(self, req_id: str, limit: int = MAX_LIST) -> dict:
        """The code that cites a requirement ID in its comments: subprograms, types and tests."""
        return self._ask(lambda g: g.requirement(req_id, limit))

    def list_requirements(self, prefix: str = "", limit: int = 100) -> dict:
        """Requirement IDs cited in the code (optionally those starting with `prefix`), with
        how many elements cite each one."""
        return self._ask(lambda g: g.list_requirements(prefix, limit))

    def untraced(self, kind: str = "subprogram", limit: int = MAX_LIST) -> dict:
        """Code elements (subprogram | type) that cite no requirement."""
        return self._ask(lambda g: g.untraced(kind, limit))


register_methods(GraphTools, TOOL_NAMES)
