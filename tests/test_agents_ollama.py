"""ADK agents against a local Ollama: real tool calling over the sample data.

Skipped unless the [agents] extra (google-adk) is installed and the Ollama of atlas.dev.toml
is running with its [agents] model pulled. The model is AtlasLlm, over atlas.llm. Run only these with
`pytest -m ollama`, or leave them out with `pytest -m "not ollama"`.
"""
import asyncio
import json
import re
import urllib.request
from pathlib import Path

import pytest

from atlas.config import load_config
from atlas.doors import DatabaseAdapter, normalize_id

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ("Which system requirement (SSS) does SRS-0004 satisfy, and in which Ada files "
            "is SRS-0004 implemented and tested?")
SAMPLE_ADA_FILES = {"tracks.ads", "tracks.adb", "logger.ads", "logger.adb", "test_tracks.adb"}

pytestmark = [pytest.mark.ollama,                      # noise from ADK imports:
              pytest.mark.filterwarnings("ignore:.*EXPERIMENTAL.*:UserWarning"),
              pytest.mark.filterwarnings("ignore:.*ReadOnly.*:UserWarning")]


@pytest.fixture(scope="module")
def ollama():
    """(base_url, api_key, model) of atlas.dev.toml, or skip if it cannot be used."""
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    cfg = load_config(ROOT / "atlas.dev.toml")
    base_url, model = cfg["llm"]["base_url"], cfg["agents"]["model"]
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=2) as r:
            models = {m["id"] for m in json.load(r)["data"]}
    except OSError as e:
        pytest.skip(f"Ollama is not running at {base_url} ({e})")
    if model not in models and f"{model}:latest" not in models:
        pytest.skip(f"model {model} is not in Ollama (ollama pull {model})")
    return base_url, cfg["llm"]["api_key"], model


def _tools(doors_config: dict, ada_root: Path) -> list:
    adapter = DatabaseAdapter(doors_config)
    requirements = {r.id: r for m in adapter.modules() for r in adapter.requirements(m)}
    links = [link for m in adapter.modules() for link in adapter.links(m)]
    adapter.close()

    def get_requirement(req_id: str) -> dict:
        """Returns the text, module and links (outgoing and incoming) of a DOORS requirement."""
        rid = normalize_id(req_id)
        r = requirements.get(rid)
        if r is None:
            return {"error": f"{req_id} does not exist"}
        return {"id": r.id, "module": r.module, "text": r.text,
                "links_out": [{"type": l.type, "target": l.target} for l in links if l.source == rid],
                "links_in": [{"type": l.type, "source": l.source} for l in links if l.target == rid]}

    def trace(req_id: str) -> dict:
        """Finds the 'Req:' tags in the Ada code that cite the requirement; returns file and line."""
        rid = normalize_id(req_id)
        references = []
        for f in sorted(ada_root.rglob("*.ad[sb]")):
            for n, text in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "Req:" in text and re.search(rf"\b{re.escape(rid)}\b", text):
                    references.append({"file": f.relative_to(ada_root).as_posix(), "line": n,
                                       "kind": "test" if "tests" in f.parts else "source"})
        return {"req_id": rid, "references": references}

    return [get_requirement, trace]


TRACE_INSTRUCTION = ("Answer traceability questions using ONLY the tools. Cite the exact "
                     "requirement IDs and file:line references. Be brief.")
EDIT_INSTRUCTION = ("You change code with the tools. Find the code with grep, read it with read_file, "
                    "then change it with edit_file. Be brief.")


def _model(ollama):
    from atlas.agents.model import AtlasLlm
    from atlas.llm import NetConfig, ServiceConfig
    base_url, api_key, model_name = ollama
    # gemma4 reasons by default. With reasoning on, 3 out of 10 runs ended with an empty
    # or invented answer; with it off, 5 out of 5 were right and about 4x faster.
    return AtlasLlm.from_service(ServiceConfig(base_url=base_url, model=model_name, api_key=api_key,
                                               reasoning_effort="none", net=NetConfig(proxy="", no_proxy="*")))


