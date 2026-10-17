"""The examples run as they are, so that they do not fall behind the code they show: those of
library use (examples/library), which use only the public API of docs/api.md, and the agents
of examples/agents load with their tools."""
import ast
import importlib
import re
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


@pytest.mark.slow
def test_pipeline_example(samples, tmp_path):
    out = _run("pipeline.py", tmp_path)
    assert "second run: {}" in out and "profile of the first one" in out


@pytest.mark.slow
def test_documents_example(docs_samples, tmp_path):
    pytest.importorskip("markitdown")
    out = _run("documents.py", tmp_path)
    assert "requirements, e.g. SRS-0001" in out and (tmp_path / "spec" / "index.md").exists()


@pytest.mark.slow
def test_traceability_example(samples, docs_samples, tmp_path):
    for module in ("markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    out = _run("traceability.py", tmp_path)
    assert "implemented by: ['tracks.delete_track']" in out and "'320 ms'" in out and "context package" in out


@pytest.mark.slow
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


def _promised() -> dict[str, set[str]]:
    """The table of docs/api.md: module -> the names it promises."""
    rows = re.findall(r"^\| `(atlas[\w.]*)` \| ([^|]+) \|", (ROOT / "docs" / "api.md").read_text(encoding="utf-8"),
                      re.MULTILINE)
    return {module: set(re.findall(r"`(\w+)`", names)) for module, names in rows}


def test_the_public_api_is_in_the_all_of_each_module():
    promised = _promised()
    assert {"atlas", "atlas.pipeline", "atlas.plugins", "atlas.mcp"} <= set(promised)
    for module, names in promised.items():
        exported = set(importlib.import_module(module).__all__)
        assert names <= exported, f"{module}: {sorted(names - exported)} are not in its __all__"


def test_the_library_examples_import_only_the_public_api():
    promised = _promised()
    for script in sorted((ROOT / "examples" / "library").glob("*.py")):
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "atlas":
                names = {a.name for a in node.names}
                assert names <= promised.get(node.module, set()), \
                    f"{script.name} imports {sorted(names - promised.get(node.module, set()))} from {node.module}"
