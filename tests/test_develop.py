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
check_command = [{json.dumps(sys.executable)}, "-m", "py_compile", "{{file}}"]
[sources.code.test_commands]
py = [{json.dumps(sys.executable)}, "{{file}}"]
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
    # the syntax error had no hint: a draft of one (its run wrote nothing after it)
    assert "## Hints to write" in md and "hints-plan.toml" in md
    draft = (cfg.workspace / "reports" / "hints-plan.toml").read_text()
    assert "\n[[hint]]\nmatch = " in draft and 'hint = ""' in draft and "# no write in the same run" in draft


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


def test_a_reviewer_reads_the_diff_and_its_issues_go_back_once(project):
    """reviewer: after a task that compiles and breaks nothing, a read-only run with the task and
    its diff; its JSON verdict goes back to the agent once (review_fixes)."""
    from atlas.agents.develop import _verdict, develop
    base, cfg = project
    (base / "agents" / "reviewer.md").write_text(AGENT.replace("NAME", "reviewer"))   # it has write_file: refused
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + 'reviewer = "reviewer"\n\n[[tasks]]\nid = "add"\ntext = "Implement add and mul."\n')
    issues = '{"verdict": "issues", "issues": [{"file": "src/calc.py", "line": 2, "issue": "mul is missing"}]}'
    client = StubClient(write(ADD)
                        + [{"write_file": {"path": "src/calc.py", "content": "x = 1\n"}}, f"```json\n{issues}\n```"]
                        + write(MUL))
    report = develop(base / "plan.toml", cfg, client=client)
    assert [(s["kind"], s["agent"], s["outcome"]) for s in report["steps"]] == [
        ("task", "implementer", "kept"), ("review", "reviewer", "issues"), ("review-fix", "implementer", "kept")]
    assert report["steps"][1]["issues"] == [{"file": "src/calc.py", "line": 2, "issue": "mul is missing"}]
    assert client.prompts[1].startswith("Review a change to the code of the project. The task it does:\n\n"
                                        "Implement add and mul.") and "+    return a + b" in client.prompts[1]
    assert client.prompts[2].startswith("A colleague reviewed") and "- src/calc.py, line 2: mul is missing" in \
        client.prompts[2]
    runs = [json.loads(l) for l in (cfg.workspace / "reports" / "develop-plan.calls.jsonl").read_text().splitlines()]
    assert runs[1]["calls"][0]["result"] == {"error": "these tools are read-only"}
    assert (base / "src" / "calc.py").read_text() == MUL and report["final"]["tests_passed"] == 1
    assert "## Review" in (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert _verdict('{"verdict": "ok"}') == [] and _verdict("Looks fine.") is None
    (base / "plan.toml").write_text(plan + 'reviewer = "nobody"\n\n[[tasks]]\ntext = "x"\n')
    with pytest.raises(ConfigError, match="there is no agent 'nobody'"):
        develop(base / "plan.toml", cfg, client=StubClient([]))


@pytest.mark.parametrize("stray", ["report", "delete"])
def test_the_files_a_task_leaves(project, stray):
    """The agent of the fourth run left a test of its own in src: a kept task lists what it
    created; with stray = "delete", a new file that is a test among the code is deleted before
    the task is judged, and a new module is not."""
    from atlas.agents.develop import develop
    base, cfg = project
    cfg["sources"]["code"]["test_patterns"] = ["tests/**", "**/*_test.py"]
    plan = PLAN.split("[[tasks]]")[0].replace("fix_rounds = 4", "fix_rounds = 0")
    (base / "plan.toml").write_text(plan + f'stray = "{stray}"\n\n[[tasks]]\nid = "add"\ntext = "Implement add."\n')
    client = StubClient(write(ADD)[:2] + [
        {"write_file": {"path": "src/scratch_test.py", "content": "import calc\nprint(calc.add(1, 2))\n"}},
        {"write_file": {"path": "src/util.py", "content": "def half(x):\n    return x / 2\n"}}, "Done."])
    report = develop(base / "plan.toml", cfg, client=client)
    step = report["steps"][0]
    assert (step["kind"], step["outcome"]) == ("task", "kept")
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert "## New files" in md and (base / "src" / "util.py").exists()
    if stray == "report":
        assert step["created"] == ["src/scratch_test.py", "src/util.py"] and "deleted" not in step
        assert (base / "src" / "scratch_test.py").exists()
        assert "- 1 add: src/scratch_test.py, src/util.py" in md
    else:
        assert step["created"] == ["src/util.py"] and step["deleted"] == ["src/scratch_test.py"]
        assert not (base / "src" / "scratch_test.py").exists()
        assert "- 1 add: src/util.py; deleted src/scratch_test.py" in md
    (base / "plan.toml").write_text(plan + 'stray = "keep"\n[[tasks]]\ntext = "x"\n')
    with pytest.raises(ConfigError, match="stray is 'report' or 'delete'"):
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


CALLS = [
    {"n": 1, "kind": "task", "task": "T01", "prompt": "x", "answer": "", "calls": [
        {"name": "write_file", "args": {"path": "src/a.adb", "content": "..."},
         "result": {"file": "src/a.adb", "created": True, "check": {"ok": False, "errors": [
             {"line": 3, "message": '"Foo" is undefined', "hint": "Foo is not declared"},
             {"line": 5, "message": 'unexpected token "bar" at 12', "details": ["did you mean Bar"]}]}}},
        {"name": "edit_file", "args": {"path": "src/a.adb"},
         "result": {"error": "the change was not made: it breaks the syntax of src/a.adb",
                    "syntax_errors": [{"line": 2, "message": 'missing ";"'}]}},
        {"name": "edit_file", "args": {"path": "src/a.adb"},
         "result": {"file": "src/a.adb", "replacements": 1, "diff": "-   bar\n+   Bar;",
                    "check": {"ok": False, "errors": [{"line": 3, "message": '"Foo" is undefined', "hint": "x"}]}}},
        {"name": "check_code", "args": {"path": "src/b.adb"},
         "result": {"file": "src/b.adb", "ok": False, "errors": [
             {"file": "b.adb", "line": 7, "message": 'unexpected token "baz" at 40'}]}}]},
    {"n": 2, "kind": "round", "task": "tests/t.py", "prompt": "y", "answer": "", "calls": [
        {"name": "check_code", "args": {"path": "src/b.adb"},
         "result": {"file": "src/b.adb", "ok": False, "errors": [
             {"file": "b.adb", "line": 9, "message": 'unexpected token "qux" at 3'}]}}]}]


def test_drafts_of_hints_from_the_calls(project, capsys):
    """The errors without a hint of a calls log, by shape, the most frequent first, each with the
    write that got rid of it; a TOML of [[hint]] with empty hints, for a person to write."""
    import re
    import tomllib
    from atlas import cli
    from atlas.agents.hints import draft_hints, hint_match, write_hints_draft
    from atlas.code.build import load_hints
    base, cfg = project
    calls = cfg.workspace / "reports" / "develop-server.calls.jsonl"
    calls.parent.mkdir(parents=True)
    calls.write_text("\n".join(json.dumps(r) for r in CALLS) + "\n")
    drafts = draft_hints(calls)
    assert [(d["shape"], d["count"]) for d in drafts] == [('unexpected token "X" at N', 3), ('missing "X"', 1)]
    token, syntax = drafts
    assert token["match"] == 'unexpected token "(?P<name>[^"]+)" at \\d+'
    assert all(re.search(token["match"], m) for m in ('unexpected token "bar" at 12', 'unexpected token "q" at 3'))
    assert token["fix"] == {"diff": "-   bar\n+   Bar;", "call": "edit_file", "file": "src/a.adb", "run": 1}
    assert token["details"] == ["did you mean Bar"] and token["runs"] == [1, 2]
    assert token["files"] == ["src/a.adb", "src/b.adb"]
    assert syntax["fix"]["run"] == 1                      # the syntax error: the next write that was made
    assert hint_match('"A" and "B" at line 4') == '"(?P<name>[^"]+)" and "(?P<name2>[^"]+)" at line \\d+'
    path, _ = write_hints_draft(calls)
    assert path.name == "hints-server.toml"
    text = path.read_text()
    assert "# 3 times, in src/a.adb, src/b.adb (runs 1, 2)" in text and "#   +   Bar;" in text
    assert [h["hint"] for h in tomllib.loads(text)["hint"]] == ["", ""]
    assert len(load_hints([path])) == 2                  # a hints file as it is, which says nothing yet
    assert cli.hints(cfg, "tasks/server.toml") == 0
    out = capsys.readouterr().out
    assert "2 errors without a hint; drafts in" in out and "fixed in run 1" in out
    with pytest.raises(SystemExit, match="no calls of atlas develop for other"):
        cli.hints(cfg, "other")


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


ANCHORED = '''agent = "implementer"
check = ["src/*.py"]
tests = ["tests/test_*.py"]
progress = 'Step (\\d+)'
advance_fixes = 0
fix_rounds = 0
require_tags = true

[[tasks]]
id = "add"
text = "Implement add."
requirements = ["SRS-0001"]

[[tasks]]
id = "mul"
text = "Implement mul."
requirements = ["srs-0002"]
'''
TEST_ADD = '''import sys
sys.path.insert(0, "SRC")
import calc
# Req: SRS-0001
def check(step, got, want):
    if got != want:
        print(f"Step {step}: expected {want}, received {got}")
        sys.exit(1)
check(1, calc.add(1, 2), 3)
'''
TEST_STP = '''import sys
sys.path.insert(0, "SRC")
import calc
if getattr(calc, "mul", lambda a, b: None)(2, 3) != 6:
    print("Step 1: expected 6")
    sys.exit(1)
'''


@pytest.fixture
def anchored(project):
    """The project with DOORS (SRS-0001, SRS-0002 and STP-0101, which verifies SRS-0002) and
    its graph built by atlas run: test_calc.py cites SRS-0001, and test_stp_0101.py is the test
    of the procedure, named after it, as atlas tests writes them."""
    import contextlib
    import io
    import sqlite3
    from atlas.cli import run
    base, cfg = project
    (base / "tests" / "test_calc.py").write_text(TEST_ADD.replace("SRC", str(base / "src")))
    (base / "tests" / "test_stp_0101.py").write_text(TEST_STP.replace("SRC", str(base / "src")))
    db = sqlite3.connect(base / "doors.sqlite")
    db.executescript("""
        CREATE TABLE requirements (id TEXT, module TEXT, text TEXT);
        CREATE TABLE links (source_id TEXT, source_module TEXT, target_id TEXT, link_type TEXT);
        INSERT INTO requirements VALUES ('SRS-0001', 'SRS_CALC', 'The calculator shall add two integers.'),
            ('SRS-0002', 'SRS_CALC', 'The calculator shall multiply two integers.'),
            ('STP-0101', 'STP_CALC', 'Test of the product. 1. Multiply 2 by 3; expect 6.');
        INSERT INTO links VALUES ('STP-0101', 'STP_CALC', 'SRS-0002', 'verifies');
    """)
    db.commit()
    db.close()
    text = cfg.path.read_text()
    doors = text.index("[sources.doors]")
    (base / "atlas.toml").write_text(text[:doors] + f'''[sources.doors]
adapter = "database"
[sources.doors.database]
driver = "sqlite"
path = "{(base / 'doors.sqlite').as_posix()}"
modules_query = "SELECT DISTINCT module FROM requirements ORDER BY module"
requirements_query = "SELECT * FROM requirements WHERE module = :module ORDER BY id"
links_query = "SELECT source_id, target_id, link_type FROM links WHERE source_module = :module"
[sources.doors.convention]
id_pattern = "^(?:SRS|STP)-\\\\d{{4}}$"
[pipeline]
steps = ["doors", "code", "graph"]
''' + text[text.index("[agents]"):].replace("[sources.code]", '[sources.code]\nlanguages = ["python"]\n'
                                                                 'test_patterns = ["tests/**"]'))
    cfg = load_config(base / "atlas.toml")
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    return base, cfg


TAGGED_ADD = "# Req: SRS-0001\ndef add(a, b):\n    return a + b\n"
TAGGED_MUL = TAGGED_ADD + "\n\n# Req: SRS-0002\ndef mul(a, b):\n    return a * b\n"


def test_tasks_anchored_to_requirements(anchored):
    """requirements: the agent gets their text, the tests that verify them are the advances of
    the task, and a kept task whose requirements no changed subprogram cites goes back once."""
    from atlas.agents.develop import develop
    base, cfg = anchored
    (base / "plan.toml").write_text(ANCHORED)
    client = StubClient(write(ADD)                                    # add: passes, but cites nothing
                        + write(TAGGED_ADD)                           # told so, it tags it
                        + write(TAGGED_MUL))                          # mul: tagged at once
    steps = []
    report = develop(base / "plan.toml", cfg, client=client, on_step=steps.append)
    assert report["requirements"] == {
        "add": {"requirements": ["SRS-0001"], "advances": ["tests/test_calc.py"], "advances_from": "requirements"},
        "mul": {"requirements": ["SRS-0002"], "advances": ["tests/test_stp_0101.py"],      # through STP-0101
                "advances_from": "requirements"}}
    assert [(s["kind"], s["task"], s["outcome"], s.get("untagged")) for s in report["steps"]] == [
        ("task", "add", "kept", ["SRS-0001"]), ("tag-fix", "add", "tagged", []), ("task", "mul", "kept", [])]
    first = client.prompts[0]
    assert first.startswith("The requirements of this task, as the project states them. Cite their IDs in a `Req:`")
    assert "SRS-0001 (SRS_CALC): The calculator shall add two integers.\n  No code cites it yet." in first
    assert first.endswith("The task:\nImplement add.")
    assert client.prompts[1].startswith("No subprogram you changed cites SRS-0001 in its `Req:` comment")
    assert "SRS-0002 (SRS_CALC): The calculator shall multiply" in client.prompts[2]
    assert (base / "src" / "calc.py").read_text() == TAGGED_MUL and report["final"]["tests_passed"] == 2
    md = (cfg.workspace / "reports" / "develop-plan.md").read_text()
    assert "## Untagged" in md and "- 1 add: SRS-0001" in md


def test_the_code_that_already_cites_a_requirement_and_untagged_without_require_tags(anchored):
    from atlas.agents.develop import Developer, load_plan
    import contextlib
    import io
    from atlas.cli import run
    base, cfg = anchored
    (base / "src" / "calc.py").write_text(TAGGED_ADD)
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    (base / "plan.toml").write_text(ANCHORED.replace("require_tags = true", "").replace(
        'requirements = ["SRS-0001"]', 'requirements = ["SRS-0001"]\nadvances = ["test_stp_0101.py"]'))
    dev = Developer(load_plan(base / "plan.toml"), cfg)
    assert dev.plan.tasks[0]["advances"] == ["tests/test_stp_0101.py"]        # those of the plan stay
    assert "advances_from" not in dev.report["requirements"]["add"]
    text = dev.task_text(dev.plan.tasks[0])
    assert "Code that already cites it: calc.add (src/calc.py, lines 2-3)" in text
    dev.plan.requirement_chars = 200
    assert len(dev.requirement("SRS-0001")) <= 200
    before = dev.files.snapshot()
    (base / "src" / "calc.py").write_text(TAGGED_ADD + "\n\ndef mul(a, b):\n    return a * b\n")
    assert dev.untagged(["SRS-0001", "SRS-0002"], before) == ["SRS-0001", "SRS-0002"]   # add did not change
    (base / "src" / "calc.py").write_text(TAGGED_ADD.replace("a + b", "b + a"))
    assert dev.untagged(["SRS-0001", "SRS-0002"], before) == ["SRS-0002"]


def test_requirements_that_are_not_in_the_graph(anchored):
    from atlas.agents.develop import Developer, load_plan
    base, cfg = anchored
    (base / "bad.toml").write_text(ANCHORED.replace('"srs-0002"', '"SRS-0003"'))
    with pytest.raises(ConfigError, match="task mul: requirement 'SRS-0003' is not in the graph"):
        Developer(load_plan(base / "bad.toml"), cfg)
    (base / "bad.toml").write_text(ANCHORED.replace('requirements = ["SRS-0001"]', 'requirements = "SRS-0001"'))
    with pytest.raises(ConfigError, match="requirements \\(a list of IDs\\)"):
        load_plan(base / "bad.toml")
    (base / "plan.toml").write_text(ANCHORED)
    cfg["project"]["workspace"] = str(base / "empty")                    # no atlas run
    with pytest.raises(ConfigError, match="has no graph with requirements: atlas run first"):
        Developer(load_plan(base / "plan.toml"), cfg)
