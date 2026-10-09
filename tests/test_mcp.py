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
    assert default_tools(load_config(toml())) == ["get_requirement"]          # only [sources.doors]

    code = f'[sources.code]\nroots = ["{tmp_path.as_posix()}"]\n'
    docs = f'[[sources.docs]]\nname = "d"\npaths = ["{tmp_path.as_posix()}"]\n'
    cfg = load_config(toml(code + docs))
    names = tool_names(cfg)
    assert names[:5] == ["list_documents", "search", "get_section", "get_requirement", "ask_docs"]
    assert {"find_symbol", "impact", "trace", "read_file"} <= set(names)
    assert not {"edit_file", "write_file", "grep"} & set(names)              # knowledge; the editor edits
    server = create_server(cfg)
    tools = _tools(server)
    assert list(tools) == names
    assert tools["impact"]["inputSchema"]["required"] == ["name"]
    assert tools["impact"]["annotations"]["readOnlyHint"] is True
    assert tools["impact"]["description"].startswith("Every subprogram that calls `name`, directly or")
    assert "find_symbol -> get_node" in server.instructions and "edit_file" not in server.instructions

    # a list of its own, writing tools included; repeated names count once
    tools = _tools(create_server(load_config(toml(code + '[mcp]\ntools = ["search", "edit_file", "search"]\n'))))
    assert list(tools) == ["search", "edit_file"]
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
                hit = (await call(session, "search", query="SRS-0102 alarm", k=3))["results"][0]
                assert hit["ref"] == "SRS-0102" and hit["cite"].startswith("hmi_manual.md › 2 Alarms")
                section = await call(session, "get_section", document=hit["document"], section=hit["section"])
                assert "raise an alarm when a track is lost" in section["markdown"]
                req = await call(session, "get_requirement", req_id="srs-0004")
                assert req["id"] == "SRS-0004" and req["official"]["module"] == "SRS_TMS"
                assert {"target": "SSS-0005", "type": "satisfies"} in req["official"]["links_out"]
                assert req["documents"][0]["cite"].startswith("spec_with_bookmarks.pdf")
                assert "SRS-0040" in (await call(session, "get_requirement", req_id="SRS-004"))["similar"]
                answer = await call(session, "ask_docs", question="When does the HMI raise an alarm?")
                assert "[1]" in answer["answer"] and answer["sources"][0]["doc"] == "hmi_manual.md"

                # code: graph and files
                traced = await call(session, "trace", req_id="SRS-0004")
                assert [i["id"] for i in traced["traced_by"]["items"]] == ["test_tracks.test_tracks",
                                                                            "tracks.delete_track"]
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
    assert "MCP server over" in log and "21 tools" in log                     # logs go to stderr


def test_serve_turned_off(toml):
    pytest.importorskip("mcp")
    from atlas.cli import main
    with pytest.raises(SystemExit) as e:
        main(["serve", str(toml("[mcp]\nenabled = false\n"))])
    assert "turned off" in str(e.value.code)
