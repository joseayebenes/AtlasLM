"""atlas eval without a model: the search cases ranked in the index (hybrid and lexical) and
the graph cases, over the samples processed with the fake API. The agent cases are in
test_agents.py."""
import contextlib
import io
import json
from pathlib import Path

import pytest

from atlas.config import ConfigError, load_config

SET = Path(__file__).resolve().parents[1] / "examples" / "eval" / "retrieval.yaml"


@pytest.fixture
def indexed(toml, samples, docs_samples, fake):
    """The samples as `atlas run` leaves them: DOORS, code, documents, graph and index."""
    pytest.importorskip("tree_sitter_ada")
    pytest.importorskip("pymupdf")
    from atlas.cli import run
    url, _ = fake
    cfg = load_config(toml(f'''
[llm]
base_url = "{url}"
model = "fake"
[embeddings]
model = "embeddinggemma"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "code", "docs", "graph", "index"]
[[sources.docs]]
name = "standards"
paths = ["{docs_samples.as_posix()}"]
patterns = ["*.pdf"]
analysis = "regex"
[[sources.docs]]
name = "manuals"
paths = ["{docs_samples.as_posix()}"]
patterns = ["*.md"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    return cfg


def test_cases_of_three_kinds(tmp_path):
    from atlas.agents.evaluate import load_cases
    cases = {c["id"]: c for c in load_cases(SET)}
    assert cases["search-logs-30-days"]["kind"] == "search" and cases["search-doors-deadline"]["kinds"] == ["requirement"]
    assert cases["graph-impact-log"]["kind"] == "graph" and cases["graph-impact-log"]["query"] == "impact"
    bad = tmp_path / "bad.yaml"
    for text in ("- id: a\n  expect: x\n",                                     # nothing to ask
                 "- id: a\n  search: x\n  trace: SRS-0001\n  expect: x\n",     # two things
                 "- id: a\n  matrix: SRS_TMS\n  expect: x\n"):                 # not a query of the set
        bad.write_text(text)
        with pytest.raises(ConfigError, match="one of question, search or a query of the graph"):
            load_cases(bad)


def test_search_and_graph_are_scored_without_a_model(indexed, fake_stats):
    from atlas.agents.evaluate import evaluate_retrieval, load_cases
    cases = load_cases(SET)
    before = fake_stats()
    res = evaluate_retrieval(cases, indexed)
    after = fake_stats()
    searches = [c for c in cases if c["kind"] == "search"]
    # no chat; one request of embeddings per hybrid search, none for the lexical ones
    assert after["chat"] == before["chat"] and after["embeddings"] - before["embeddings"] == len(searches)

    assert res["graph"] == {"passed": 10, "total": 10, "rate": 1.0}
    assert res["search"]["cases"] == len(searches) == 20
    assert set(res["search"]["lexical"]) == {"hit@1", "hit@3", "hit@5", "hit@10", "mrr"}
    by_id = {r["id"]: r for r in res["results"]}
    for case in ("search-id-srs-0102", "search-logs-30-days", "search-doors-deadline", "search-section-logging",
                 "search-code-remove-track"):
        assert by_id[case]["lexical"]["rank"] == 1, case
    assert by_id["search-id-srs-0102"]["lexical"]["top"][0] == "hmi_manual.md › 2 Alarms (requirement SRS-0102)"

    # a case is found at the position of its last expected text, and not found if one is missing
    wrong = [{"id": "two", "kind": "search", "search": "SRS-0102 alarm", "expect": ["SRS-0102", "no such text"]},
             {"id": "none", "kind": "graph", "query": "trace", "trace": "SRS-9999", "expect": ["SRS-9999"]}]
    res = evaluate_retrieval(wrong, indexed)
    two, none = res["results"]
    assert two["lexical"]["rank"] is None and two["lexical"]["ranks"]["SRS-0102"] == 1
    assert res["search"]["lexical"] == {"hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "hit@10": 0.0, "mrr": 0.0}
    assert none["passed"] is False and "error" in none


def test_atlas_eval_with_only_retrieval_needs_no_agent(indexed, monkeypatch, capsys):
    from atlas.cli import evaluate
    monkeypatch.setitem(indexed["agents"], "eval", {"sets": [str(SET)]})
    assert evaluate(indexed) == 0
    out = capsys.readouterr().out
    assert "ok   graph-impact-log  impact Logger.Log" in out
    assert "search, lexical: hit@1" in out and "graph: 10 of 10 right (100%)" in out and "agents:" not in out
    report = json.loads((indexed.workspace / "reports" / "eval-retrieval.json").read_text())
    assert set(report) == {"retrieval"} and report["retrieval"]["graph"]["passed"] == 10


DEVELOP = Path(__file__).resolve().parents[1] / "examples" / "eval" / "develop.yaml"
CALC = Path(__file__).resolve().parents[1] / "examples" / "develop" / "calc"
SOLVED = '''def add(a, b):
    return a + b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''


class StubClient:
    """Scripted turns, as in test_develop: a dict is a turn of tool calls, a string the answer."""

    def __init__(self, turns):
        from types import SimpleNamespace
        self.turns, self.ns = list(turns), SimpleNamespace
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        ns, turn = self.ns, self.turns.pop(0)
        if isinstance(turn, dict):
            calls = [ns(id=f"c{i}", type="function", function=ns(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = ns(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = ns(content=turn, tool_calls=None), "stop"
        return ns(choices=[ns(message=message, finish_reason=finish)],
                  usage=ns(prompt_tokens=50, completion_tokens=5, total_tokens=55, cost=0.001))


def _write(content):
    return [{"read_file": {"path": "src/calc.py"}}, {"write_file": {"path": "src/calc.py", "content": content}},
            "Done."]


def test_develop_cases(tmp_path):
    from atlas.agents.evaluate import load_cases
    case = load_cases(DEVELOP)[0]
    assert (case["kind"], case["develop"], case["from"], case["expect_tests_passed"]) == (
        "develop", "examples/develop/calc/plan.toml", "examples/develop/calc/start", 2)
    bad = tmp_path / "bad.yaml"
    for text, error in (("- id: a\n  develop: plan.toml\n  expect_tests_passed: 2\n", "has id, develop, from"),
                        ("- id: a\n  develop: p\n  from: s\n  expect_tests_passed: 2\n  expect: [x]\n", "not \\['expect'\\]"),
                        ("- id: a\n  develop: p\n  from: s\n  expect_tests_passed: all\n", "integers"),
                        ("- id: a\n  search: x\n  expect: x\n  runs: 2\n", "unknown keys \\['runs'\\]")):
        bad.write_text(text)
        with pytest.raises(ConfigError, match=error):
            load_cases(bad)


def test_a_develop_case_runs_its_plan_on_a_copy_of_its_start(toml, tmp_path):
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from atlas.agents.evaluate import evaluate_sets
    cfg = load_config(toml())
    cases = tmp_path / "develop.yaml"
    cases.write_text(f"- id: calc\n  develop: {(CALC / 'plan.toml').as_posix()}\n  from: {(CALC / 'start').as_posix()}\n"
                     f"  expect_tests_passed: 2\n")
    add = SOLVED.split("\n\n\n")[0] + "\n"
    out = evaluate_sets(cfg, client=StubClient(_write(add) + _write(SOLVED)), sets=[str(cases)])
    res = out["develop"]["develop"]
    run = res["results"][0]
    assert (run["passed"], run["tests_passed"], run["tests"]) == (True, 2, 2) and "model" in run
    assert run["edits"] == {"made": 2, "failed": 0} and run["compiles"]["failed"] == 0 and run["calls"] == 4
    assert run["cost"] == pytest.approx(0.006) and run["agent_runs"] == 2 and run["for_a_person"] == []
    assert res["by_case"] == {"calc": "1/1"} and res["totals"]["tests_passed"] == 2
    assert (CALC / "start" / "src" / "calc.py").read_text().count("return 0") == 1    # the start is not touched
    reports = cfg.workspace / "reports"
    assert json.loads((reports / "eval-develop.json").read_text())["develop"]["passed"] == 1
    assert "**2 of 2 tests pass.**" in (reports / "eval-develop" / "calc-1.md").read_text()


def test_atlas_eval_of_the_develop_set_with_the_fake_server(toml, fake, monkeypatch, capsys):
    """The model of the fake server answers without tools: nothing changes, and the report says so."""
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from atlas.cli import evaluate
    url, _ = fake
    cfg = load_config(toml(f'''
[llm]
base_url = "{url}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
'''))
    monkeypatch.setattr(cfg, "path", DEVELOP.parents[2] / "atlas.toml")   # the paths of the set, from the repository
    assert evaluate(cfg, set=str(DEVELOP)) == 0
    out = capsys.readouterr().out
    assert "FAIL calc-from-start #1" in out and "0 of 2 tests (expected 2)" in out
    assert "develop: 0 of 1 runs reached their tests" in out
    report = json.loads((cfg.workspace / "reports" / "eval-develop.json").read_text())["develop"]
    assert report["results"][0]["model"] == "fake" and report["results"][0]["tests"] == 2