async def _ask(model, tools: list, question: str, instruction: str = TRACE_INSTRUCTION) -> tuple[list[str], str]:
    """Runs the question through an LlmAgent; returns (tools called, final answer)."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(name="assistant", model=model, tools=tools, instruction=instruction)
    runner = InMemoryRunner(agent=agent, app_name="atlas-test")
    session = await runner.session_service.create_session(app_name="atlas-test", user_id="test")
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls, answer = [], ""
    async for event in runner.run_async(user_id="test", session_id=session.id, new_message=message):
        for part in (event.content.parts if event.content else None) or []:
            if part.function_call:
                calls.append(part.function_call.name)
            elif part.text and not part.thought and event.is_final_response():
                answer += part.text
    return calls, answer


def test_agent_traces_a_requirement_with_tools(ollama, toml, samples):
    tools = _tools(load_config(toml())["sources"]["doors"], samples / "ada")
    calls, answer = asyncio.run(asyncio.wait_for(_ask(_model(ollama), tools, QUESTION), timeout=300))

    assert {"get_requirement", "trace"} <= set(calls), calls
    assert "SSS-0005" in answer, answer
    cited = set(re.findall(r"\w+\.ad[sb]", answer))
    assert {"tracks.ads", "test_tracks.adb"} <= cited, answer
    assert cited <= SAMPLE_ADA_FILES, f"cites files that do not exist: {answer}"


def test_agent_edits_code_with_the_file_tools(ollama, samples, tmp_path, monkeypatch):
    """The file tools of atlas.code in the hands of gemma4: a precise change and nothing else.
    Before edit_file tolerated escaped quotes and other indentation, the model found the
    line but looped until the timeout; with that tolerance, 5 out of 5 in about 20 s."""
    import shutil
    from atlas.code import CodeTools
    shutil.copytree(samples / "ada", tmp_path / "ada")
    monkeypatch.chdir(tmp_path)
    before = {f: f.read_text() for f in (tmp_path / "ada").rglob("*.ad?")}
    tools = CodeTools(["ada"])
    task = 'In the Ada project, change the log message of Delete_Track from "delete" to "track deleted". ' \
           "Change nothing else."
    calls, _ = asyncio.run(asyncio.wait_for(
        _ask(_model(ollama), [tools.list_files, tools.grep, tools.read_file, tools.edit_file], task,
             EDIT_INSTRUCTION), timeout=300))

    assert "edit_file" in calls, calls
    tracks = tmp_path / "ada" / "src" / "tracks.adb"
    assert tracks.read_text() == before[tracks].replace('Logger.Log ("delete");', 'Logger.Log ("track deleted");')
    assert all(f.read_text() == text for f, text in before.items() if f != tracks)


def test_agent_answers_with_the_tools_of_atlas_serve(ollama, toml, samples, docs_samples, tmp_path):
    """gemma4 with only the tools of `atlas serve`, over MCP (what OpenCode or VS Code do), on
    a workspace processed with the embeddings of Ollama. Without reasoning, 12 of 12 answers
    to four questions (traceability, impact, documents, a section) were right, in 4-37 s."""
    import shutil
    import sys
    pytest.importorskip("mcp")
    pytest.importorskip("tree_sitter_ada")
    pytest.importorskip("markitdown")
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    from mcp import StdioServerParameters
    from atlas.cli import run
    base_url, api_key, _ = ollama
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(docs_samples / "spec_with_bookmarks.pdf", docs)
    dev = load_config(ROOT / "atlas.dev.toml")
    cfg = load_config(toml(f'''
[llm]
base_url = "{base_url}"
api_key = "{api_key}"
model = "{dev["llm"]["model"]}"
[embeddings]
model = "{dev["embeddings"]["model"]}"
[network]
proxy = ""
no_proxy = "*"
[[sources.docs]]
name = "docs"
paths = ["{docs.as_posix()}"]
analysis = "regex"
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
'''))
    assert run(cfg) == 0

    async def ask() -> tuple[list[str], str]:
        with open(tmp_path / "server.log", "w") as errlog:
            toolset = McpToolset(connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(command=sys.executable, cwd=str(ROOT),
                                                    args=["-m", "atlas.cli", "serve", str(cfg.path)]),
                timeout=60), errlog=errlog)
            try:
                return await _ask(_model(ollama), [toolset], QUESTION.replace("Ada files", "Ada subprograms"))
            finally:
                await toolset.close()

    calls, answer = asyncio.run(asyncio.wait_for(ask(), timeout=300))
    assert "trace" in calls, calls          # it gives the DOORS links too: get_requirement is optional
    assert "SSS-0005" in answer and "delete_track" in answer and "test_tracks" in answer, answer
    cited = set(re.findall(r"\b(?:tracks|logger|test_tracks)\.\w+", answer.lower()))
    assert cited <= {"tracks.delete_track", "tracks.update", "test_tracks.test_tracks", "logger.log",
                     "logger.next_id", "tracks.adb", "tracks.ads", "test_tracks.adb"}, answer


def test_the_assistant_of_atlas(ollama, toml, samples):
    """The built-in assistant (atlas/agents/builtin/assistant.md) with its own tools, as
    `atlas agent assistant` runs it, over the DOORS and the graph of the samples."""
    pytest.importorskip("tree_sitter_ada")
    import contextlib
    import io
    from atlas.agents.runtime import run_agent
    from atlas.cli import run
    base_url, api_key, model = ollama
    cfg = load_config(toml(f'''
[llm]
base_url = "{base_url}"
api_key = "{api_key}"
model = "{model}"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "code", "graph"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    res = run_agent("assistant", QUESTION.replace("Ada files", "Ada subprograms"), cfg)
    assert "trace" in [c["name"] for c in res["calls"]], res
    assert "SSS-0005" in res["answer"] and "delete_track" in res["answer"].lower(), res["answer"]
    assert "test_tracks" in res["answer"].lower(), res["answer"]


def test_requirement_verification_finds_the_deadline_that_differs(ollama, toml, docs_samples, tmp_path):
    """The verification of a requirement against the documents: SRS-0001 says 499 ms in DOORS
    and 301 ms in the specification. gemma4 found it and cited both, in about 35 s."""
    pytest.importorskip("markitdown")
    import contextlib
    import io
    import shutil
    from atlas.agents.verify import verify
    from atlas.cli import run
    from atlas.index import Index
    base_url, api_key, model = ollama
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(docs_samples / "spec_with_bookmarks.pdf", docs)
    dev = load_config(ROOT / "atlas.dev.toml")
    cfg = load_config(toml(f'''
[llm]
base_url = "{base_url}"
api_key = "{api_key}"
model = "{model}"
[embeddings]
model = "{dev["embeddings"]["model"]}"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "docs", "graph", "index"]
[[sources.docs]]
name = "specs"
paths = ["{docs.as_posix()}"]
analysis = "regex"
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    res = verify("SRS-0001", cfg, index=Index.from_config(cfg))
    assert res["verdict"] == "inconsistent", res
    assert any("499" in i["official"] and "301" in i["document"] and "spec_with_bookmarks" in i["source"]
               for i in res["issues"]), res
