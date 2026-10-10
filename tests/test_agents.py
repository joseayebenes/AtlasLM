"""atlas.agents: agents defined in Markdown files, run with ADK over atlas.llm."""
import contextlib
import io
import json
from types import SimpleNamespace

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
    assert found["assistant"].tools == ["search", "get_requirement"]
    assert found["assistant"].instruction == "Only search."
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


# --- running them with ADK over AtlasLlm ---------------------------------------------------

class StubClient:
    """An OpenAI client that answers with scripted turns and keeps the requests: a dict is a
    turn of tool calls ({name: args}), a string the final answer."""

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        turn = self.turns.pop(0)
        if isinstance(turn, dict):
            arguments = lambda a: a if isinstance(a, str) else json.dumps(a)
            calls = [SimpleNamespace(id=f"call_{i}", type="function",
                                     function=SimpleNamespace(name=n, arguments=arguments(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = SimpleNamespace(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = SimpleNamespace(content=turn, tool_calls=None), "stop"
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110))


@pytest.fixture
def workspace(toml, samples):
    """DOORS and the graph of the Ada samples, as `atlas run` leaves them."""
    pytest.importorskip("tree_sitter_ada")
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from atlas.cli import run
    cfg = load_config(toml(f'''
[pipeline]
steps = ["doors", "code", "graph"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    return cfg


def test_an_agent_answers_with_its_tools_through_atlas_llm(workspace, monkeypatch):
    from atlas.agents.runtime import run_agent
    monkeypatch.chdir(workspace.path.parent)
    client = StubClient([{"trace": {"req_id": "SRS-0004"}}, "SRS-0004 is implemented by tracks.delete_track."])
    res = run_agent("assistant", "Who implements SRS-0004?", workspace, client=client)
    assert res == {"answer": "SRS-0004 is implemented by tracks.delete_track.", "stopped": False,
                   "calls": [{"name": "trace", "args": {"req_id": "SRS-0004"}}]}

    first, second = client.requests
    assert first["model"] == "gemma4" and first["reasoning_effort"] == "none"      # [llm], and the agent's file
    assert first["messages"][0]["role"] == "system" and "engineering project" in first["messages"][0]["content"]
    assert first["messages"][-1] == {"role": "user", "content": "Who implements SRS-0004?"}
    tools = {t["function"]["name"]: t["function"] for t in first["tools"]}
    assert list(tools) == load_definitions(workspace)["assistant"].tools
    assert tools["trace"]["parameters"]["required"] == ["req_id"]
    assert tools["trace"]["description"].startswith("Everything about a requirement ID")
    call, result = second["messages"][-2:]
    assert call["role"] == "assistant" and call["tool_calls"][0]["function"]["name"] == "trace"
    assert result["role"] == "tool" and result["tool_call_id"] == "call_0"
    assert "tracks.delete_track" in result["content"] and "SSS-0005" in result["content"]


def test_max_steps_and_broken_tool_calls(workspace, toml, monkeypatch):
    from atlas.agents import model
    from atlas.agents.runtime import run_agent
    from atlas.cli import main
    cfg = workspace
    cut = '{"req_id": "SRS-0004"'                                # a small model cut the JSON
    client = StubClient([{"trace": cut}, {"trace": cut}, {"trace": cut}, "never"])
    monkeypatch.setitem(cfg["agents"], "assistant", {"max_steps": 2})
    res = run_agent("assistant", "Trace SRS-0004", cfg, client=client)
    assert res["stopped"] and len(client.requests) == 2
    assert res["calls"][0] == {"name": "trace", "args": {"req_id": "SRS-0004"}}

    # atlas agent, over the client of atlas.llm
    stub = StubClient(["Nothing to trace."])
    monkeypatch.setattr(model, "create_client", lambda service: (stub, None))
    with pytest.raises(SystemExit) as end:
        main(["agent", "assistant", "Hello", str(workspace.path)])
    assert end.value.code == 0 and stub.requests[0]["messages"][-1]["content"] == "Hello"
    with pytest.raises(SystemExit, match="there is no agent 'nobody'"):
        main(["agent", "nobody", "Hello", str(workspace.path)])


def test_genai_schemas_become_json_schemas():
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from atlas.agents.model import to_tools
    declaration = types.FunctionDeclaration(name="f", description="F.", parameters=types.Schema(
        type=types.Type.OBJECT, required=["x"], properties={
            "x": types.Schema(type=types.Type.STRING, description="an x"),
            "ys": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.INTEGER))}))
    request = LlmRequest(config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[declaration])]))
    assert to_tools(request) == [{"type": "function", "function": {"name": "f", "description": "F.", "parameters": {
        "type": "object", "required": ["x"], "properties": {"x": {"type": "string", "description": "an x"},
                                                            "ys": {"type": "array", "items": {"type": "integer"}}}}}}]


def test_build_context_fits_a_budget(workspace, monkeypatch):
    from atlas.agents.tools import AgentTools
    monkeypatch.chdir(workspace.path.parent)
    plugins.load_plugins()
    context = SimpleNamespace(cfg=workspace, disabled=[])
    tools = plugins.get("tool", "build_context")(context)
    assert isinstance(AgentTools.for_context(context), AgentTools)

    req = tools.run(target="srs-0004")
    assert (req["target"], req["kind"], req["truncated"], req["omitted"]) == ("SRS-0004", "requirement", False, [])
    md = req["markdown"]
    for text in ("## Requirement", "satisfies SSS-0005", "## Code that implements it",
                 "`procedure Delete_Track (I : Integer) is`", "`test_tracks.test_tracks` (cites it)",
                 "Logger.Log (\"delete\");"):
        assert text in md, text
    assert md.index("## Requirement") < md.index("## Code that implements it") < md.index("## Source")

    unit = tools.run(target="tracks")
    assert unit["kind"] == "unit" and "Requirements: SRS-0002, SRS-0003" in unit["markdown"]
    assert "```\n    1 | package Tracks is" in unit["markdown"]
    element = tools.run(target="Delete_Track")
    assert "## Called by" in element["markdown"] and "Uses: tracks.trk_db" in element["markdown"]
    assert "## Exercises" in tools.run(target="test_tracks.test_tracks")["markdown"]

    small = tools.run(target="tracks", budget_tokens=200)
    assert small["truncated"] and small["tokens"] <= 200 and "Spec" in small["omitted"]
    assert "not in the graph" in tools.run(target="nothing")["error"]


# --- verification of requirements against the documentation ---------------------------------

class StubJudge:
    """A JsonLlm that answers every verification with the same judgement and keeps the prompts."""

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def ask_json(self, system, user, schema, name):
        self.prompts.append((system, user))
        return self.answer


@pytest.fixture
def documented(toml, docs_samples, tmp_path):
    """DOORS (doors.sqlite), the specification analysed with regex and the graph."""
    pytest.importorskip("markitdown")
    import shutil
    from atlas.cli import run
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(docs_samples / "spec_with_bookmarks.pdf", docs)
    cfg = load_config(toml(f'''
[pipeline]
steps = ["doors", "docs", "graph"]
[[sources.docs]]
name = "specs"
paths = ["{docs.as_posix()}"]
analysis = "regex"
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    return cfg


def test_a_requirement_against_its_documentation(documented, capsys):
    from atlas.agents.verify import evidence, verify, verify_all
    from atlas.cli import verify as verify_command
    from atlas.graph import KnowledgeGraph
    g = KnowledgeGraph.from_config(documented)
    ev = evidence("SRS-0001", g)
    assert ev["official"]["module"] == "SRS_TMS" and ev["linked"] == [
        {"id": "SSS-0002", "type": "satisfies", "text": "The mission system shall provide capability 2."}]
    assert ev["passages"][0]["source"] == "spec_with_bookmarks.pdf › 1.1 Function group 1.1"
    assert "within 301 ms" in ev["passages"][0]["text"] and ev["values_differ"][0]["document"] == ["0", "1.1.1", "301"]

    judge = StubJudge({"verdict": "inconsistent", "summary": "The deadlines differ.", "issues": [
        {"type": "value", "official": "within 499 ms", "document": "within 301 ms",
         "source": "spec_with_bookmarks.pdf › 1.1 Function group 1.1", "explanation": "Two deadlines."},
        "not an issue"]})
    res = verify("SRS-0001", documented, llm=judge, graph=g)
    assert res["verdict"] == "inconsistent" and res["issues"][0]["document"] == "within 301 ms"
    assert len(res["issues"]) == 1 and res["sources"] == ["spec_with_bookmarks.pdf › 1.1 Function group 1.1"]
    system, prompt = judge.prompts[0]
    assert system.startswith("You verify engineering requirements")
    assert "Official text (DOORS): The TMF shall process track event 1 within" in prompt
    assert "[1] spec_with_bookmarks.pdf › 1.1 Function group 1.1\nThe TMF shall process track event 0" in prompt
    assert "Linked: satisfies SSS-0002: The mission system shall provide capability 2." in prompt
    assert verify("SRS-0001", documented, llm=judge, graph=g) == res and len(judge.prompts) == 1    # the cache

    # a whole module; what no document states is not asked
    judge = StubJudge({"verdict": "consistent", "issues": [], "summary": "Same."})
    report = verify_all("SSS_MISSION", documented, llm=judge, graph=g)
    assert report["counts"] == {"consistent": 0, "inconsistent": 0, "no_documentation": 12} and not judge.prompts
    report = verify_all("SRS_TMS", documented, llm=judge, graph=g, workers=4)
    assert report["counts"]["consistent"] == 39 and report["counts"]["inconsistent"] == 1      # SRS-0001, cached
    assert report["asked"] == 39
    assert "## SRS-0001" in (documented.workspace / "reports" / "verify-SRS_TMS.md").read_text()
    assert "neither a requirement nor a module" in verify_all("NOPE", documented, llm=judge, graph=g)["error"]
    assert verify_command(documented, "SSS-0001") == 0 and "1 requirements: 0 inconsistent" in capsys.readouterr().out
