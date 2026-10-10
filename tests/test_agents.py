"""atlas.agents: agents defined in Markdown files, run with ADK over atlas.llm."""
import pytest

from atlas import plugins
from atlas.agents.definitions import BUILTIN, load_definitions, parse_definition
from atlas.config import ConfigError, load_config

AGENT = """---
name: {name}
description: {description}
tools: {tools}
---
{body}
"""


def _agent(folder, file, name="tracer", description="Traces.", tools="[trace]", body="Trace requirements."):
    folder.mkdir(exist_ok=True)
    (folder / file).write_text(AGENT.format(name=name, description=description, tools=tools, body=body))


def test_the_agents_come_from_markdown_files(toml, tmp_path):
    builtin = load_definitions()
    assert set(builtin) == {"assistant", "requirement_verifier", "impact_analyst", "coverage_auditor"}
    assistant = builtin["assistant"]
    assert assistant.instruction.startswith("You answer questions about an engineering project")
    assert assistant.max_steps == 10 and assistant.reasoning_effort == "none"
    plugins.load_plugins()
    assert set(assistant.tools) <= set(plugins.available("tool"))
    assert builtin["requirement_verifier"].tools == []

    # those of the project: a new one, and one with the name of a built-in one replaces it
    agents = tmp_path / "agents"
    _agent(agents, "tracer.md")
    _agent(agents, "my_assistant.md", name="assistant", tools="search, get_requirement", body="Only search.")
    cfg = load_config(toml("[agents.tracer]\nmax_steps = 3\nmodel = \"other\"\n"))
    found = load_definitions(cfg)
    assert found["assistant"].tools == ["search", "get_requirement"] and found["assistant"].instruction == "Only search."
    assert (found["tracer"].max_steps, found["tracer"].model, found["tracer"].tools) == (3, "other", ["trace"])
    assert found["tracer"].source == str(agents / "tracer.md")
    assert set(load_definitions(load_config(toml('[agents]\ndirs = []\n')))) == set(builtin)


def test_bad_agent_files_and_retouches(toml, tmp_path):
    with pytest.raises(ConfigError, match="YAML header"):
        parse_definition("no header\n", "a.md")
    with pytest.raises(ConfigError, match=r"unknown keys \['tool'\]"):
        parse_definition("---\nname: a\ntool: [x]\n---\nDo it.\n", "a.md")
    with pytest.raises(ConfigError, match="no instructions"):
        parse_definition("---\nname: a\n---\n", "a.md")
    with pytest.raises(ConfigError, match="lower case"):
        parse_definition("---\nname: My Agent\n---\nDo it.\n", "a.md")
    assert parse_definition("---\ntools: [trace]\n---\nDo it.\n", "dir/named_by_file.md").name == "named_by_file"
    with pytest.raises(ConfigError, match="does not exist"):
        load_definitions(load_config(toml("[agents.nobody]\nmax_steps = 2\n")))
    with pytest.raises(ConfigError, match=r"\[agents.assistant\] unknown key: 'tool'"):
        load_config(toml("[agents.assistant]\ntool = [\"x\"]\n"))
    with pytest.raises(ConfigError, match="positive"):
        load_definitions(load_config(toml("[agents.assistant]\nmax_steps = 0\n")))
    _agent(tmp_path / "agents", "one.md", name="twin")
    _agent(tmp_path / "agents", "two.md", name="twin")
    with pytest.raises(ConfigError, match="two agents named 'twin'"):
        load_definitions(load_config(toml()))


def test_atlas_agents_lists_them(toml, capsys):
    from atlas.cli import list_agents
    assert list_agents(load_config(toml())) == 0
    out = capsys.readouterr().out
    assert "assistant              built-in" in out and "tools: search, get_section" in out
    assert BUILTIN.name == "builtin"
