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
    from atlas.mcp import create_server, default_tools, graph_instructions, tool_names
    plugins.load_plugins()
    assert default_tools(load_config(toml())) == ["search", "get_requirement", "trace", "matrix", "gaps",
                                                  "list_requirements", "neighbors", "build_context"]  # only DOORS
    assert default_tools(load_config(toml('[pipeline]\nsteps = ["doors"]\n'))) == ["get_requirement"]   # no index

    code = f'[sources.code]\nroots = ["{tmp_path.as_posix()}"]\n'
    docs = f'[[sources.docs]]\nname = "d"\npaths = ["{tmp_path.as_posix()}"]\n'
    cfg = load_config(toml(code + docs))
    names = tool_names(cfg)                       # with code, the profile of an editor
    assert names == ["list_documents", "search", "get_section", "get_requirement", "find_symbol", "impact",
                     "read_source", "trace", "trace_code", "build_context", "context_at", "similar_requirements", "read_file"]
    checked = load_config(toml(code + 'check_command = ["gcc", "-c", "{file}"]\n' + docs))
    assert tool_names(checked)[-1] == "check_code"
    names = tool_names(load_config(toml(code + docs + '[mcp]\nprofile = "all"\n')))
    assert names[:5] == ["list_documents", "search", "get_section", "get_requirement", "ask_docs"]
    assert {"find_symbol", "impact", "trace", "trace_code", "matrix", "gaps", "build_context",
            "read_file"} <= set(names)
    assert not {"edit_file", "write_file", "grep"} & set(names)              # knowledge; the editor edits
    graph = graph_instructions(names)
    assert "Usual flow: find_symbol -> get_node -> callers / callees / trace_code / impact -> read_source." in graph
    assert graph.endswith("; `matrix` and `gaps`, the traceability of a DOORS module and what it lacks.")
    names = tool_names(load_config(toml(code + docs + '[mcp]\nprofile = "trace"\n')))
    assert names == ["search", "get_requirement", "trace", "matrix", "gaps", "list_requirements", "neighbors"]
    assert graph_instructions(names).endswith("in lower case. `trace` gives everything about a requirement (DOORS, "
                                              "documents, code, tests); `matrix` and `gaps`, the traceability of a "
                                              "DOORS module and what it lacks.")         # no flow without code
    assert tool_names(load_config(toml())) == default_tools(load_config(toml()))     # without code, all
    with pytest.raises(ConfigError, match="profile must be one of editor, trace, all"):
        load_config(toml('[mcp]\nprofile = "small"\n'))
    server = create_server(cfg)
    tools = _tools(server)
    assert list(tools) == tool_names(cfg)
    assert tools["impact"]["inputSchema"]["required"] == ["name"]
    assert tools["trace_code"]["title"] == "Trace code"
    assert tools["impact"]["annotations"] == {"readOnlyHint": True, "destructiveHint": False,
                                              "idempotentHint": True, "openWorldHint": False}
    assert tools["impact"]["description"].startswith("What changing a code element or a requirement affects.")
    graph = next(p for p in server.instructions.split("\n\n") if p.startswith("Graph of"))
    assert graph == ("Graph of code, requirements and documents: code identifiers are `unit.name` in lower case. "
                     "Usual flow: find_symbol -> trace_code / impact -> read_source. `trace` gives everything about a "
                     "requirement (DOORS, documents, code, tests); `trace_code`, the requirements and tests of a code "
                     "element; `impact`, what a change affects.")      # no get_node, callers, matrix or gaps
    assert "edit_file" not in server.instructions
    assert "`build_context` gives in one call" in server.instructions
    assert "`attribute` (\"status=Approved" in server.instructions                 # the filters of search
    assert {"attribute", "module", "chapter"} <= set(tools["search"]["inputSchema"]["properties"])
    assert "status=Approved;verification=Test" in tools["search"]["description"]

    # a list of its own, writing tools included; repeated names count once
    server = create_server(load_config(toml(code + '[mcp]\ntools = ["search", "edit_file", "search", "ask_atlas"]\n')))
    tools = _tools(server)
    assert list(tools) == ["search", "edit_file", "ask_atlas"]
    assert "`ask_atlas` asks the assistant" in server.instructions
    assert tools["edit_file"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    assert tools["ask_atlas"]["annotations"] == {"readOnlyHint": True, "destructiveHint": False}   # the model
    with pytest.raises(ConfigError, match=r"unknown tools \['graph_query'\]"):
        create_server(load_config(toml('[mcp]\ntools = ["search", "graph_query"]\n')))
    with pytest.raises(ConfigError, match="disabled"):
        create_server(load_config(toml('[mcp]\ntools = ["search"]\n[plugins]\ndisabled = ["tool:search"]\n')))
    names = tool_names(load_config(toml(code + '[plugins]\ndisabled = ["tool:impact"]\n')))
    assert "impact" not in names and "trace_code" in names
    with pytest.raises(ConfigError, match="turned off"):
        create_server(load_config(toml("[mcp]\nenabled = false\n")))


def test_the_instructions_name_every_tool_and_the_traceability_of_the_project(toml, samples):
    """Each tool, offered alone, has its paragraph in the instructions (the compiler and the
    language server had none), the one of the graph names no other graph tool, and only the
    tools that write or run code are not read-only."""
    pytest.importorskip("mcp")
    import re
    from atlas.mcp import INSTRUCTIONS, create_server, graph_instructions, tool_groups
    from atlas.plugins import Requirement
    from atlas.store import Store
    plugins.load_plugins()
    code = f'[sources.code]\nroots = ["{(samples / "ada").as_posix()}"]\nlanguages = ["ada"]\n'
    groups = tool_groups()
    for name in plugins.available("tool"):
        cfg = load_config(toml(f'{code}[mcp]\ntools = ["{name}"]\n'))
        server = create_server(cfg)
        paragraphs = [INSTRUCTIONS[g] for g, tools in groups.items() if name in tools]
        assert paragraphs and all(p in server.instructions for p in paragraphs), name
        if name in groups.get("graph", ()):
            graph = graph_instructions([name])
            assert graph in server.instructions, name
            assert not [t for t in groups["graph"] if t != name and re.search(rf"\b{t}\b", graph)], name
        tool = plugins.get("tool", name)
        assert _tools(server)[name]["annotations"]["readOnlyHint"] is (not tool.writes), name
    assert "`run_program` runs the program" in create_server(load_config(
        toml(code + '[mcp]\ntools = ["run_program"]\n'))).instructions

    # how this project cites requirements, its IDs and modules, and the steps of a change
    cfg = load_config(toml(code))
    store = Store(cfg.workspace)
    store.replace_module("SRS_TMS", [Requirement("SRS-0004", "Delete.", "SRS_TMS")], [], {"SRS-0004": "h"})
    text = create_server(cfg).instructions
    assert "Traceability of this project: The code cites the requirements it implements in a comment with the " \
           "tag `Req:` (`-- Req: SRS-0004`)" in text
    assert "Requirement IDs match `^(?:SRS|SSS)-\\d{4}$`. DOORS modules: SRS_TMS." in text
    assert "A change: `context_at` or `build_context` of the code to change -> edit it -> compile it -> run the " \
           "tests that `trace` gives for its requirements." in text
    text = create_server(load_config(toml(code.replace("languages", 'req_regex = "@req (.+)"\nlanguages') +
                                          '[mcp]\ntools = ["read_file", "check_code", "run_test"]\n'))).instructions
    assert "in comments that match `@req (.+)`" in text
    assert "A change: read the code to change -> edit it -> `check_code` the file until it has no errors -> " \
           "`run_test` its tests." in text


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
[mcp]
profile = "all"
'''))
    assert run(cfg) == 0
    return cfg, ada, docs


def test_atlas_serve_over_stdio_follows_atlas_run(project, tmp_path, capsys):
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
    assert "MCP server over" in log and "29 tools (profile all)" in log                     # logs go to stderr

    # every call of the editor is in the usage log of the workspace, without its arguments
    from atlas.cli import usage
    from atlas.usage import summary
    res = summary(cfg.workspace / "atlas.db")
    mcp = {r["name"]: r for r in res["by_name"] if r["origin"] == "mcp"}
    assert mcp["search"]["calls"] == 2 and mcp["get_section"]["calls"] == 2 and mcp["callers"]["calls"] == 2
    assert mcp["read_file"]["errors"] == 1 and mcp["get_requirement"]["errors"] == 1   # outside the roots; SRS-004
    assert mcp["ask_docs"]["requests"] >= 2 and mcp["ask_docs"]["completion_tokens"] > 0
    assert mcp["search"]["embedding_tokens"] > 0 and res["answers"] == {"ask_docs": {"answered": 1}}
    import sqlite3
    assert sqlite3.connect(cfg.workspace / "atlas.db").execute(
        "SELECT COUNT(*) FROM usage WHERE args IS NOT NULL").fetchone() == (0,)    # counts: no arguments
    assert usage(cfg, since="7d") == 0
    out = capsys.readouterr().out
    assert "mcp    search" in out and "ask_docs" in out and "1 answered" in out and "requests" in out


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


def test_serve_turned_off(toml):
    pytest.importorskip("mcp")
    from atlas.cli import main
    with pytest.raises(SystemExit) as e:
        main(["serve", str(toml("[mcp]\nenabled = false\n"))])
    assert "turned off" in str(e.value.code)
