"""hephesto.mcp and `hephesto serve`: the compiler, the tests and the judge of a change for the
agent of an editor, over atlas's server; and atlas serve, which never offers them."""
import json
import shutil
import sys

import pytest

import hephesto  # noqa: F401  (its tools)
from atlas import plugins
from atlas.config import ConfigError, load_config


def _tools(server) -> dict[str, dict]:
    """name -> the tool as a client sees it (inputSchema, annotations...)."""
    import anyio

    async def go():
        return await server.list_tools()
    return {t.name: t.model_dump(by_alias=True, exclude_none=True) for t in anyio.run(go)}


def test_hephesto_serve_offers_the_compiler_the_tests_and_the_judge(toml, samples):
    pytest.importorskip("mcp")
    from hephesto.mcp import create_server, tool_names
    plugins.load_plugins()
    code = f'[sources.code]\nroots = ["{(samples / "ada").as_posix()}"]\nreq_regex = "@req (.+)"\nlanguages = ["ada"]\n'
    cfg = load_config(toml(code))
    assert tool_names(cfg) == ["check_code", "run_test", "run_tests", "judge_changes"]
    assert "run_program" in tool_names(load_config(toml(code + 'program_command = ["prog"]\n')))
    assert tool_names(cfg, edit=True)[:7] == ["list_files", "grep", "read_file", "outline", "read_subprogram",
                                               "edit_file", "write_file"]
    assert "judge_changes" not in tool_names(load_config(toml(code + '[plugins]\ndisabled = ["tool:judge_changes"]\n')))
    server = create_server(cfg)
    tools = _tools(server)
    assert list(tools) == tool_names(cfg)
    assert tools["check_code"]["annotations"] == {"readOnlyHint": False, "destructiveHint": False}   # it only builds
    assert tools["run_test"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    assert "`check_code` compiles a file" in server.instructions and "`judge_changes` says" in server.instructions
    assert "edit_file, write_file" not in server.instructions
    assert "A change: read the code to change -> edit it -> `check_code` the file until it has no errors -> " \
           "`run_test` its tests." in server.instructions
    edit = create_server(cfg, edit=True)
    assert _tools(edit)["edit_file"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    assert "edit_file, write_file" in edit.instructions


def test_atlas_serve_never_offers_what_writes_or_runs(toml, tmp_path):
    """With hephesto installed and imported, atlas serve still only reads: its tools are not
    offered, by profile or by name."""
    pytest.importorskip("mcp")
    from atlas.mcp import create_server, default_tools, tool_names
    plugins.load_plugins()
    code = f'[sources.code]\nroots = ["{tmp_path.as_posix()}"]\ncheck_command = ["gcc", "-c", "{{file}}"]\n'
    cfg = load_config(toml(code))
    assert not {"check_code", "edit_file", "run_test"} & set(default_tools(cfg) + tool_names(cfg))
    for name in ("edit_file", "check_code", "judge_changes"):
        with pytest.raises(ConfigError, match=f"\\['{name}'\\] write or run the code.*hephesto serve"):
            create_server(load_config(toml(code + f'[mcp]\ntools = ["read_file", "{name}"]\n')))


CALC_TEST = """import sys
sys.path.insert(0, "SRC")
import calc
def check(step, got, want):
    if got != want:
        print(f"Step {step}: expected {want}, received {got}")
        sys.exit(1)
check(1, calc.add(1, 2), 3)
check(2, getattr(calc, "mul", lambda a, b: None)(2, 3), 6)
"""


def test_the_judge_of_develop_for_an_editor(toml, tmp_path, monkeypatch):
    """run_tests and judge_changes over the Python project of test_develop: by test, how far
    it gets; after a change, what changed, whether it compiles and which tests moved."""
    pytest.importorskip("mcp")
    import anyio
    from atlas.mcp import default_tools
    from hephesto.mcp import create_server
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(CALC_TEST.replace("SRC", (tmp_path / "src").as_posix()))
    (tmp_path / "plan.toml").write_text("tests = [\"tests/test_*.py\"]\nprogress = 'Step (\\d+)'\n")
    code = f'''[sources.code]
roots = ["src", "tests"]
exclude = ["**/__pycache__/**"]
protected = ["tests/**"]
check_command = ["{sys.executable}", "-m", "py_compile", "{{file}}"]
[sources.code.test_commands]
py = ["{sys.executable}", "{{file}}"]
'''
    plugins.load_plugins()
    cfg = load_config(toml(code))
    assert not {"run_tests", "judge_changes"} & set(default_tools(cfg))         # they run code: not in atlas
    server = create_server(cfg)
    tools = _tools(server)
    assert tools["run_tests"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    assert tools["judge_changes"]["annotations"]["destructiveHint"] is True
    assert "`judge_changes` says after a change" in server.instructions

    def call(name, **arguments):
        async def go():
            return await server.call_tool(name, arguments)
        result = anyio.run(go)                       # mcp 1.x: (content, structured); 2.x: CallToolResult
        content = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
        return json.loads(content[0].text)

    assert call("run_tests", plan="plan.toml") == {
        "tests_of": "plan.toml", "passed": 0, "of": 1,
        "tests": {"tests/test_calc.py": {"passed": False, "progress": 1.0, "output": "Step 1: expected 3, received 0"}}}
    assert call("run_tests", plan="../outside.toml")["error"].startswith("../outside.toml is not a plan file")
    first = call("judge_changes")                      # not a git repository: the first call takes the snapshot
    assert first["changed"] == [] and first["changed_since"].startswith("nothing")
    assert first["tests"]["advanced"] == [] and first["tests"]["went_back"] == []

    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")       # gets to step 2
    judged = call("judge_changes")
    assert judged["changed_since"] == "the last call" and judged["changed"] == ["src/calc.py"]
    assert judged["compiles"] == ["src/calc.py"] and "does_not_compile" not in judged
    assert judged["tests"]["advanced"] == ["tests/test_calc.py"] and judged["tests"]["tests_of"] == "plan.toml"

    (tmp_path / "src" / "calc.py").write_text("def add(a, b:\n    return a + b\n")        # breaks it
    judged = call("judge_changes")
    assert list(judged["does_not_compile"]) == ["src/calc.py"] and judged["compiles"] == []
    back = judged["tests"]["went_back"]
    assert [b["test"] for b in back] == ["tests/test_calc.py"] and back[0]["was"] == "got to 2"
    assert json.loads((cfg.workspace / "judge.json").read_text())["plan"] == "plan.toml"

    # in a git repository, the changes are against the last commit
    if shutil.which("git"):
        import subprocess
        (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        for command in (["init", "-q"], ["add", "src", "tests", "plan.toml"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", *command], cwd=tmp_path, check=True)
        (tmp_path / "src" / "more.py").write_text("X = 1\n")
        judged = call("judge_changes", paths=["src/more.py", "src/none.py"])
        assert judged["changed_since"] == "the last commit (git)" and judged["changed"] == ["src/more.py"]
        assert judged["compiles"] == ["src/more.py"] and list(judged["does_not_compile"]) == ["src/none.py"]
