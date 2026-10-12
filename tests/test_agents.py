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
    assert set(builtin) == {"assistant", "requirement_verifier", "impact_analyst", "coverage_auditor", "req_linker",
                            "compliance_checker"}
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
    monkeypatch.setitem(workspace.data, "llm", {"extra_body": {"provider": {"order": ["deepinfra"]}}})
    client = StubClient([{"trace": {"req_id": "SRS-0004"}}, "SRS-0004 is implemented by tracks.delete_track."])
    res = run_agent("assistant", "Who implements SRS-0004?", workspace, client=client)
    assert res == {"answer": "SRS-0004 is implemented by tracks.delete_track.", "stopped": False,
                   "calls": [{"name": "trace", "args": {"req_id": "SRS-0004"}}]}

    first, second = client.requests
    assert first["model"] == "gemma4" and first["reasoning_effort"] == "none"      # [llm], and the agent's file
    assert first["extra_body"] == {"provider": {"order": ["deepinfra"]}}           # [llm] extra_body, as is
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


def test_links_are_proposed_and_the_code_is_not_changed(workspace, samples, capsys):
    from atlas.agents.link import candidates, link, link_all
    from atlas.cli import link as link_command
    from atlas.graph import KnowledgeGraph
    code = {f: f.read_text() for f in (samples / "ada").rglob("*.ad?")}
    g = KnowledgeGraph.from_config(workspace)
    # SRS-0005 refines SRS-0004, which tracks.delete_track implements: a candidate, without a model
    c = candidates("SRS-0005", g)
    assert [x["id"] for x in c["candidates"]] == ["tracks.delete_track"] and c["implemented_by"] == []
    assert c["candidates"][0]["evidence"] == ["implements SRS-0004, linked to SRS-0005 (references)"]
    assert "Removes track I" in c["candidates"][0]["doc"] and "Delete_Track" in c["candidates"][0]["code"]

    judge = StubJudge({"summary": "It deletes tracks.", "links": [
        {"code": "Tracks.Delete_Track", "confidence": "certain", "reason": "It deletes the track."},
        {"code": "tracks.invented", "confidence": "high", "reason": "Not offered."}]})
    res = link("SRS-0005", workspace, llm=judge, graph=g)
    assert res["verdict"] == "proposed" and len(res["links"]) == 1          # what was not offered is dropped
    x = res["links"][0]
    assert (x["code"], x["confidence"], x["tag"]) == ("tracks.delete_track", "low", "-- Req: SRS-0005")
    assert x["tag_at"].endswith("tracks.ads:17")                            # the declaration in the spec
    system, prompt = judge.prompts[0]
    assert system.startswith("You help keep the traceability") and "Official text (DOORS): The TMF shall" in prompt
    assert "Why it is a candidate: implements SRS-0004, linked to SRS-0005 (references)" in prompt
    assert link("SRS-0005", workspace, llm=judge, graph=g) == res and len(judge.prompts) == 1     # the cache

    # a module: its requirements without code; those without candidates are not asked
    judge = StubJudge({"links": [], "summary": "None."})
    report = link_all("SRS_TMS", workspace, llm=judge, graph=g)
    assert report["counts"] == {"proposed": 1, "none": 0, "no_candidates": 35} and report["asked"] == 0
    md = (workspace.workspace / "reports" / "link-SRS_TMS.md").read_text()
    assert "Add `-- Req: SRS-0005` at" in md and "Nothing has been changed in the code" in md
    assert "neither a requirement nor a module" in link_all("NOPE", workspace, llm=judge, graph=g)["error"]
    assert link_command(workspace, "SRS-0005") == 0
    out = capsys.readouterr().out
    assert "SRS-0005 -> tracks.delete_track (low)" in out and "nothing changed in the code" in out
    assert {f: f.read_text() for f in (samples / "ada").rglob("*.ad?")} == code


