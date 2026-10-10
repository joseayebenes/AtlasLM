"""ADK agents (phase 5 of the plan), defined in Markdown files.

    definitions.py   AgentDef and load_definitions: the agents of atlas/agents/builtin and of
                     [agents] dirs, with the retouches of [agents.<name>] (no ADK needed)
    model.py         AtlasLlm: an ADK model over the client of atlas.llm (proxy, TLS, limits)
    runtime.py       build_agent and run_agent: the agent of a file as an ADK LlmAgent, with
                     its tools (the "tool" plugins) and at most max_steps calls to the model
    context.py       build_context: a Markdown package about a requirement, code or a unit
    verify.py        verification of requirements against the documentation
    reports.py       impact_report and coverage_report: the graph finds, an agent writes
    tools.py         build_context, verify_requirement and ask_atlas as "tool" plugins
    evaluate.py      atlas eval: questions with a known answer, scored without a model

    from atlas.agents.runtime import run_agent
    run_agent("assistant", "Which tests verify SRS-0004?", cfg)["answer"]
"""
