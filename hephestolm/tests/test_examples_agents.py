"""The coding agents of hephestolm/examples, as the configurations of the repository load them."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_agents_of_the_examples_load_and_their_tools_exist(monkeypatch):
    """hephestolm/examples/agents: the coding agents measured with gemma-4-26b (implementer,
    fixer, planner) and the tester, with the sampling of atlas.openrouter.toml; every tool they
    name is a "tool" plugin of atlas or of hephesto, so that hephesto develop does not stop at
    the first task."""
    import hephesto  # noqa: F401  (its tools)
    from atlas import plugins
    from atlas.agents.definitions import load_definitions
    from atlas.config import load_config
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    found = load_definitions(load_config(ROOT / "atlas.openrouter.toml"))
    plugins.load_plugins([])
    tools = set(plugins.available("tool"))
    for name in ("implementer", "fixer", "planner", "tester"):
        agent = found[name]
        assert agent.source.endswith(f"examples/agents/{name}.md")
        assert not set(agent.tools) - tools, (name, sorted(set(agent.tools) - tools))
    for name in ("implementer", "fixer"):
        assert (found[name].stall_steps, found[name].keep_tool_results) == (15, 10)
        assert {"replace_subprogram", "add_subprogram", "library_spec", "search_reference"} <= set(found[name].tools)
    for name in ("implementer", "fixer", "planner"):
        agent = found[name]
        assert (agent.reasoning_effort, agent.temperature, agent.top_p, agent.top_k) == ("low", 1.0, 0.95, 64)
        assert "gemma-4-26b" in (ROOT / "hephestolm" / "examples" / "agents" / f"{name}.md").read_text().split("---")[1]
    assert "run_test" in found["fixer"].tools and not {"write_file", "edit_file"} & set(found["planner"].tools)
    dev = load_definitions(load_config(ROOT / "atlas.dev.toml"))       # gemma4:12b: no sampling of gemma-4-26b
    assert dev["implementer"].top_k is None and dev["implementer"].tools == found["implementer"].tools
