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
tools: [read_file, write_file, edit_file, check_code, run_test]
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
        if isinstance(turn, BaseException):                 # the model server fails, or Ctrl+C
            raise turn
        if isinstance(turn, dict):
            calls = [SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = SimpleNamespace(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = SimpleNamespace(content=turn, tool_calls=None), "stop"
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, total_tokens=55, cost=0.001))


def interrupted(run) -> None:
    """`run` ends in the KeyboardInterrupt of the stub. The asyncio task it leaves behind says
    so when it is collected (a real Ctrl+C, which asyncio turns into a cancellation, does not):
    collected here, quietly."""
    import gc
    import logging
    with pytest.raises(KeyboardInterrupt):
        run()
    log = logging.getLogger("asyncio")
    level, log.level = log.level, logging.CRITICAL + 1
    try:
        gc.collect()
    finally:
        log.setLevel(level)


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
[sources.code.syntax_commands]
py = []                                 # off: these tests write code that does not compile on purpose
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
    assert report["final"] == {"tests_passed": 0, "tests": 1, "check_errors": [], "errors": 0,
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
    assert runs[0]["calls"][1]["result"]["file"] == "src/calc.py" and "seconds" in runs[0]["calls"][1]


def test_a_change_that_breaks_the_syntax_is_not_made(project):
    """With the syntax check (Python's needs no command), the broken write of the agent is
    refused, it sees why, and the code stays as it was: nothing to undo."""
    from atlas.agents.develop import develop
    base, cfg = project
    cfg["sources"]["code"]["syntax_commands"] = {}
    (base / "plan.toml").write_text(PLAN.split("[[tasks]]")[0] + '[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    client = StubClient(write("def add(a, b:\n    return a + b\n") + write(ADD) + ["Done."] * 8)
    report = develop(base / "plan.toml", cfg, client=client)
    runs = [json.loads(l) for l in (cfg.workspace / "reports" / "develop-plan.calls.jsonl").read_text().splitlines()]
    refused = runs[0]["calls"][1]["result"]
    assert refused["error"].startswith("the change was not made: it breaks the syntax of src/calc.py")
    assert (report["steps"][0]["kind"], report["steps"][0]["outcome"]) == ("task", "kept")
    assert "undone" not in report["steps"][0]
    # the report counts the edits and the compilations, and the errors of the compiler it met
    assert report["steps"][0]["edits"] == {"made": 0, "failed": 1}
    assert report["totals"]["edits"]["failed"] >= 1 and report["compiler_errors"][0]["count"] >= 1
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert "## Compiler errors" in md and "| edits (failed) |" in md and "Edits: " in md


def test_a_run_that_stalls_starts_again_fresh_with_what_it_did(project, caplog):
    """stall_steps: tool calls without changing the code end the run; develop starts it again,
    with a new conversation that says what the first one changed (restarts)."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "agents" / "implementer.md").write_text(AGENT.replace("NAME", "implementer")
                                                   .replace("max_steps: 6", "max_steps: 6\nstall_steps: 2"))
    (base / "plan.toml").write_text(PLAN.split("[[tasks]]")[0] + '[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    read = {"read_file": {"path": "src/calc.py"}}
    half = "def add(a, b):\n    return a + b + 0\n"
    client = StubClient([read, {"write_file": {"path": "src/calc.py", "content": half}}, read, read,   # stalls
                         read, {"write_file": {"path": "src/calc.py", "content": ADD}}, "Done."] + ["No idea."] * 6)
    report = develop(base / "plan.toml", cfg, client=client)
    first, second = report["steps"][:2]
    assert (first["kind"], first["outcome"], first["stalled"], first["calls"]) == ("task", "restarted", True, 4)
    assert (second["kind"], second["outcome"]) == ("task-restart", "kept") and "stalled" not in second
    restart = client.prompts[1]
    assert restart.startswith("Implement add.\n\nThis is a fresh start: an earlier run on this stopped after 4 tool "
                              "calls without changing the code in its last steps")
    assert "+    return a + b + 0" in restart                    # what it changed, which stays
    assert (base / "src" / "calc.py").read_text() == ADD
    assert "Failed to detach context" not in caplog.text        # the run of ADK ends cleanly when cut


class ParallelClient:
    """Scripted turns for runs at the same time: each new conversation takes the next script,
    and its tool calls carry the letter of its script, so that its next turns are known."""

    def __init__(self, scripts):
        import threading
        self.scripts = {chr(65 + i): list(s) for i, s in enumerate(scripts)}
        self.order, self.lock = list(self.scripts), threading.Lock()
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        with self.lock:
            called = [m for m in kwargs["messages"] if m.get("tool_calls")]
            tag = called[-1]["tool_calls"][0]["id"][0] if called else self.order.pop(0)
            turn = self.scripts[tag].pop(0)
        if isinstance(turn, dict):
            calls = [SimpleNamespace(id=f"{tag}{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = SimpleNamespace(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = SimpleNamespace(content=turn, tool_calls=None), "stop"
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, total_tokens=55))


def test_parallel_attempts_each_in_a_copy_and_the_tests_choose(project):
    """attempts = 3: three runs at once, each in its own copy; the one that breaks nothing and
    gets further wins, and its files come to the working folder."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "tests" / "test_calc.py").write_text(TEST.replace('"SRC"', 'str(__import__("pathlib").Path(__file__)'
                                                                        '.resolve().parent.parent / "src")'))
    plan = PLAN.split("[[tasks]]")[0].replace("build_fixes = 1", "build_fixes = 0").replace("fix_rounds = 4",
                                                                                          "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + 'attempts = 3\n\n[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    client = ParallelClient([write(MUL), write("def add(a, b:\n    return a + b\n"), write(ADD)])
    report = develop(base / "plan.toml", cfg, client=client)
    assert (base / "src" / "calc.py").read_text() == MUL                   # the one that passes the test
    tasks = [s for s in report["steps"] if s["kind"] == "task"]
    assert sorted(s["attempt"] for s in tasks) == [1, 2, 3]
    assert sorted(s["outcome"] for s in tasks) == ["kept", "not chosen", "undone"]
    chosen = next(s for s in tasks if s["outcome"] == "kept")
    assert chosen["chosen"].endswith("of 3") and chosen["tests_passed"] == 1
    assert report["final"]["tests_passed"] == 1
    assert not (cfg.workspace / "attempts" / "1").exists()                  # the copies go away
    assert "| task #" in (cfg.workspace / "reports" / "develop-plan.md").read_text()


def test_a_planner_reads_and_the_agent_gets_the_task_with_its_plan(project):
    """planner: the architect and editor of Aider, with the same model in both roles."""
    from atlas.agents.develop import develop
    from atlas.config import ConfigError
    base, cfg = project
    (base / "agents" / "planner.md").write_text(AGENT.replace("NAME", "planner").replace(
        "tools: [read_file, write_file, edit_file, check_code, run_test]", "tools: [read_file]"))
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + 'planner = "planner"\n\n[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    client = StubClient([{"read_file": {"path": "src/calc.py"}}, "Replace the body of add: return a + b."]
                        + write(ADD))
    report = develop(base / "plan.toml", cfg, client=client)
    assert [(s["kind"], s["agent"], s["outcome"]) for s in report["steps"]] == [
        ("plan", "planner", "planned"), ("task", "implementer", "kept")]
    assert client.prompts[0] == "Implement add."
    assert client.prompts[1].startswith("Implement add.\n\nHow to do it, from a colleague")
    assert client.prompts[1].endswith("Replace the body of add: return a + b.")
    (base / "plan.toml").write_text(plan + 'planner = "nobody"\n\n[[tasks]]\ntext = "x"\n')
    with pytest.raises(ConfigError, match="there is no agent 'nobody'"):
        develop(base / "plan.toml", cfg, client=StubClient([]))


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


ADVANCES = '''agent = "implementer"
check = ["src/*.py"]
tests = ["tests/test_*.py"]
progress = 'Step (\\d+)'
advance_fixes = 1
fix_rounds = 0

[[tasks]]
id = "add"
text = "Implement add."
advances = ["test_calc.py"]

[[tasks]]
id = "mul"
text = "Implement mul."
advances = ["tests/test_*.py"]
'''


def test_a_task_that_does_not_advance_its_tests_goes_back_to_the_agent(project):
    """The task of HELLO left an exception that no test could get past, and eight tasks went
    by without anything saying so: a task says which tests it makes get further."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "plan.toml").write_text(ADVANCES)
    client = StubClient(
        write("def add(a, b):\n    return a - b\n")          # add: compiles, breaks nothing, the test does not advance
        + write(ADD)                                            # told so, it does
        + [{"read_file": {"path": "src/calc.py"}}, "Nothing to do."]   # mul: nothing
        + write("def add(a, b):\n    return 0\n"))           # told so, it breaks add: that turn alone is undone
    report = develop(base / "plan.toml", cfg, client=client)
    assert [(s["kind"], s["task"], s.get("outcome")) for s in report["steps"]] == [
        ("task", "add", "kept"), ("advance-fix", "add", "advanced"), ("task", "mul", "kept"),
        ("advance-fix", "mul", "undone")]
    add, mul = report["steps"][0], report["steps"][2]
    assert "not_advanced" not in add and mul["not_advanced"] == ["tests/test_calc.py"]
    assert report["steps"][3]["broke"][0].startswith("tests/test_calc.py got to 2 and now got to 1")
    assert (base / "src" / "calc.py").read_text() == ADD                    # what add left
    prompt = client.prompts[1]
    assert prompt.startswith("The change does not do all that the task asks yet: the test tests/test_calc.py should")
    assert "Step 1: expected 3, received -1" in prompt
    assert "Step 2: expected 6, received None" in client.prompts[3]
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert "## Did not advance" in md and "- 3 mul: tests/test_calc.py" in md


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
    (base / "bad.toml").write_text('tests = ["tests/test_*.py"]\n[[tasks]]\ntext = "x"\nadvances = ["test_other.py"]\n')
    with pytest.raises(ConfigError, match="none of its tests"):
        Developer(load_plan(base / "bad.toml"), cfg)
    (base / "bad.toml").write_text('tests = ["tests/test_*.py"]\n[[tasks]]\ntext = "x"\nadvance = ["test_calc.py"]\n')
    with pytest.raises(ConfigError, match="not \\['advance'\\]"):
        load_plan(base / "bad.toml")
    (base / "bad.toml").write_text('tests = ["tests/test_*.py"]\ntask_calls = -1\n')
    with pytest.raises(ConfigError, match="task_calls is a number, 0 or more"):
        load_plan(base / "bad.toml")


def test_a_plan_is_checked_before_any_run(project):
    """An agent that does not exist or cannot do its part, or tests that an agent could change:
    a ConfigError at the start, and not a plan that goes through every task with an error."""
    from atlas.agents.develop import Developer, load_plan
    base, cfg = project
    head = PLAN.split("[[tasks]]")[0]
    for name, tools in (("reader", "read_file, check_code, run_test"), ("blind", "read_file, write_file, run_test"),
                        ("untested", "read_file, write_file, check_code")):
        (base / "agents" / f"{name}.md").write_text(AGENT.replace("NAME", name).replace(
            "read_file, write_file, edit_file, check_code, run_test", tools))

    def developer(plan: str):
        (base / "bad.toml").write_text(plan)
        return Developer(load_plan(base / "bad.toml"), cfg)

    with pytest.raises(ConfigError, match="there is no agent 'nobody'"):
        developer(head.replace('agent = "implementer"', 'agent = "nobody"'))
    with pytest.raises(ConfigError, match="agent reader has no tool that changes the code"):
        developer(head.replace('agent = "implementer"', 'agent = "reader"'))
    with pytest.raises(ConfigError, match="agent blind needs check_code among its tools"):
        developer(head.replace('agent = "implementer"', 'agent = "blind"'))
    with pytest.raises(ConfigError, match="fix_agent untested needs run_test among its tools: .* run the failing test"):
        developer(head.replace('fix_agent = "fixer"', 'fix_agent = "untested"'))
    developer(head.replace('agent = "implementer"', 'agent = "untested"'))      # the agent of the tasks needs no run_test
    cfg["sources"]["code"]["protected"] = []
    with pytest.raises(ConfigError, match="the test tests/test_calc.py is not protected"):
        developer(head)


def test_check_says_what_a_plan_starts_from_without_a_model(project, capsys):
    from atlas.agents.develop import Developer, load_plan
    from atlas.cli import main
    base, cfg = project
    (base / "src" / "broken.py").write_text("def f(:\n")
    (base / "plan.toml").write_text(PLAN + '\n[[tasks]]\nid = "long"\ntext = "' + "x" * 1300 + '"\n')
    found = Developer(load_plan(base / "plan.toml"), cfg).check()
    assert (found["tasks"], found["tests_passed"]) == (3, 0)
    assert found["tests"] == {"tests/test_calc.py": "Step 1: expected 3, received 0"}
    assert list(found["check_errors"]) == ["src/broken.py"]
    assert found["warnings"] == ["task long has 1300 characters, more than 1200: a small model does better with "
                                 "smaller tasks"]
    assert found["agents"] == {"agent": "implementer", "fix_agent": "fixer"}
    with pytest.raises(SystemExit) as stop:
        main(["develop", str(base / "plan.toml"), str(cfg.path), "--check"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "plan plan: 3 tasks, 1 tests; agent implementer, fix_agent fixer" in out
    assert "check_code: 1 files with errors\n  src/broken.py: line" in out
    assert "tests: 0 of 1 pass at the start\n  tests/test_calc.py: Step 1: expected 3, received 0" in out
    assert "warning: task long has 1300 characters" in out
    assert not (cfg.workspace / "reports" / "develop-plan.json").exists()          # nothing ran


def test_a_budget_stops_the_runs_of_an_attempt(project):
    """task_calls: once an attempt at a task has spent it, no build fix (nor restart, nor turn
    of advances) starts; the task is undone or kept as always, and its step says why."""
    from atlas.agents.develop import develop
    base, cfg = project
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + 'task_calls = 2\n\n[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    client = StubClient(write("def add(a, b:\n    return a + b\n") + write(ADD))     # no turns for a build fix
    report = develop(base / "plan.toml", cfg, client=client)
    assert [(s["kind"], s["outcome"]) for s in report["steps"]] == [("task", "undone"), ("task-retry", "kept")]
    assert report["steps"][0]["budget_exceeded"] == "task_calls = 2, spent 2"
    assert "budget_exceeded" not in report["steps"][1]                     # a new attempt, a new budget
    assert (base / "src" / "calc.py").read_text() == ADD and not client.turns
    assert "(budget spent: task_calls = 2, spent 2)" in (cfg.workspace / "reports" / "develop-plan.md").read_text()


def test_a_run_that_fails_is_an_error_and_not_a_task_done(project):
    from atlas.agents.develop import develop
    base, cfg = project
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + '[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    report = develop(base / "plan.toml", cfg, client=StubClient([RuntimeError("the server is down")]))
    (step,) = report["steps"]
    assert step["outcome"] == "error" and "the server is down" in step["error"]
    assert report["final"]["errors"] == 1
    assert "1 agent runs failed with an error." in (cfg.workspace / "reports" / "develop-plan.md").read_text()


def test_a_failure_in_a_line():
    from atlas.agents.develop import _last_lines
    output = '''Traceback (most recent call last):
  File "tests/python/test_stp_0101.py", line 11, in test_stp_0101
    raise AssertionError(f"Step {step}: command '{command}'")
AssertionError: Step 2: command 'PING', expected 'OK PONG', received 'the end of the connection'
with_server: the server stopped by itself during the test (exit code 1)
raised CONSTRAINT_ERROR : controller.adb:103 length check failed
The exception went through, the most recent first:
  controller.adb:103 in controller__execute
  socket_server.adb:19 in _ada_socket_server
'''
    assert _last_lines(output) == ("AssertionError: Step 2: command 'PING', expected 'OK PONG', received 'the end of "
                                   "the connection' | raised CONSTRAINT_ERROR : controller.adb:103 length check failed")
    assert _last_lines("one\ntwo\nthree\n") == "two | three"


def test_an_interrupted_run_leaves_the_files_as_they_were_and_resume_ends_it(project, tmp_path):
    """Ctrl+C in the middle of a task puts its files back and leaves the step `interrupted`;
    --resume does it again, not the task before it, and appends to the calls of the run. The
    patch of each task applies to the code as it was before it."""
    import shutil
    import subprocess
    from atlas.agents.develop import develop
    base, cfg = project
    original = (base / "src" / "calc.py").read_text()
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + '[[tasks]]\nid = "add"\ntext = "Implement add."\n\n'
                                           '[[tasks]]\nid = "mul"\ntext = "Implement mul."\n')
    client = StubClient(write(ADD) + [{"read_file": {"path": "src/calc.py"}},
                                      {"write_file": {"path": "src/calc.py", "content": MUL + "# half done\n"}},
                                      KeyboardInterrupt()])
    interrupted(lambda: develop(base / "plan.toml", cfg, client=client))
    assert (base / "src" / "calc.py").read_text() == ADD                    # as the first task left it
    reports = cfg.workspace / "reports"
    report = json.loads((reports / "develop-plan.json").read_text())
    assert [(s["kind"], s["task"], s["outcome"]) for s in report["steps"]] == [
        ("task", "add", "kept"), ("task", "mul", "interrupted")]
    assert report["steps"][1]["undone"] == ["src/calc.py"] and "error" not in report["steps"][1]

    client = StubClient(write(MUL))
    report = develop(base / "plan.toml", cfg, client=client, resume=True)
    assert client.prompts == ["Implement mul."]                             # add is not done again
    assert [(s["kind"], s["task"], s["outcome"]) for s in report["steps"]] == [
        ("task", "add", "kept"), ("task", "mul", "interrupted"), ("task", "mul", "kept")]
    assert report["final"]["tests_passed"] == 1 and len(report["resumed"]) == 1
    runs = [json.loads(l) for l in (reports / "develop-plan.calls.jsonl").read_text().splitlines()]
    assert [(r["n"], r["task"]) for r in runs] == [(1, "add"), (3, "mul")]

    assert [s.get("patch") for s in report["steps"]] == ["develop-plan/add.patch", None, "develop-plan/mul.patch"]
    md = (reports / "develop-plan.md").read_text()
    assert "kept ([patch](develop-plan/add.patch))" in md and "[develop-plan/all.patch](develop-plan/all.patch)" in md
    if shutil.which("git"):                     # each patch applies to the code before its task, and all.patch to the first
        for patches, expected in ((["add.patch"], ADD), (["add.patch", "mul.patch"], MUL), (["all.patch"], MUL)):
            fresh = tmp_path / f"fresh-{len(patches)}-{patches[0]}"
            (fresh / "src").mkdir(parents=True)
            (fresh / "src" / "calc.py").write_text(original)
            for patch in patches:
                subprocess.run(["git", "apply", str(reports / "develop-plan" / patch)], cwd=fresh, check=True)
            assert (fresh / "src" / "calc.py").read_text() == expected


def test_resume_goes_on_with_the_rounds(project):
    """A round interrupted is done again; those before it count, and so does what they tried."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "plan.toml").write_text(PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 3"))
    (base / "src" / "calc.py").write_text(ADD)
    nothing = [{"read_file": {"path": "src/calc.py"}}, "No idea."]
    interrupted(lambda: develop(base / "plan.toml", cfg, client=StubClient(nothing + [KeyboardInterrupt()])))
    client = StubClient(nothing * 2)
    report = develop(base / "plan.toml", cfg, client=client, resume=True)
    assert [(s["kind"], s["outcome"], s.get("round")) for s in report["steps"]] == [
        ("round", "no progress", 1), ("round", "interrupted", None), ("round", "no progress", 2)]
    assert "- No idea. -> no progress" in client.prompts[0]                 # the round before the interruption
    assert report["for_a_person"]["tests/test_calc.py"]["reason"] == "no progress in 2 rounds"
    assert len(client.turns) == 2                       # no third round: the test went to a person


