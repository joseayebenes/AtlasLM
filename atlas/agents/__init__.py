"""ADK agents (phase 5 of the plan), defined in Markdown files.

    definitions.py   AgentDef and load_definitions: the agents of atlas/agents/builtin and of
                     [agents] dirs, with the retouches of [agents.<name>] (no ADK needed)
    model.py         AtlasLlm: an ADK model over the client of atlas.llm (proxy, TLS, limits)
    runtime.py       build_agent, Conversation and run_agent: the agent of a file as an ADK
                     LlmAgent, with its tools (the "tool" plugins) and at most max_steps calls
                     to the model in an answer; a conversation of several questions in one session
    context.py       build_context: a Markdown package about a requirement, code or a unit
    verify.py        verification of requirements against the documentation
    reports.py       impact_report and coverage_report: the graph finds, an agent writes
    comply.py        compliance of the code with a requirement, obligation by obligation
    tools.py         build_context, verify_requirement, link_requirement, check_compliance and ask_atlas
                     as "tool" plugins
    evaluate.py      atlas eval: questions with a known answer, scored without a model

    from atlas.agents import run_agent
    run_agent("assistant", "Which tests verify SRS-0004?", cfg)["answer"]
"""
__all__ = ["build_context", "run_agent"]


def __getattr__(name: str):
    """On first use: importing a module of atlas.agents does not load the graph and the
    model client that these two need."""
    if name == "build_context":
        from atlas.agents.context import build_context
        return build_context
    if name == "run_agent":
        from atlas.agents.runtime import run_agent
        return run_agent
    raise AttributeError(f"module 'atlas.agents' has no attribute {name!r}")
