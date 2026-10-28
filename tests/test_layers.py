"""atlas knows and reads; hephesto writes and runs. The modules that write or run the code of
the project (on their way to hephesto) are only imported by the ones that are going with them,
and by the few that still mix both sides (TRANSITIONAL), until they are split."""
import ast
from pathlib import Path

ATLAS = Path(__file__).resolve().parents[1] / "atlas"

HEPHESTO = {"atlas.agents.develop", "atlas.agents.hints", "atlas.code.build", "atlas.code.judge", "atlas.code.tools",
            "atlas.code.procedures"}
TRANSITIONAL = {"atlas.cli", "atlas.mcp", "atlas.agents.evaluate", "atlas.code"}


def _module(path: Path) -> str:
    parts = path.relative_to(ATLAS.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path, module: str) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[:len(package.split(".")) - node.level + 1]
                name = ".".join(base + ([node.module] if node.module else []))
            else:
                name = node.module or ""
            found |= {name} | {f"{name}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def test_the_reading_tools_bring_nothing_that_writes_or_runs(tmp_path):
    """read_file, outline, library_spec and find_definition, as plugins, over a CodeReader and a
    Toolchain: no CodeTools and no BuildTools in their context."""
    from types import SimpleNamespace
    from atlas import plugins
    from atlas.code import CodeReader, Toolchain
    from atlas.config import load_config
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    toml = tmp_path / "atlas.toml"
    toml.write_text(f'[project]\nworkspace = "{(tmp_path / "ws").as_posix()}"\n[sources.code]\nroots = ["src"]\n'
                    'languages = ["python"]\n', encoding="utf-8")
    plugins.load_plugins()
    context = SimpleNamespace(cfg=load_config(toml))
    tools = {n: plugins.get("tool", n)(context) for n in ("read_file", "outline", "library_spec", "find_definition")}
    assert tools["outline"].run(path="src/main.py")["subprograms"][0]["name"] == "main"
    assert "main" in tools["read_file"].run(path="src/main.py")["source"]
    assert type(context._code_tools) is CodeReader and type(context._toolchain) is Toolchain
    assert not hasattr(context, "_build_tools")
    assert not any(t.writes for t in tools.values())


def test_atlas_does_not_import_what_writes_or_runs_the_code():
    wrong = {}
    for path in ATLAS.rglob("*.py"):
        module = _module(path)
        if module in HEPHESTO | TRANSITIONAL:
            continue
        bad = sorted(i for i in _imports(path, module) if i in HEPHESTO)
        if bad:
            wrong[module] = bad
    assert not wrong, f"modules of atlas that import what writes or runs the code: {wrong}"
