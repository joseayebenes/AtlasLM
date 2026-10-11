"""Tools of atlas.agents for agents and the MCP server.

    build_context        everything about a requirement, a code element or a unit, in one
                         Markdown package within a budget of tokens, without a model
    verify_requirement   whether the official text of a requirement and the documents agree,
                         judged by the agent requirement_verifier (needs the model)
    link_requirement     the code that may implement a requirement, proposed by the agent
                         req_linker (needs the model); it changes nothing
    ask_atlas            a question to the agent assistant, which searches with its own tools
                         (needs the model and ADK): one call for the client, several behind

They are registered as "tool" plugins with these names, for agent files and [mcp] tools.
"""
from __future__ import annotations

from typing import Any

from atlas.agents.context import build_context
from atlas.graph.tools import NO_GRAPH, GraphTools
from atlas.plugins import register_methods

__all__ = ["AgentTools", "TOOL_NAMES"]

TOOL_NAMES = ("build_context", "verify_requirement", "link_requirement", "ask_atlas")


class AgentTools:
    def __init__(self, graph: GraphTools, cfg=None, context: Any = None):
        self.graph = graph
        self.cfg = cfg
        self.context = context

    @classmethod
    def for_context(cls, context: Any) -> "AgentTools":
        """One instance per context, over the graph tools of that context."""
        tools = getattr(context, "_agent_tools", None)
        if tools is None:
            tools = cls(GraphTools.for_context(context), context.cfg, context)
            setattr(context, "_agent_tools", tools)
        return tools

    def build_context(self, target: str, budget_tokens: int = 4000) -> dict:
        """Everything about a requirement ID, a code element or a unit (package) in one
        Markdown package of at most `budget_tokens`: requirements with their text,
        signatures and comments, callers and callees, tests, document sections and source.
        Call it before changing code, instead of many searches."""
        g = self.graph.graph()
        return build_context(g, target, budget_tokens) if g is not None else {"error": NO_GRAPH}

    def verify_requirement(self, req_id: str) -> dict:
        """Whether the official text of a requirement (DOORS) and the documents say the same:
        verdict (consistent | inconsistent | no_documentation), the inconsistencies (value,
        condition, contradiction, scope, undefined) with the words of each text and their
        source, and the values that differ. Asks the model: slower than trace."""
        import openai
        from atlas.agents.verify import verify
        from atlas.index import Index
        from atlas.llm.connection import diagnose
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        try:
            return verify(req_id, self.cfg, graph=g, index=Index.from_config(self.cfg))
        except openai.APIError as e:
            return {"error": f"the model server failed: {diagnose(e)}"}

    def link_requirement(self, req_id: str) -> dict:
        """The code elements that may implement a requirement, proposed by the agent
        req_linker: for each one, a confidence (high | medium | low), the reason, the
        evidence that made it a candidate and the `Req:` tag to add and where. It changes
        nothing in the code. Asks the model: slower than trace."""
        import openai
        from atlas.agents.link import link
        from atlas.index import Index
        from atlas.llm.connection import diagnose
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        try:
            return link(req_id, self.cfg, graph=g, index=Index.from_config(self.cfg))
        except openai.APIError as e:
            return {"error": f"the model server failed: {diagnose(e)}"}


    def ask_atlas(self, question: str) -> dict:
        """Asks the assistant of atlas, an agent that answers from the documents, the DOORS
        requirements and the code with its own tools, citing them. One call that does the
        searches behind; slower than the other tools. Returns the answer and the tools it
        called."""
        import openai
        from atlas.agents.runtime import run_agent
        from atlas.llm.connection import diagnose
        try:
            return run_agent("assistant", question, self.cfg, self.context)
        except ImportError as e:
            return {"error": str(e)}
        except openai.APIError as e:
            return {"error": f"the model server failed: {diagnose(e)}"}


register_methods(AgentTools, TOOL_NAMES)
