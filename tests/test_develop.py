"""atlas develop: tasks, checks, tests, undo of what breaks, rounds and what goes to a person.
The agent is scripted (a stub of the OpenAI client); the project is Python, the test real."""
import json
import sys
from types import SimpleNamespace

import pytest

from atlas.config import ConfigError, load_config

pytest.importorskip("google.adk", reason="needs the [agents] extra")

TEST = '''import sys
sys.path.insert(0, "SRC")
import calc
def check(step, got, want):
    if got != want:
        print(f"Step {step}: expected {want}, received {got}")
        sys.exit(1)
check(1, calc.add(1, 2), 3)
check(2, getattr(calc, "mul", lambda a, b: None)(2, 3), 6)
'''
AGENT = """---
name: NAME
tools: [read_file, write_file, edit_file]
max_steps: 6
---
You write the code of src.
"""
PLAN = '''agent = "implementer"
fix_agent = "fixer"
check = ["src/*.py"]
tests = ["tests/test_*.py"]
progress = 'Step (\\d+)'
build_fixes = 1
fix_rounds = 4
stuck_rounds = 2

[[tasks]]
id = "add"
text = "Implement add."

[[tasks]]
text = "Make it faster."
'''
ADD = "def add(a, b):\n    return a + b\n"
MUL = ADD + "\n\ndef mul(a, b):\n    return a * b\n"


class StubClient:
    """Scripted turns, as in test_agents: a dict is a turn of tool calls, a string the answer."""

    def __init__(self, turns):
        self.turns, self.prompts = list(turns), []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        if len(kwargs["messages"]) == 2:                    # the first turn of a run: system and task
            self.prompts.append(kwargs["messages"][1]["content"])
        turn = self.turns.pop(0)
        if isinstance(turn, dict):
            calls = [SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = SimpleNamespace(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = SimpleNamespace(content=turn, tool_calls=None), "stop"
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, total_tokens=55, cost=0.001))


def write(content):
    """A run of the agent that reads src/calc.py, writes `content` and says so."""
    return [{"read_file": {"path": "src/calc.py"}}, {"write_file": {"path": "src/calc.py", "content": content}}, "Done."]


@pytest.fixture
def project(tmp_path, toml, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(TEST.replace("SRC", str(tmp_path / "src")))
    (tmp_path / "agents").mkdir()
    for name in ("implementer", "fixer"):
        (tmp_path / "agents" / f"{name}.md").write_text(AGENT.replace("NAME", name))
    (tmp_path / "plan.toml").write_text(PLAN)
    cfg = load_config(toml(f'''
[agents]
dirs = ["{(tmp_path / 'agents').as_posix()}"]
[sources.code]
roots = ["src", "tests"]
exclude = ["**/__pycache__/**"]
protected = ["tests/**"]
check_command = ["{sys.executable}", "-m", "py_compile", "{{file}}"]
[sources.code.test_commands]
py = ["{sys.executable}", "{{file}}"]
'''))
    return tmp_path, cfg


def test_what_breaks_is_undone_and_what_is_stuck_goes_to_a_person(project):
    from atlas.agents.develop import develop
    base, cfg = project
    client = StubClient(
        write(ADD)                                              # add: the test gets to step 2, kept
        + write("def add(a, b):\n    return a - b\n")          # faster, but wrong: the test fails sooner, undone
        + write("def add(a, b:\n    return a + b\n")           # the retry does not compile
        + ["I cannot fix it."]                                  # nor its build fix: undone
        + [{"read_file": {"path": "src/calc.py"}}, "No idea."] * 2)  # two rounds without progress on the test
    steps = []
    report = develop(base / "plan.toml", cfg, client=client, on_step=steps.append)
    assert [(s["kind"], s["task"], s.get("outcome")) for s in report["steps"]] == [
        ("task", "add", "kept"), ("task", "T02", "undone"), ("task-retry", "T02", "undone"),
        ("build-fix", "T02", "errors remain"), ("round", "tests/test_calc.py", "no progress"),
        ("round", "tests/test_calc.py", "no progress")]
    assert [s["n"] for s in steps] == [1, 2, 4, 3, 5, 6]          # each one when it ends
    assert (base / "src" / "calc.py").read_text() == ADD                    # as the first task left it
    second, retry = report["steps"][1], report["steps"][2]
    assert second["undone"] == ["src/calc.py"]
    assert second["broke"][0].startswith("tests/test_calc.py got to 2 and now got to 1: Step 1: expected 3")
    assert "does not compile" in retry["broke"][0]
    assert report["final"] == {"tests_passed": 0, "tests": 1, "check_errors": [],
                               "failing": {"tests/test_calc.py": "Step 2: expected 6, received None"}}
    first_round, second_round = client.prompts[-2:]
    assert first_round.startswith("The test tests/test_calc.py fails. What it printed:\n\nStep 2: expected 6")
    assert "Earlier attempts" not in first_round and "- No idea. -> no progress" in second_round
    assert "A first attempt at this task was undone" in client.prompts[2] and "now got to 1" in client.prompts[2]
    assert client.prompts[3].startswith("The code does not compile after the last change")
    person = report["for_a_person"]["tests/test_calc.py"]
    assert person["reason"] == "no progress in 2 rounds" and person["attempts"] == ["No idea. -> no progress"] * 2
    assert report["totals"]["calls"] == 8 and report["totals"]["cost"] == pytest.approx(0.014)
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert "**0 of 1 tests pass.**" in md and "## For a person" in md and "## Undone" in md
    assert json.loads((cfg.workspace / "reports" / "develop-plan.json").read_text())["final"]["tests_passed"] == 0
    runs = [json.loads(l) for l in (cfg.workspace / "reports" / "develop-plan.calls.jsonl").read_text().splitlines()]
    assert [r["n"] for r in runs] == [1, 2, 3, 4, 5, 6] and runs[0]["prompt"] == "Implement add."
    assert [c["name"] for c in runs[0]["calls"]] == ["read_file", "write_file"] and runs[0]["answer"] == "Done."


def test_a_round_that_fixes_the_test(project):
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "plan.toml").write_text(PLAN.split("[[tasks]]")[0])          # no tasks: only the rounds
    (base / "src" / "calc.py").write_text(ADD)
    client = StubClient([{"read_file": {"path": "tests/test_calc.py"}},
                         {"write_file": {"path": "tests/test_calc.py", "content": "pass\n"}}]      # protected
                        + write(MUL))
    report = develop(base / "plan.toml", cfg, client=client)
    assert [(s["kind"], s["outcome"]) for s in report["steps"]] == [("round", "fixed")]
    assert report["final"]["tests_passed"] == 1 and not report["for_a_person"]
    assert (base / "tests" / "test_calc.py").read_text().startswith("import sys")


def test_bad_plans(project):
    from atlas.agents.develop import Developer, load_plan
    base, cfg = project
    (base / "bad.toml").write_text('agents = "x"\n')
    with pytest.raises(ConfigError, match="unknown keys"):
        load_plan(base / "bad.toml")
    (base / "bad.toml").write_text('[[tasks]]\nid = "x"\n')
    with pytest.raises(ConfigError, match="needs its text"):
        load_plan(base / "bad.toml")
    (base / "bad.toml").write_text('[[tasks]]\ntext = "x"\n')
    with pytest.raises(ConfigError, match="no tests"):
        load_plan(base / "bad.toml")
    (base / "bad.toml").write_text('tests = ["tests/none_*.py"]\n')
    with pytest.raises(ConfigError, match="no file matches"):
        Developer(load_plan(base / "bad.toml"), cfg)
