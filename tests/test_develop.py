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
        "tools: [read_file, write_file, edit_file]", "tools: [read_file]"))
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