def test_the_code_of_a_requirement_is_judged_obligation_by_obligation(workspace, capsys):
    from atlas.agents.comply import comply, comply_all, evidence
    from atlas.cli import comply as comply_command
    from atlas.graph import KnowledgeGraph
    g = KnowledgeGraph.from_config(workspace)
    ev = evidence("SRS-0004", g)
    assert ev["text"].startswith("The TMF shall process track event 4") and ev["tests"] == ["test_tracks.test_tracks"]
    [code] = ev["code"]
    assert code["id"] == "tracks.delete_track" and "Logger.Log" in code["source"]
    assert code["calls"] == [{"id": "logger.log", "doc": "Writes a line to the log.\nReq: SRS-0103"}]

    # the verdict follows from the obligations, whatever the model says
    judge = StubJudge({"verdict": "complies", "summary": "Timing cannot be told.", "obligations": [
        {"text": "process track event 4", "status": "met", "evidence": "Trk_DB (I).Id := 0;"},
        {"text": "within 320 ms", "status": "cannot_tell", "evidence": "no timing in the code"},
        {"text": "", "status": "met"}, "not an obligation"]})
    res = comply("SRS-0004", workspace, llm=judge, graph=g)
    assert res["verdict"] == "cannot_tell" and len(res["obligations"]) == 2 and res["code"] == ["tracks.delete_track"]
    system, prompt = judge.prompts[0]
    assert system.startswith("You check whether the code that implements a requirement")
    assert "[tracks.delete_track] (subprogram" in prompt and "It calls: logger.log (Writes a line to the log." in prompt
    assert "Tests that verify or cover it: test_tracks.test_tracks" in prompt
    assert comply("SRS-0004", workspace, llm=judge, graph=g) == res and len(judge.prompts) == 1     # the cache

    # a module: what has no code is not asked; one obligation not met is enough
    judge = StubJudge({"obligations": [{"text": "a", "status": "met", "evidence": "x"},
                                       {"text": "b", "status": "partial", "evidence": "y"}], "summary": "No."})
    report = comply_all("SRS_TMS", workspace, llm=judge, graph=g)
    assert report["counts"] == {"complies": 0, "does_not_comply": 3, "cannot_tell": 1, "no_code": 36}
    assert report["asked"] == 3                                   # SRS-0004 came from the cache
    md = (workspace.workspace / "reports" / "comply-SRS_TMS.md").read_text()
    assert "## SRS-0001: does_not_comply" in md and "- ◐ b: y" in md and "not a proof" in md
    assert "neither a requirement nor a module" in comply_all("NOPE", workspace, llm=judge, graph=g)["error"]
    assert comply_command(workspace, "SRS-0004") == 0
    assert "SRS-0004 (tracks.delete_track): cannot_tell" in capsys.readouterr().out


# --- reports written by an agent from the graph -----------------------------------------------

class StubWriter:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def ask_text(self, system, user):
        self.prompts.append((system, user))
        return self.text


def test_impact_and_coverage_reports(workspace):
    from atlas.agents.reports import coverage_report, impact_report
    writer = StubWriter("**Change**: Logger.Log.")
    res = impact_report("Logger.Log", workspace, llm=writer)
    system, data = writer.prompts[0]
    assert system.startswith("You write the impact analysis") and res["node"] == "logger.log"
    assert "- tracks.delete_track (subprogram, distance 1" in data
    assert "- test_tracks.test_tracks (test, distance 2" in data
    assert "- SRS-0004 (SRS_TMS): The TMF shall process track event 4" in data
    assert "[through tracks.delete_track]" in data
    assert "- SSS-0005 (SSS_MISSION)" in data and "[through SRS-0004]" in data
    assert "- SRS-0103 (not in DOORS)" in data
    written = (workspace.workspace / "reports" / "impact-logger.log.md").read_text()
    assert written.startswith("**Change**: Logger.Log.") and "## Data of the graph" in written
    req = impact_report("SSS-0005", workspace, llm=writer)
    assert "- tracks.delete_track" in writer.prompts[1][1] and req["node"] == "SSS-0005"
    assert "not in the graph" in impact_report("nothing", workspace, llm=writer)["error"]

    writer = StubWriter("**State**: 4 of 40 with code.")
    res = coverage_report("SRS_TMS", workspace, llm=writer)
    system, data = writer.prompts[0]
    assert system.startswith("You audit the traceability") and res["summary"]["requirements"] == 40
    assert res["gaps"]["requirements_without_code"] == 36 and "requirements without tests (39):" in data
    assert "- SRS-0005" in data and "- … and 21 more" in data and "SRS-0040" not in data      # 15 of 36
    assert "- SRS-0040" in (workspace.workspace / "reports" / "coverage-SRS_TMS.md").read_text()
    assert coverage_report("NOPE", workspace, llm=writer)["modules"] == ["SRS_TMS", "SRS_TMS_OLD", "SSS_MISSION"]


