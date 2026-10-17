"""atlas.mcp and `atlas serve`: one MCP server with the document, graph and file tools."""
import json
import shutil
import sys
from pathlib import Path

import pytest

from atlas import plugins
from atlas.cli import run
from atlas.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _tools(server) -> dict[str, dict]:
    """name -> the tool as a client sees it (inputSchema, annotations...)."""
    import anyio

    async def go():
        return await server.list_tools()
    return {t.name: t.model_dump(by_alias=True, exclude_none=True) for t in anyio.run(go)}


def test_the_configuration_chooses_the_tools(toml, tmp_path):
    pytest.importorskip("mcp")
    from atlas.mcp import create_server, default_tools, tool_names
    plugins.load_plugins()
    assert default_tools(load_config(toml())) == ["search", "get_requirement", "trace", "matrix", "gaps",
                                                  "list_requirements", "neighbors", "build_context"]  # only DOORS
    assert default_tools(load_config(toml('[pipeline]\nsteps = ["doors"]\n'))) == ["get_requirement"]   # no index

    code = f'[sources.code]\nroots = ["{tmp_path.as_posix()}"]\n'
    docs = f'[[sources.docs]]\nname = "d"\npaths = ["{tmp_path.as_posix()}"]\n'
    cfg = load_config(toml(code + docs))
    names = tool_names(cfg)
    assert names[:5] == ["list_documents", "search", "get_section", "get_requirement", "ask_docs"]
    assert {"find_symbol", "impact", "trace", "trace_code", "matrix", "gaps", "build_context",
            "read_file"} <= set(names)
    assert not {"edit_file", "write_file", "grep"} & set(names)              # knowledge; the editor edits
    server = create_server(cfg)
    tools = _tools(server)
    assert list(tools) == names
    assert tools["impact"]["inputSchema"]["required"] == ["name"]
    assert tools["impact"]["annotations"]["readOnlyHint"] is True
    assert tools["impact"]["description"].startswith("What changing a code element or a requirement affects.")
    assert "find_symbol -> get_node" in server.instructions and "edit_file" not in server.instructions
    assert "`build_context` gives in one call" in server.instructions

    # a list of its own, writing tools included; repeated names count once
    server = create_server(load_config(toml(code + '[mcp]\ntools = ["search", "edit_file", "search", "ask_atlas"]\n')))
    tools = _tools(server)
    assert list(tools) == ["search", "edit_file", "ask_atlas"]
    assert "`ask_atlas` asks the assistant" in server.instructions
    assert tools["edit_file"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    with pytest.raises(ConfigError, match=r"unknown tools \['graph_query'\]"):
        create_server(load_config(toml('[mcp]\ntools = ["search", "graph_query"]\n')))
    with pytest.raises(ConfigError, match="disabled"):
        create_server(load_config(toml('[mcp]\ntools = ["search"]\n[plugins]\ndisabled = ["tool:search"]\n')))
    names = tool_names(load_config(toml(code + '[plugins]\ndisabled = ["tool:impact"]\n')))
    assert "impact" not in names and "callers" in names
    with pytest.raises(ConfigError, match="turned off"):
        create_server(load_config(toml("[mcp]\nenabled = false\n")))


@pytest.fixture
def project(toml, samples, docs_samples, tmp_path, fake):
    """A workspace that `atlas run` has filled: DOORS, the code graph of the Ada samples and
    two indexed documents, with the fake API for the embeddings and the answers."""
    for module in ("mcp", "markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    ada, docs = tmp_path / "ada", tmp_path / "docs"
    shutil.copytree(samples / "ada", ada)
    docs.mkdir()
    for f in ("spec_with_bookmarks.pdf", "hmi_manual.md"):
        shutil.copy(docs_samples / f, docs / f)
    cfg = load_config(toml(f'''
[llm]
base_url = "{fake[0]}"
model = "fake"
[embeddings]
model = "embeddinggemma"
[network]
proxy = ""
no_proxy = "*"
[[sources.docs]]
name = "docs"
paths = ["{docs.as_posix()}"]
patterns = ["*.pdf", "*.md"]
analysis = "regex"
[sources.code]
roots = ["{ada.as_posix()}"]
test_patterns = ["**/tests/**"]
'''))
    assert run(cfg) == 0
    return cfg, ada, docs


def test_atlas_serve_over_stdio_follows_atlas_run(project, tmp_path):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    cfg, ada, docs = project
    params = StdioServerParameters(command=sys.executable, args=["-m", "atlas.cli", "serve", str(cfg.path)],
                                   cwd=str(ROOT))
    errlog = tmp_path / "server.log"

    async def call(session, tool: str, **args) -> dict:
        res = await session.call_tool(tool, args)
        assert not getattr(res, "is_error", getattr(res, "isError", False)), res
        return json.loads(res.content[0].text)

    async def client():
        with open(errlog, "w") as err:
            async with stdio_client(params, errlog=err) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                assert {"search", "get_requirement", "trace", "callers", "read_file"} <= set(names)

                # documents and DOORS
                hits = (await call(session, "search", query="SRS-0102 alarm", k=3))["results"]
                hit = next(h for h in hits if h["document"] == "hmi_manual.md")
                assert hit["ref"] == "SRS-0102" and hit["cite"].startswith("hmi_manual.md › 2 Alarms")
                assert {"SRS_TMS_OLD", "hmi_manual.md"} <= {h["document"] for h in hits if h["ref"] == "SRS-0102"}
                section = await call(session, "get_section", document=hit["document"], section=hit["section"])
                assert "raise an alarm when a track is lost" in section["markdown"]
                assert "DOORS module" in (await call(session, "get_section", document="SRS_TMS", section="1"))["error"]
                listed = (await call(session, "list_documents"))["documents"]
                assert {"name": "SRS_TMS", "source": "DOORS"}.items() <= next(d for d in listed
                                                                              if d["name"] == "SRS_TMS").items()
                req = await call(session, "get_requirement", req_id="srs-0004")
                assert req["id"] == "SRS-0004" and req["official"]["module"] == "SRS_TMS"
                assert {"target": "SSS-0005", "type": "satisfies"} in req["official"]["links_out"]
                assert req["documents"][0]["cite"].startswith("spec_with_bookmarks.pdf")
                assert "SRS-0040" in (await call(session, "get_requirement", req_id="SRS-004"))["similar"]
                answer = await call(session, "ask_docs", question="When does the HMI raise an alarm?")
                assert "[1]" in answer["answer"] and answer["sources"][0]["doc"] == "hmi_manual.md"

                # the graph: traceability of DOORS, documents, code and tests
                traced = await call(session, "trace", req_id="SRS-0004")
                assert traced["in_doors"] and {"id": "SSS-0005", "type": "satisfies"} in traced["links_out"]
                assert traced["documents"][0]["document"] == "spec_with_bookmarks.pdf"
                assert [i["id"] for i in traced["implemented_by"]["items"]] == ["tracks.delete_track"]
                assert traced["verified_by"] == [{"id": "test_tracks.test_tracks", "how": "tag"}]
                matrix = await call(session, "matrix", module="SRS_TMS", limit=4)
                assert [r["code"] for r in matrix["rows"]] == [["tracks.track"], ["tracks.update"], ["tracks.update"],
                                                                ["tracks.delete_track"]]
                assert matrix["summary"]["with_tests"] == 1 and "SRS-0002" in matrix["gaps"]["without_tests"]["items"]
                gaps = await call(session, "gaps")
                assert gaps["cited_in_code_not_in_doors"]["items"] == [{"id": "SRS-0103", "cited_by": ["logger.log"]}]
                impact = await call(session, "impact", name="Logger.Log")
                assert {"SRS-0103", "SRS-0004", "SSS-0005"} <= {r["id"] for r in impact["requirements"]}
                package = await call(session, "build_context", target="SRS-0004")
                assert package["tokens"] <= 4000 and "procedure Delete_Track" in package["markdown"]

                # code and files
                source = await call(session, "read_source", name="Delete_Track")
                assert "procedure Delete_Track" in source["source"]
                assert (await call(session, "read_file", path=source["file"], end=2))["end"] == 2
                assert "outside the code roots" in (await call(session, "read_file", path=str(cfg.path)))["error"]

                # `atlas run` while the server is up: it answers with the new graph and index
                callers = await call(session, "callers", name="logger.log")
                assert [i["id"] for i in callers["items"]] == ["tracks.delete_track", "tracks.update"]
                body, manual = ada / "src" / "logger.adb", docs / "hmi_manual.md"
                body.write_text(body.read_text().replace("Counter := Counter + 1;",
                                                         "Counter := Counter + 1;\n      Log (\"id\");"))
                manual.write_text(manual.read_text().replace("30 days", "60 days"))
                assert await anyio.to_thread.run_sync(run, cfg) == 0
                callers = await call(session, "callers", name="logger.log")
                assert [i["id"] for i in callers["items"]] == ["logger.next_id", "tracks.delete_track",
                                                               "tracks.update"]
                hit = (await call(session, "search", query="keep logs", k=1, document="hmi_manual.md"))["results"][0]
                assert "60 days" in hit["text"]

    anyio.run(client)
    log = errlog.read_text()
    assert "MCP server over" in log and "27 tools" in log                     # logs go to stderr


def test_serve_logs_how_it_reaches_the_model(toml, monkeypatch, caplog):
    """The client gives the server its own environment: the log says whether it has the
    proxy, and which variables of atlas.toml it lacks."""
    import logging
    from atlas.mcp import _log_connection
    monkeypatch.delenv("TEST_PROXY", raising=False)
    path = toml('[llm]\nbase_url = "https://gateway/v1"\n[network]\nproxy = "${TEST_PROXY:-}"\nno_proxy = ""\n')
    with caplog.at_level(logging.INFO, logger="atlas.mcp"):
        _log_connection(load_config(path))
    assert "model: https://gateway/v1 without proxy" in caplog.text
    assert any(r.levelno == logging.WARNING and "TEST_PROXY of atlas.toml not set" in r.getMessage()
               for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv("TEST_PROXY", "http://jane:Secret1@proxy:8080")
    with caplog.at_level(logging.INFO, logger="atlas.mcp"):
        _log_connection(load_config(path))
    assert "model: https://gateway/v1 through the proxy http://***@proxy:8080" in caplog.text
    assert "Secret1" not in caplog.text and not any(r.levelno == logging.WARNING for r in caplog.records)


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


def test_the_judge_of_atlas_develop_for_an_editor(toml, tmp_path, monkeypatch):
    """run_tests and judge_changes over the Python project of test_develop: by test, how far
    it gets; after a change, what changed, whether it compiles and which tests moved."""
    pytest.importorskip("mcp")
    import anyio
    from atlas.mcp import create_server, default_tools
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
    assert not {"run_tests", "judge_changes"} & set(default_tools(cfg))         # they run code: only if asked for
    cfg = load_config(toml(code + '[mcp]\ntools = ["read_file", "run_tests", "judge_changes"]\n'))
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


def test_serve_turned_off(toml):
    pytest.importorskip("mcp")
    from atlas.cli import main
    with pytest.raises(SystemExit) as e:
        main(["serve", str(toml("[mcp]\nenabled = false\n"))])
    assert "turned off" in str(e.value.code)
