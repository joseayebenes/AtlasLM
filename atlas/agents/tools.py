"""Tools of atlas.agents for agents and the MCP server.

    build_context        everything about a requirement, a code element or a unit, in one
                         Markdown package within a budget of tokens, without a model
    context_at           the same for the element at a line of a file (what the editor has open)
    similar_requirements requirements like one that already have code, with the signature and
                         the Req: tag of that code: examples to follow, without a model
    verify_requirement   whether the official text of a requirement and the documents agree,
                         judged by the agent requirement_verifier (needs the model)
    link_requirement     the code that may implement a requirement, proposed by the agent
                         req_linker (needs the model); it changes nothing
    check_compliance     whether the code of a requirement does all it asks, obligation by
                         obligation, judged by the agent compliance_checker (needs the model)
    ask_atlas            a question to the agent assistant, which searches with its own tools
                         (needs the model and ADK): one call for the client, several behind

They are registered as "tool" plugins with these names, for agent files and [mcp] tools.
"""
from __future__ import annotations

import logging
from typing import Any

from atlas.agents.context import build_context, similar_requirements
from atlas.graph.tools import NO_GRAPH, GraphTools
from atlas.plugins import register_methods

__all__ = ["AgentTools", "TOOL_NAMES"]

TOOL_NAMES = ("build_context", "context_at", "similar_requirements", "verify_requirement", "link_requirement",
              "check_compliance", "ask_atlas")

log = logging.getLogger(__name__)


class AgentTools:
    def __init__(self, graph: GraphTools, cfg=None, context: Any = None):
        self.graph = graph
        self.cfg = cfg
        self.context = context
        self._search: Any = None           # the index, opened on first use; False if there is none

    @classmethod
    def for_context(cls, context: Any) -> "AgentTools":
        """One instance per context, over the graph tools of that context."""
        tools = getattr(context, "_agent_tools", None)
        if tools is None:
            tools = cls(GraphTools.for_context(context), context.cfg, context)
            context._agent_tools = tools
        return tools

    def build_context(self, target: str, budget_tokens: int = 4000) -> dict:
        """Everything about a requirement ID, a code element or a unit (package) in one
        Markdown package of at most `budget_tokens`: requirements with their text,
        signatures and comments, callers and callees, tests, document sections and source.
        Call it before changing code, instead of many searches."""
        g = self.graph.graph()
        return build_context(g, target, budget_tokens, self._index()) if g is not None else {"error": NO_GRAPH}

    def context_at(self, path: str, line: int, budget_tokens: int = 4000) -> dict:
        """build_context of the code at a line of a file: the subprogram that holds it (or
        the type, variable or unit), with its requirements, callers, tests, documents and
        source. For the file and the line that the editor has open, before changing them.
        `path` is relative to the folder of the project, or absolute."""
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        n, err = g.at(path, line)
        if err or n is None:
            return err or {"error": f"nothing at {path}:{line}"}
        return build_context(g, n, budget_tokens, self._index())

    def similar_requirements(self, req_id: str, k: int = 3) -> dict:
        """Up to `k` requirements like this one that already have code, as examples of how
        this project implements and tags one: those linked to it in DOORS or stated in the
        same sections, then those whose text is alike. For each one, its text, why it is
        similar, the subprogram that implements it with its signature, the line of its
        `Req:` tag and, for the first one, the start of its body. Without a model."""
        g = self.graph.graph()
        return similar_requirements(g, req_id, k, self._index()) if g is not None else {"error": NO_GRAPH}

    def _index(self) -> Any:
        """The index of the workspace for the searches without vectors, or None if there is
        none (no atlas.db, or atlas.index cannot be imported)."""
        if self._search is None:
            self._search = False
            if self.cfg is not None and (self.cfg.workspace / "atlas.db").exists():
                try:
                    from atlas.index import Index
                    self._search = Index.from_config(self.cfg)
                except Exception as e:
                    log.info("no index for the similar requirements: %s", e)
        return self._search or None

    def verify_requirement(self, req_id: str) -> dict:
        """Whether the official text of a requirement (DOORS) and the documents say the same:
        verdict (consistent | inconsistent | no_documentation), the inconsistencies (value,
        condition, contradiction, scope, undefined) with the words of each text and their
        source, and the values that differ. Asks the model: slower than trace."""
        import openai
        from atlas.agents.verify import verify
        from atlas.index import Index
        from atlas.llm import model_failed
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        try:
            return verify(req_id, self.cfg, graph=g, index=Index.from_config(self.cfg))
        except openai.APIError as e:
            return model_failed(e, self.cfg)

    def link_requirement(self, req_id: str) -> dict:
        """The code elements that may implement a requirement, proposed by the agent
        req_linker: for each one, a confidence (high | medium | low), the reason, the
        evidence that made it a candidate and the `Req:` tag to add and where. It changes
        nothing in the code. Asks the model: slower than trace."""
        import openai
        from atlas.agents.link import link
        from atlas.index import Index
        from atlas.llm import model_failed
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        try:
            return link(req_id, self.cfg, graph=g, index=Index.from_config(self.cfg))
        except openai.APIError as e:
            return model_failed(e, self.cfg)


    def check_compliance(self, req_id: str) -> dict:
        """Whether the code that implements a requirement (by its Req: tags) does all that it
        asks: the requirement split into obligations, each met, not_met, partial or
        cannot_tell (timing, performance, code not shown), with the code that decides it,
        and a verdict (complies | does_not_comply | cannot_tell | no_code). A help for the
        inspection, not a proof. Asks the model: slower than trace."""
        import openai
        from atlas.agents.comply import comply
        from atlas.llm import model_failed
        g = self.graph.graph()
        if g is None:
            return {"error": NO_GRAPH}
        try:
            return comply(req_id, self.cfg, graph=g)
        except openai.APIError as e:
            return model_failed(e, self.cfg)

    def ask_atlas(self, question: str) -> dict:
        """Asks the assistant of atlas, an agent that answers from the documents, the DOORS
        requirements and the code with its own tools, citing them. One call that does the
        searches behind; slower than the other tools. Returns the answer, the tools it
        called and its `status`: "answered" (every ID and value of the answer is in what its
        tools returned), "partial" (some are not: those sentences in `uncited`) or
        "not_found" (no answer, or no tool gave anything); a partial answer must be given as
        such."""
        import json
        import openai
        from atlas.agents.runtime import run_agent
        from atlas.index.search import grounded_status
        from atlas.llm import model_failed
        record: list = []
        try:
            res = run_agent("assistant", question, self.cfg, self.context, record=record)
        except ImportError as e:
            return {"error": str(e)}
        except openai.APIError as e:
            return model_failed(e, self.cfg)
        results = [json.dumps(c["result"], ensure_ascii=False, default=str) for c in record
                   if not (isinstance(c["result"], dict) and "error" in c["result"])]
        status = grounded_status(res["answer"], results)
        if res.get("stopped") and status["status"] == "answered":
            status["status"] = "partial"
        return {**res, **status}


register_methods(AgentTools, TOOL_NAMES)
