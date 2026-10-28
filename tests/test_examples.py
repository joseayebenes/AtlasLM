"""The examples run as they are, so that they do not fall behind the code they show: those of
library use (examples/library), which use only the public API of docs/api.md, the walkthrough
of the course (examples/walkthrough.py), and the agents of examples/agents load with their
tools."""
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


def test_walkthrough(samples, docs_samples, fake, tmp_path):
    """examples/walkthrough.py, the script of the course and the smoke test of a workstation:
    every action prints its command, its output and its time, and the table at the end
    says each one went well."""
    for module in ("markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    config = tmp_path / "atlas.toml"
    config.write_text((ROOT / "atlas.test.toml").read_text(encoding="utf-8")       # its paths, from the repository
                      .replace("http://127.0.0.1:8770/v1", fake[0])
                      .replace('"./workspace-test"', f'"{(tmp_path / "ws").as_posix()}"')
                      .replace('"examples/', f'"{ROOT.as_posix()}/examples/')
                      .replace('dirs = ["agents"]', f'dirs = ["{ROOT.as_posix()}/agents"]'), encoding="utf-8")
    res = subprocess.run([sys.executable, str(ROOT / "examples" / "walkthrough.py"), str(config), "--skip", "agent"],
                         capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=600)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert f"$ atlas trace SRS-0004 {config}" in out and '$ atlas search "track deletion latency"' in out
    assert re.search(r"docs\.process: (\d+) units, 0 to process, \1 up to date", out)          # run again: nothing
    assert "40 requirements: 4 with code" in out and "Sources:" in out
    table = out.rsplit("action", 1)[1]
    for action in ("version", "check", "sections", "run", "run again", "status", "graph", "trace", "matrix",
                   "search", "ask"):
        assert re.search(rf"^{action} +[\d.]+  ok$", table, re.M), (action, table)
    assert re.search(r"^agent +-  skipped \(--skip\)$", table, re.M)
