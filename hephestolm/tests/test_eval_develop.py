"""The develop cases of an evaluation set, run by hephesto (hephesto/evaluate.py): a plan of
hephesto develop on a copy of its starting folder, with a scripted model or the fake server."""
import json
from pathlib import Path

import pytest

from atlas.config import load_config

ROOT = Path(__file__).resolve().parents[2]
DEVELOP = ROOT / "hephestolm" / "examples" / "develop.yaml"
CALC = ROOT / "hephestolm" / "examples" / "develop" / "calc"
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


def test_a_develop_case_runs_its_plan_on_a_copy_of_its_start(toml, tmp_path):
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from atlas.agents.evaluate import evaluate_sets
    from hephesto.evaluate import RUNNERS
    cfg = load_config(toml())
    cases = tmp_path / "develop.yaml"
    cases.write_text(f"- id: calc\n  develop: {(CALC / 'plan.toml').as_posix()}\n  from: {(CALC / 'start').as_posix()}\n"
                     f"  expect_tests_passed: 2\n")
    add = SOLVED.split("\n\n\n")[0] + "\n"
    out = evaluate_sets(cfg, client=StubClient(_write(add) + _write(SOLVED)), sets=[str(cases)], runners=RUNNERS)
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


def test_hephesto_eval_of_the_develop_set_with_the_fake_server(toml, fake, monkeypatch, capsys):
    """The model of the fake server answers without tools: nothing changes, and the report says so."""
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    from hephesto.cli import evaluate
    url, _ = fake
    cfg = load_config(toml(f'''
[llm]
base_url = "{url}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
'''))
    monkeypatch.setattr(cfg, "path", ROOT / "atlas.toml")   # the paths of the set, from the repository
    assert evaluate(cfg, set=str(DEVELOP)) == 0
    out = capsys.readouterr().out
    assert "FAIL calc-from-start #1" in out and "0 of 2 tests (expected 2)" in out
    assert "develop: 0 of 1 runs reached their tests" in out
    report = json.loads((cfg.workspace / "reports" / "eval-develop.json").read_text())["develop"]
    assert report["results"][0]["model"] == "fake" and report["results"][0]["tests"] == 2