def test_the_rounds_have_parallel_attempts_too(project):
    """attempts = 2 in a round: the attempt that breaks the code is undone, the one that fixes
    the test wins; the report has the wall time of the plan as well as that of the runs."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "tests" / "test_calc.py").write_text(TEST.replace('"SRC"', 'str(__import__("pathlib").Path(__file__)'
                                                                        '.resolve().parent.parent / "src")'))
    (base / "src" / "calc.py").write_text(ADD)
    plan = PLAN.split("[[tasks]]")[0].replace("build_fixes = 1", "build_fixes = 0").replace("fix_rounds = 4",
                                                                                          "fix_rounds = 1")
    (base / "plan.toml").write_text(plan + "attempts = 2\n")
    report = develop(base / "plan.toml", cfg, client=ParallelClient([write("def add(a, b):\n    return 0\n"),
                                                                     write(MUL)]))
    assert sorted((s["kind"], s["outcome"]) for s in report["steps"]) == [("round", "fixed"), ("round", "undone")]
    chosen = next(s for s in report["steps"] if s["outcome"] == "fixed")
    assert chosen["round"] == 1 and chosen["chosen"].endswith("of 2") and chosen["patch"] == "develop-plan/round-1.patch"
    assert (base / "src" / "calc.py").read_text() == MUL and report["final"]["tests_passed"] == 1
    totals = report["totals"]
    assert totals["wall_seconds"] >= 0 and "min of wall time" in (cfg.workspace / "reports" / "develop-plan.md").read_text()


class TaskClient:
    """Scripted turns for tasks at the same time: each new conversation takes the next script
    of the task its prompt starts with, and its tool calls carry its number. `together`: the
    tasks whose first turns wait for each other, which they can only do if they run at once."""

    def __init__(self, scripts: dict[str, list[list]], together: tuple[str, ...] = ()):
        import threading
        self.scripts = {task: [list(s) for s in conversations] for task, conversations in scripts.items()}
        self.turns: dict[str, list] = {}
        self.lock, self.prompts = threading.Lock(), []
        self.barrier = threading.Barrier(len(together)) if together else None
        self.together, self.started = together, set()
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        called = [m for m in kwargs["messages"] if m.get("tool_calls")]
        first = None
        with self.lock:
            if called:
                tag = called[-1]["tool_calls"][0]["id"].split("-")[0]
            else:
                prompt = kwargs["messages"][1]["content"]
                self.prompts.append(prompt)
                task = next(task for task in self.scripts if prompt.startswith(task))
                first = task if task in self.together and task not in self.started else None
                self.started.add(task)
                tag = f"k{len(self.turns)}"
                self.turns[tag] = self.scripts[task].pop(0)
            turn = self.turns[tag].pop(0)
        if isinstance(turn, BaseException):                 # Ctrl+C
            raise turn
        if first:                                    # the first turn of its first conversation
            self.barrier.wait(timeout=20)            # broken, and the test fails, if the other one is not running
        if isinstance(turn, dict):
            calls = [SimpleNamespace(id=f"{tag}-{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn.items())]
            message, finish = SimpleNamespace(content=None, tool_calls=calls), "tool_calls"
        else:
            message, finish = SimpleNamespace(content=turn, tool_calls=None), "stop"
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, total_tokens=55))


def write_to(path, content):
    """A run of the agent that reads `path`, writes `content` in it and says so."""
    return [{"read_file": {"path": path}}, {"write_file": {"path": path, "content": content}}, "Done."]


MODULE_TEST = '''import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import NAME
if NAME.value() != 1:
    print(f"Step 1: expected 1, received {NAME.value()}")
    sys.exit(1)
'''
INDEPENDENT = '''agent = "implementer"
check = ["src/*.py"]
tests = ["tests/test_*.py"]
progress = 'Step (\\\\d+)'
fix_rounds = 0
parallel = PARALLEL
''' + "".join(f'\n[[tasks]]\nid = "{n}"\ntext = "Implement {n}."\nafter = []\n' for n in "abc")


def independent(base):
    """Three modules, each with its test, and nothing else."""
    (base / "src" / "calc.py").unlink()
    (base / "tests" / "test_calc.py").unlink()
    for name in "abc":
        (base / "src" / f"{name}.py").write_text("def value():\n    return 0\n")
        (base / "tests" / f"test_{name}.py").write_text(MODULE_TEST.replace("NAME", name))


def test_independent_tasks_at_once_end_as_one_after_another(project):
    """parallel: three tasks without `after` go at once (at most 2 until the request limit is
    shared), each in its copy; the tests that pass at the end are those of the tasks one by one."""
    from atlas.agents.develop import develop
    base, cfg = project
    independent(base)
    one = "def value():\n    return 1\n"
    (base / "parallel.toml").write_text(INDEPENDENT.replace("PARALLEL", "3"))
    client = TaskClient({f"Implement {n}.": [write_to(f"src/{n}.py", one)] for n in "abc"},
                        together=("Implement a.", "Implement b."))
    report = develop(base / "parallel.toml", cfg, client=client)
    assert report["final"]["tests_passed"] == 3 and report["final"]["check_errors"] == []
    assert sorted((s["task"], s["outcome"]) for s in report["steps"]) == [("a", "kept"), ("b", "kept"), ("c", "kept")]
    assert all(s["patch"] == f"develop-parallel/{s['task']}.patch" for s in report["steps"])
    parallel = {n: (base / "src" / f"{n}.py").read_text() for n in "abc"}
    assert report["schedule"]["parallel"] == 2 and report["schedule"]["conflicts"] == 0
    assert report["warnings"] == ["parallel = 3 goes as 2: the request limit is not shared yet with the other "
                                  "processes of atlas (serve, run), and more agents at once would take it from them"]
    assert not (cfg.workspace / "tasks").exists()                    # the copies go away
    md = (cfg.workspace / "reports" / "develop-parallel.md").read_text()
    assert "Tasks at most 2 at once: " in md and "x as fast." in md and "Warning: parallel = 3 goes as 2" in md

    for name in "abc":                                                      # the same plan, one by one
        (base / "src" / f"{name}.py").write_text("def value():\n    return 0\n")
    (base / "serial.toml").write_text(INDEPENDENT.replace("PARALLEL", "1"))
    serial = develop(base / "serial.toml", cfg, client=StubClient(
        [turn for n in "abc" for turn in write_to(f"src/{n}.py", one)]))
    assert [(s["task"], s["outcome"]) for s in serial["steps"]] == [("a", "kept"), ("b", "kept"), ("c", "kept")]
    assert serial["final"]["tests_passed"] == report["final"]["tests_passed"]
    assert {n: (base / "src" / f"{n}.py").read_text() for n in "abc"} == parallel
    assert "schedule" not in serial and "warnings" not in serial


def test_ctrl_c_with_tasks_at_once_and_resume(project):
    """Ctrl+C in one task leaves the working folder with only the tasks brought to it; one that
    ended in its copy and was not brought goes again with --resume."""
    from atlas.agents.develop import develop
    base, cfg = project
    independent(base)
    one = "def value():\n    return 1\n"
    (base / "plan.toml").write_text(INDEPENDENT.replace("PARALLEL", "2"))
    client = TaskClient({"Implement a.": [[{"read_file": {"path": "src/a.py"}}, KeyboardInterrupt()]],
                         "Implement b.": [write_to("src/b.py", one)], "Implement c.": [write_to("src/c.py", one)]},
                        together=("Implement a.", "Implement b."))
    interrupted(lambda: develop(base / "plan.toml", cfg, client=client))
    report = json.loads((cfg.workspace / "reports" / "develop-plan.json").read_text())
    outcomes = {s["task"]: s["outcome"] for s in report["steps"]}
    assert outcomes["a"] == "interrupted"
    for name in "bc":                         # brought to the working folder, or interrupted and not there
        changed = (base / "src" / f"{name}.py").read_text() == one
        assert outcomes.get(name, "interrupted") == ("kept" if changed else "interrupted")
    assert not (cfg.workspace / "tasks").exists()
    client = TaskClient({f"Implement {n}.": [write_to(f"src/{n}.py", one)] for n in "abc"})
    report = develop(base / "plan.toml", cfg, client=client, resume=True)
    assert report["final"]["tests_passed"] == 3
    assert sorted(client.prompts) == [f"Implement {n}." for n in "abc" if outcomes.get(n) != "kept"]


def test_two_tasks_at_once_that_change_the_same_file(project):
    """The second one to end finds the file changed by the first: it is undone, and goes again
    alone on the code that the first one left."""
    from atlas.agents.develop import develop
    base, cfg = project
    (base / "tests" / "test_calc.py").write_text(TEST.replace('"SRC"', 'str(__import__("pathlib").Path(__file__)'
                                                                        '.resolve().parent.parent / "src")'))
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + "parallel = 2\n" + "".join(
        f'\n[[tasks]]\nid = "{n}"\ntext = "Implement {n}."\nafter = []\n' for n in ("add", "mul")))
    mul_alone = "def add(a, b):\n    return 0\n\n\ndef mul(a, b):\n    return a * b\n"
    client = TaskClient({"Implement add.": [write(ADD), write(MUL)], "Implement mul.": [write(mul_alone), write(MUL)]},
                        together=("Implement add.", "Implement mul."))
    report = develop(base / "plan.toml", cfg, client=client)
    assert (base / "src" / "calc.py").read_text() == MUL and report["final"]["tests_passed"] == 1
    (conflict,) = [s for s in report["steps"] if s["outcome"] == "conflict"]
    first = "mul" if conflict["task"] == "add" else "add"
    assert conflict["conflict"] == {"files": ["src/calc.py"], "with": [first]} and "patch" not in conflict
    assert [(s["task"], s["outcome"]) for s in report["steps"]][-1] == (conflict["task"], "kept")
    assert sum(s["outcome"] == "kept" for s in report["steps"]) == 2
    assert client.prompts[-1] == f"Implement {conflict['task']}."             # again, on the code of now
    assert report["schedule"]["conflicts"] == 1
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert f"conflict (src/calc.py, with {first}; done again alone)" in md
    assert "1 undone for a conflict and done again alone" in md


def test_after_names_tasks_of_the_plan_without_a_cycle(project):
    from atlas.agents.develop import load_plan
    base, _ = project
    head = 'tests = ["tests/test_*.py"]\n'
    (base / "p.toml").write_text(head + '[[tasks]]\ntext = "x"\n[[tasks]]\ntext = "y"\n'
                                        '[[tasks]]\nid = "z"\ntext = "z"\nafter = ["T01"]\n')
    assert [t["after"] for t in load_plan(base / "p.toml").tasks] == [[], ["T01"], ["T01"]]   # the one before
    for tasks, error in (
            ('[[tasks]]\nid = "a"\ntext = "x"\nafter = ["b"]\n[[tasks]]\nid = "b"\ntext = "y"\n',
             "the tasks wait for each other: a after b after a"),
            ('[[tasks]]\nid = "a"\ntext = "x"\nafter = ["a"]\n', "the tasks wait for each other: a after a"),
            ('[[tasks]]\nid = "a"\ntext = "x"\nafter = ["nobody"]\n', "task a is after nobody, which is not a task"),
            ('[[tasks]]\nid = "a"\ntext = "x"\n[[tasks]]\nid = "a"\ntext = "y"\n', "two tasks have the id a"),
            ('[[tasks]]\ntext = "x"\nafter = "T01"\n', "after \\(a list of tasks\\)"),
            ('parallel = 0\n', "parallel is a whole number, 1 or more")):
        (base / "p.toml").write_text(head + tasks)
        with pytest.raises(ConfigError, match=error):
            load_plan(base / "p.toml")


def test_the_agents_at_once_against_the_request_limit(project):
    """parallel x attempts agents, a turn every 30 s each: against [network] requests_per_minute."""
    from atlas.agents.develop import Developer, load_plan
    base, cfg = project
    head = PLAN.split("[[tasks]]")[0]
    (base / "p.toml").write_text(head + "parallel = 2\nattempts = 2\n")
    cfg.data["network"] = {"requests_per_minute": 6}
    found = Developer(load_plan(base / "p.toml"), cfg).check()
    assert found["warnings"] == ["4 agents at once (parallel 2 x attempts 2) ask for about 8 requests per minute, a "
                                 "turn every 30 s, more than [network] requests_per_minute = 6: they will wait for "
                                 "each other, and atlas serve or atlas run here get fewer"]
    cfg.data["network"] = {"requests_per_minute": 28}
    assert Developer(load_plan(base / "p.toml"), cfg).check()["warnings"] == []
