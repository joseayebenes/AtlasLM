"""The examples run as they are, so that they do not fall behind the code they show: those of
library use (examples/library), and the agents of examples/agents load with their tools."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, *args) -> str:
    res = subprocess.run([sys.executable, str(ROOT / "examples" / "library" / script), *map(str, args)],
                         capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


def test_pipeline_example(samples, tmp_path):
    out = _run("pipeline.py", tmp_path)
    assert "second run: {}" in out and "profile of the first one" in out


def test_documents_example(docs_samples, tmp_path):
    pytest.importorskip("markitdown")
    out = _run("documents.py", tmp_path)
    assert "requirements, e.g. SRS-0001" in out and (tmp_path / "spec" / "index.md").exists()


def test_traceability_example(samples, docs_samples, tmp_path):
    for module in ("markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    out = _run("traceability.py", tmp_path)
    assert "implemented by: ['tracks.delete_track']" in out and "'320'" in out and "context package" in out


def test_search_example(docs_samples, tmp_path, fake):
    pytest.importorskip("markitdown")
    (tmp_path / "atlas.toml").write_text(f'''
[project]
workspace = "{(tmp_path / 'ws').as_posix()}"
[llm]
base_url = "{fake[0]}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["docs", "index"]
[[sources.docs]]
name = "manuals"
paths = ["{(docs_samples / 'hmi_manual.md').as_posix()}"]
''')
    out = _run("search.py", tmp_path / "atlas.toml")
    assert "hmi_manual.md" in out and "[1]" in out


def test_the_agents_of_the_examples_load_and_their_tools_exist(monkeypatch):
    """examples/agents: the coding agents measured with gemma-4-26b (implementer, fixer,
    planner) and the tester, with the sampling of atlas.openrouter.toml; every tool they name is
    a "tool" plugin, so that atlas develop does not stop at the first task."""
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
        assert "gemma-4-26b" in (ROOT / "examples" / "agents" / f"{name}.md").read_text().split("---")[1]
    assert "run_test" in found["fixer"].tools and not {"write_file", "edit_file"} & set(found["planner"].tools)
    dev = load_definitions(load_config(ROOT / "atlas.dev.toml"))       # gemma4:12b: no sampling of gemma-4-26b
    assert dev["implementer"].top_k is None and dev["implementer"].tools == found["implementer"].tools