def test_ask_atlas_is_the_assistant_as_a_tool(workspace, monkeypatch):
    from atlas.agents import model
    monkeypatch.chdir(workspace.path.parent)
    stub = StubClient([{"trace_code": {"name": "Update"}}, "tracks.update implements SRS-0002 and SRS-0003."])
    monkeypatch.setattr(model, "create_client", lambda service: (stub, None))
    plugins.load_plugins()
    context = SimpleNamespace(cfg=workspace, store=None, disabled=[])
    res = plugins.get("tool", "ask_atlas")(context).run(question="What does Update implement?")
    assert res["answer"] == "tracks.update implements SRS-0002 and SRS-0003."
    assert res["calls"] == [{"name": "trace_code", "args": {"name": "Update"}}]
    assert "SRS-0002" in stub.requests[1]["messages"][-1]["content"]           # the result of trace_code


def test_evaluation_scores_the_answers_without_a_model(workspace, tmp_path, monkeypatch):
    from atlas.agents.evaluate import evaluate, evaluate_sets, load_cases
    monkeypatch.chdir(workspace.path.parent)
    cases_file = tmp_path / "set.yaml"
    cases_file.write_text("- id: trace\n  question: Who implements SRS-0004?\n  expect: [delete_track, SSS-0005]\n"
                          "  tools: [trace]\n- id: wrong\n  question: And SRS-0002?\n  expect: SRS-0002\n")
    cases = load_cases(cases_file)
    assert cases[1] == {"id": "wrong", "question": "And SRS-0002?", "expect": ["SRS-0002"], "tools": [],
                        "agent": "assistant", "kind": "agent"}
    client = StubClient([{"trace": {"req_id": "SRS-0004"}}, "tracks.Delete_Track, through SSS-0005.",
                         "I do not know."])
    res = evaluate(cases, workspace, client=client)
    assert (res["passed"], res["total"], res["rate"], res["by_case"]) == (1, 2, 0.5, {"trace": "1/1", "wrong": "0/1"})
    assert res["results"][1]["missing"] == ["SRS-0002"] and res["results"][1]["missing_tools"] == []
    assert res["results"][0]["calls"] == ["trace"]

    bad = tmp_path / "bad.yaml"
    bad.write_text("- id: a\n  question: q\n")
    with pytest.raises(ConfigError, match="a case has id, expect and one of question"):
        load_cases(bad)
    with pytest.raises(ConfigError, match=r"\[agents.eval\] sets"):
        evaluate_sets(workspace)
    monkeypatch.setitem(workspace["agents"], "eval", {"sets": [str(cases_file)], "runs": 2})
    out = evaluate_sets(workspace, client=StubClient(["delete_track SSS-0005"] * 4))
    assert set(out["set"]) == {"agents"} and out["set"]["agents"]["total"] == 4
    assert (workspace.workspace / "reports" / "eval-set.json").exists()


def test_the_app_for_adk_web(workspace, monkeypatch):
    """adk web and adk run load examples/adk/atlas_agent: an agent of the project, written
    only as a Markdown file, is there without touching the code or the TOML."""
    import importlib.util
    from pathlib import Path
    _agent(workspace.path.parent / "agents", "tracer.md", tools="[trace, gaps, build_context]")

    def load(agent):
        monkeypatch.setenv("ATLAS_CONFIG", str(workspace.path))
        monkeypatch.setenv("ATLAS_AGENT", agent)
        path = Path(__file__).resolve().parents[1] / "examples" / "adk" / "atlas_agent" / "agent.py"
        spec = importlib.util.spec_from_file_location("atlas_adk_agent", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.root_agent

    assert load("assistant").name == "assistant" and len(load("assistant").tools) == 8
    tracer = load("tracer")
    assert tracer.name == "tracer" and [t.__name__ for t in tracer.tools] == ["trace", "gaps", "build_context"]
