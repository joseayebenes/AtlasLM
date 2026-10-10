"""Tools of atlas.agents for agents and the MCP server.

    build_context    everything about a requirement, a code element or a unit, in one
                     Markdown package within a budget of tokens, without a model

They are registered as "tool" plugins with these names, for agent files and [mcp] tools.
"""
from __future__ import annotations

from typing import Any

from atlas.agents.context import build_context
from atlas.graph.tools import NO_GRAPH, GraphTools
from atlas.plugins import register_methods

__all__ = ["AgentTools", "TOOL_NAMES"]

TOOL_NAMES = ("build_context",)


class AgentTools:
    def __init__(self, graph: GraphTools):
        self.graph = graph

    @classmethod
    def for_context(cls, context: Any) -> "AgentTools":
        """One instance per context, over the graph tools of that context."""
        tools = getattr(context, "_agent_tools", None)
        if tools is None:
            tools = cls(GraphTools.for_context(context))
            setattr(context, "_agent_tools", tools)
        return tools

    def build_context(self, target: str, budget_tokens: int = 4000) -> dict:
        """Everything about a requirement ID, a code element or a unit (package) in one
        Markdown package of at most `budget_tokens`: requirements with their text,
        signatures and comments, callers and callees, tests, document sections and source.
        Call it before changing code, instead of many searches."""
        g = self.graph.graph()
        return build_context(g, target, budget_tokens) if g is not None else {"error": NO_GRAPH}


register_methods(AgentTools, TOOL_NAMES)
