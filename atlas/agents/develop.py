"""atlas develop: an agent writes the code task by task, and the tests say what it got right.

    atlas develop tasks/server.toml atlas.toml

The plan is a TOML file:

    agent = "implementer"                    # the agent of the tasks
    agent_command = ["opencode", "run", "{prompt}"]   # or: an agent of outside, instead of agent and fix_agent
    command_timeout = 1800                   # seconds of each run of agent_command (0: no limit)
    fix_agent = "fixer"                      # the agent that fixes a failing test (default: agent)
    check = ["src/*.adb"]                    # check_code on these files after each task
    tests = ["tests/python/test_*.py"]       # the acceptance tests, run with run_test, in this order
    progress = 'Step\\s+(\\d+)'                # optional: how far a failing test got (the last number)
    build_fixes = 2                          # turns to fix what check reports, after a task
    advance_fixes = 2                        # turns to make the tests of `advances` get further
    fix_rounds = 12                          # rounds to fix failing tests, after the tasks
    stuck_rounds = 2                         # rounds without progress before a test goes to a person
    restarts = 1                             # fresh starts of a run that stopped (max_steps, stall_steps)
    attempts = 1                             # parallel attempts at each task, each in a copy: the tests choose
    parallel = 1                             # tasks at once, those whose `after` are finished, each in a copy
    planner = "planner"                      # optional: an agent that reads and says how to do each task
    task_calls = 0                           # budget of an attempt at a task: tool calls (0: no limit)
    task_seconds = 0                         # ... seconds
    task_cost = 0.0                          # ... cost, as the model server reports it
    require_tags = false                     # a kept task whose requirements no changed subprogram cites goes back once
    requirement_chars = 1200                 # characters of each requirement in front of a task
    reviewer = "reviewer"                    # optional: an agent that reads the diff of a task that compiles
    review_fixes = 1                         # turns that the verdict of the reviewer goes back to the agent (0: report)
    stray = "report"                         # or "delete": the new files of a task that are tests outside tests/
    propose = false                          # true: the agents work on a copy; the patches are what it gives
    max_copy_mb = 500                        # the largest working folder that propose and attempts copy
    fix_prompt = "..."                       # optional: {test}, {output}, {code}, {attempts}
    advance_prompt = "..."                   # optional: {test}, {output}, {code}

    [[tasks]]
    id = "controller"                        # optional: T01, T02...
    text = "Create the package Controller of SRS-0191 ..."
    requirements = ["SRS-0191"]              # optional: the requirements it implements, in the graph
    advances = ["tests/python/test_stp_0101.py"]   # optional: the tests it makes get further
    after = ["T01"]                          # optional: the tasks it needs finished (default: the one before)

The paths of the plan (check, tests, advances) are relative to the folder of atlas.toml, like
those of [sources.code], wherever atlas runs from.

What it does comes from what went wrong when gemma-4-26b and devstral wrote a server with no
one to help them: code that compiled and did not work, tests changed to pass, failures tried
again and again in the same way, and changes that broke what worked.

1. Each task goes to the agent; then check_code runs on the files of `check`, and the tests run.
   When check finds errors, they go back to the agent, at most `build_fixes` times.
2. A task that leaves errors, or makes a test fail sooner than before (a test that passed
   fails; with `progress`, a test stops at an earlier step), is undone: the files go back to
   how they were. It is tried once more, told what it broke; if it breaks it again, it stays
   undone. The code never gets worse.
   A task can say which tests it makes get further (`advances`: paths or patterns of tests).
   When one of them fails where it failed before, the task goes back to the agent with what
   the test printed, at most `advance_fixes` times, like the errors of check; a turn of these
   that breaks something is undone alone. If the tests still do not get further, the task is
   kept and reported (`not_advanced`): its code compiles and breaks nothing, but it does not
   do what it should, and that is said now, not eight tasks later (the task of HELLO with
   gemma-4-26b left an exception that no test could get past, and nothing said so).
3. Then the rounds: the fix agent gets one failing test, what it printed and the code it cites,
   and the earlier attempts on it. A round that breaks something is undone. A test that does
   not get further in `stuck_rounds` rounds is left for a person: the agent does not insist on
   what it cannot fix. While a test gets further it keeps the turn; when not, the next one.
4. A report after each step, in <workspace>/reports/develop-<plan>.json and .md: what each
   task and round did, its time, tokens and cost, its edits (and how many failed) and its
   compilations (and how many had errors), the tests that pass and those that need a person,
   with their output, and the errors of the compiler of the whole plan, the most frequent
   first, with whether a hint came with them ([sources.code] hints: one more after each run),
   and the time of the runs and the wall time of the plan (`wall_seconds`: with the tests, and
   with the parallel attempts counted once).
   develop-<plan>.calls.jsonl keeps the prompt, the tool calls with their results and the
   answer of every run of an agent. What each task and each round left in the code is a patch,
   develop-<plan>/<id>.patch (round-<n>.patch), and all of them in develop-<plan>/all.patch, to
   review them and to apply them elsewhere (git apply, patch -p1).

With a planner, each task goes first to that agent (with tools to read, not to change): it
writes how to do it, and the task goes to the agent with that plan. The architect and editor of
Aider: the model does not have to think out the change and get the tools right in the same
turns (with the same model in both roles, GPT-4o-mini went from 55.6 to 60.2 %).

With a reviewer (off by default), a task that compiles and breaks nothing goes to that agent,
read-only whatever its tools, with the task and the diff of the change: it answers in JSON, ok or
the issues with their lines (what neither the compiler nor the tests see: a swallowed exception,
dead branches, changes outside the task). The issues go back to the agent once (`review_fixes`);
a fix that breaks something is undone. The rules of [sources.code] rules come first, without a
model.

With attempts > 1, each task (and its retry) and each round go to that many runs at once, each
in its own copy of the working folder, the folder of atlas.toml (<workspace>/attempts/<n>: the
code roots, the scripts of the project, its own build folder), with its build fixes and the
turns of `advances`. Of those that break nothing, the one with more tests passing, and further,
wins, and its files are copied to the working folder. A small model gets one in N right more
often than one right in a row (Agentless, S*); the request limit leaves room (a turn of
gemma-4-26b takes ~30 s: 2 requests per minute of 28). The code roots must be inside the
working folder.

A run that stops without finishing (max_steps, or stall_steps of the agent: that many tool
calls without changing the code) starts again with a fresh conversation, at most `restarts`
times: the same prompt, the diff of what it changed, which stays, and what check_code says
now. A long conversation of failed edits does not get better with more turns.

Each attempt at a task (the task, its retry, a round), with its build fixes, its turns of
`advances` and its restarts, has a budget: task_calls, task_seconds and task_cost (0: no
limit). When it is spent no more runs of it start; what it did is kept or undone as always, and
its step says so (`budget_exceeded`). A build fix of gemma-4-26b spent its 40 calls and 37
minutes without fixing anything.

The plan is checked before any run: its agents exist and can do their part (a tool that
changes the code; check_code when [sources.code] has a compiler; run_test for the fix agent),
and its tests are protected. Developer.check() (`atlas develop PLAN --check`) also runs
check_code and the tests once, without a model, and says which tasks are too long. A run that
fails (the model server) is an `error` step, not a kept task.

A run cut short (Ctrl+C, or an error of atlas itself) puts the files back as they were before
its attempt, and its step is `interrupted`. `atlas develop PLAN --resume` (resume=True) goes
on from the report: the tasks it finished are done (one kept, or undone after its retry), and
those with an `error` or `interrupted` go again; the rounds go on from where they were. The
state is what the tests say now, not what the report says. With attempts > 1, Ctrl+C waits for
the runs in progress to end.

A task anchored to requirements (`requirements`, IDs of the graph: atlas run first, and an ID
that is not there stops the plan before it starts) gets three things without a model:
- in front of its text, for each requirement, its official text, the sections that state it and
  the code that already cites it (from trace, at most `requirement_chars` characters each): the
  agent does not have to find them, nor guess them from the words of the task;
- without `advances`, the tests of the plan that verify those requirements: those that cite
  them (trace verified_by), and those of the test procedures linked to them with `verifies`
  (an STP of DOORS, whose tests atlas tests names after it: test_stp_0101), by file or by name;
- after it is kept, the changed files are analysed (the analyzer of their language, without
  the pipeline) and the step says which of its requirements no changed subprogram cites
  (`untagged`). With require_tags, the task goes back to the agent once to tag them; a turn that
  breaks something is undone alone. The traceability does not depend on an instruction.

The files a kept task creates are listed in its step (`created`, "New files" in the report): in
the fourth run, the agent left a test of its own in src, which went into check, into every copy
of `attempts` and into the next atlas run. With stray = "delete", the new files of the run of a
task that match [sources.code] test_patterns, and not its protected ones, are deleted before the
task is judged (`deleted`): a test of the agent outside tests/. Only those: a new package of the
code is never deleted. `protected` already keeps the agent out of the tests themselves.

With parallel > 1, the tasks whose `after` are finished (a task without it is after the one
before it: one by one, as without parallel) go at once, at most `parallel`, each in its own copy
of the working folder (<workspace>/tasks/<id>) with all the machinery of a task: its planner,
its attempts, its build fixes, its turns of `advances` and its retry. When one ends, only the
files it changed come to the working folder, and the tests run again there. If a task that
ended first had changed one of those files, or if together they break what worked, the one
that comes second is undone (`conflict` in its step, with the files and the tasks it met) and
goes again alone, on the code as it is now, once those running end; the tasks after it wait.
A task that is the only one that can go runs in the working folder, as without parallel. The
report says how much faster the tasks went (`schedule`: their time one after another, and the
wall time). Each agent at once asks for a turn every ~30 s: parallel x attempts of them are
checked against [network] requests_per_minute, with a warning when they would fill it. Until
the request limit is shared between processes, parallel is at most 2.

Undo puts back the files of the code roots that an agent can change (neither excluded nor
protected); nothing is committed. The tests should be protected ([sources.code] protected).

What goes into a prompt from the project (what a test printed, the code it cites, the errors of
the compiler, the diff of a restart, what an undone task broke) goes in a <data> block
(guard.data_block): it is data, never instructions. The warnings of the tools about phrases
written to steer a model in what they returned (guard.py) go to the step, in `warnings`, and to
the Markdown report, in "Instructions inside the data".

With agent_command, the agent is a program of outside (OpenCode, Claude Code...): each run of
the agent runs that command in the working folder (or in the copy of an attempt), with the
prompt in place of "{prompt}" (or on its standard input, if no argument has it), for at most
command_timeout seconds; a run that runs out of time is one that stopped, and its process
group is killed. Everything else is the same: check, tests, undo, advances, rounds, restarts
and report. Its tools are its own, so the protected files are not safe by them: they are read
before each run and put back after it if it changed them (the step says which, in
`protected`). What it prints goes to the calls log as a single call, with its seconds.

Next to the patches, answers.md has the whole answer of each run, and the report links each step
with its patch and its answer. With propose = true, the working folder is not touched: the agents
work on a copy of it (<workspace>/attempts/0), which stays until the next develop to look at it
(--resume goes on in it), and the patches are what the plan gives; a person applies those they
want, as atlas link proposes a tag and does not write it. A copy (of propose, attempts or
parallel) cannot be larger than max_copy_mb, and the tests cannot name the working folder by its
absolute path: they would test its code, not the copy's.
"""
from __future__ import annotations

import difflib
import fnmatch
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from atlas.agents.hints import shape as _shape, write_hints_draft
from atlas.agents.guard import data_block
from atlas.config import Config, ConfigError

__all__ = ["Plan", "develop", "load_plan", "patch"]

log = logging.getLogger(__name__)

MAX_OUTPUT = 1500          # characters of a test output in a prompt
MAX_CHANGES = 3000         # characters of the diff of what a stopped run changed, in the prompt of its restart
MAX_ANSWER = 400           # characters of an answer of the agent kept for the report and the attempts
WRITES = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")
SHOWN_ERRORS = 15          # errors of the compiler in the Markdown report
MAX_TASK_CHARS = 1200      # a longer task gets a warning from check(): small models do better with small tasks
REQUIREMENT_CHARS = 1200   # characters of each requirement in front of a task
SHOWN_SECTIONS = 2         # sections that state a requirement, in front of a task
SHOWN_CODE = 5             # code elements that cite a requirement, in front of a task
SIMILAR = 2                # requirements like it already implemented, when atlas has similar_requirements
STRAY = ("report", "delete")   # what to do with the tests an agent leaves among the code
MAX_PARALLEL = 2           # tasks at once, until the request limit is shared between processes (serve, run)
TURN_SECONDS = 30          # a turn of an agent (gemma-4-26b), until develop has measured one

FIX_PROMPT = """The test {test} fails. What it printed:

{output}
{code}
Find the cause in the code and fix it. The test is right and cannot be changed: change the code it tests. \
Check every file you change with check_code, and run the test with run_test before you finish.
{attempts}"""
BUILD_FIX_PROMPT = """The code does not compile after the last change. check_code says:

{errors}

Fix these errors without changing what the code does, and check the files again with check_code."""
ADVANCE_PROMPT = """The change does not do all that the task asks yet: the test {test} should get \
further with it, and it fails where it failed before. What it printed:

{output}
{code}
Find why in the code and fix it. The test is right and cannot be changed: change the code it tests. \
Check every file you change with check_code.
"""
RESTART = """{prompt}

This is a fresh start: an earlier run on this stopped after {calls} tool calls{why}, before it \
finished. What it changed in the code, which stays:
{changes}
{check}Go on from the code as it is now: keep what is right, and do not try again what did not work."""
PLANNED = """{task}

How to do it, from a colleague who has read the code and the requirements (follow it unless the code says \
otherwise):
{plan}"""
REQUIREMENTS = """The requirements of this task, as the project states them. Cite their IDs in a \
`{tag}` comment right before each subprogram that implements them (in its spec when it has one).

{blocks}

The task:
{task}"""
TAGS_PROMPT = """No subprogram you changed cites {missing} in its `{tag}` comment. Add those IDs to the \
`{tag}` comment right before each subprogram that implements them, in its spec when it has one \
(`-- {tag} {first}` in Ada, `# {tag} {first}` in Python), without changing what the code does, and check \
the files again with check_code."""
REVIEW_PROMPT = """Review a change to the code of the project. The task it does:

{task}

The change:

{diff}
It compiles and no test got worse. Look for what the compiler and the tests do not see: an exception \
that is swallowed (when others => null, except: pass), a branch that can never run, code or files left \
behind, changes the task does not ask for, a part of the task that is not done. You can read the code; you \
cannot change it. Answer only with JSON, {{"verdict": "ok"}} when there is nothing to fix, or {{"verdict": \
"issues", "issues": [{{"file": "src/x.adb", "line": 12, "issue": "what is wrong and what to do"}}]}}."""
REVIEW_FIX_PROMPT = """A colleague reviewed the change you made for this task:

{task}

and found these issues:
{issues}

Fix them, without breaking what works and without changing what the task does not ask for. Check every \
file you change with check_code."""
RETRY = """

A first attempt at this task was undone, because it broke what worked:
{broken}
Do the task without breaking that."""


@dataclass
class Plan:
    name: str
    tasks: list[dict]
    agent: str = "implementer"
    fix_agent: str = ""
    agent_command: list[str] = field(default_factory=list)
    # TODO: also from task_seconds, the budget of each run, when it is in develop (it is in
    # claude/hungry-kapitsa-257889): the smaller of the two
    command_timeout: float = 1800
    check: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    progress: str = ""
    build_fixes: int = 2
    advance_fixes: int = 2
    fix_rounds: int = 12
    stuck_rounds: int = 2
    restarts: int = 1
    attempts: int = 1
    parallel: int = 1
    planner: str = ""
    task_calls: int = 0
    task_seconds: int = 0
    task_cost: float = 0.0
    require_tags: bool = False
    requirement_chars: int = REQUIREMENT_CHARS
    reviewer: str = ""
    review_fixes: int = 1
    stray: str = "report"
    propose: bool = False
    max_copy_mb: int = 500
    fix_prompt: str = FIX_PROMPT
    advance_prompt: str = ADVANCE_PROMPT


def load_plan(path: str | Path) -> Plan:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"plan {path}: {e}") from None
    known = {f for f in Plan.__dataclass_fields__} - {"name"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"plan {path}: unknown keys {unknown} (valid: {', '.join(sorted(known))})")
    tasks = data.pop("tasks", [])
    if not isinstance(tasks, list) or not all(isinstance(t, dict) and isinstance(t.get("text"), str) for t in tasks):
        raise ConfigError(f"plan {path}: each [[tasks]] needs its text")
    strings = lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)
    for t in tasks:
        unknown = sorted(set(t) - {"id", "text", "advances", "requirements", "after"})
        if unknown or not all(strings(t.get(key, [])) for key in ("advances", "requirements", "after")):
            raise ConfigError(f"plan {path}: a task has id, text, requirements (a list of IDs), advances (a list "
                              "of tests) and after (a list of tasks)" + (f", not {unknown}" if unknown else ""))
    ids = [str(t.get("id") or f"T{i:02d}") for i, t in enumerate(tasks, 1)]
    tasks = [{"id": ids[i], "text": t["text"], "advances": t.get("advances", []),
              "requirements": t.get("requirements", []),
              "after": list(dict.fromkeys(t["after"])) if "after" in t else ids[i - 1:i]}
             for i, t in enumerate(tasks)]
    _check_after(path, tasks)
    if data.get("progress"):
        try:
            re.compile(data["progress"])
        except re.error as e:
            raise ConfigError(f"plan {path}: progress is not a valid regular expression: {e}") from None
    parallel = data.get("parallel", 1)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ConfigError(f"plan {path}: parallel is a whole number, 1 or more (1: one task at a time)")
    for key in ("task_calls", "task_seconds", "task_cost"):
        value = data.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"plan {path}: {key} is a number, 0 or more (0: no limit)")
    if data.get("stray", "report") not in STRAY:
        raise ConfigError(f"plan {path}: stray is {' or '.join(map(repr, STRAY))}, not {data['stray']!r}")
    if "agent_command" in data:
        command = data["agent_command"]
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
            raise ConfigError(f'plan {path}: agent_command is the program and its arguments, as a list: '
                              f'["opencode", "run", "{{prompt}}"]')
        both = [k for k in ("agent", "fix_agent") if k in data]
        if both:
            raise ConfigError(f"plan {path}: agent_command or {both[0]}, not both: agent_command is the agent")
    plan = Plan(name=path.stem, tasks=tasks, **data)
    if plan.agent_command:
        plan.agent = Path(plan.agent_command[0]).name
    plan.fix_agent = plan.fix_agent or plan.agent
    if not plan.tests:
        raise ConfigError(f"plan {path}: no tests: they are what says whether the code works")
    return plan


def _check_after(path: Path, tasks: list[dict]) -> None:
    """The ids of the tasks are unique, and their `after` name tasks of the plan without a cycle."""
    ids = [t["id"] for t in tasks]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise ConfigError(f"plan {path}: two tasks have the id {twice[0]}")
    for t in tasks:
        unknown = [a for a in t["after"] if a not in ids]
        if unknown:
            raise ConfigError(f"plan {path}: task {t['id']} is after {unknown[0]}, which is not a task of the plan")
    after = {t["id"]: t["after"] for t in tasks}
    state: dict[str, int] = {}                # 1: being visited, 2: done

    def visit(task: str, trail: list[str]) -> None:
        if state.get(task) == 2:
            return
        if state.get(task) == 1:
            cycle = trail[trail.index(task):] + [task]
            raise ConfigError(f"plan {path}: the tasks wait for each other: {' after '.join(cycle)}")
        state[task] = 1
        for a in after[task]:
            visit(a, trail + [task])
        state[task] = 2

    for t in ids:
        visit(t, [])


class _Files:
    """The files an agent can change, to put them back as they were."""

    def __init__(self, code):
        self.code = code

    def _all(self) -> dict[Path, bytes]:
        out = {}
        for root in self.code.roots:
            for p in (root.rglob("*") if root.is_dir() else []):
                if p.is_file() and not self.code._excluded(p) and not self.code.protected_file(p):
                    out[p] = p.read_bytes()
        return out

    def snapshot(self) -> dict[Path, bytes]:
        return self._all()

    def restore(self, snapshot: dict[Path, bytes]) -> list[str]:
        changed = []
        for p, data in self._all().items():
            if p not in snapshot:
                p.unlink()
                changed.append(self.code._show(p))
            elif data != snapshot[p]:
                p.write_bytes(snapshot[p])
                changed.append(self.code._show(p))
        for p, data in snapshot.items():
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                changed.append(self.code._show(p))
        return sorted(changed)


class _Budget:
    """What one attempt at a task (or a round) has spent, against task_calls, task_seconds and
    task_cost of the plan (0: no limit)."""

    def __init__(self, plan: Plan):
        self.plan, self.started = plan, time.time()
        self.calls, self.cost, self.exceeded = 0, 0.0, ""

    def spend(self, step: dict) -> None:
        self.calls += step.get("calls", 0)
        self.cost += step.get("cost", 0)

    def spent(self) -> str:
        """Why no more runs start ("task_calls = 40, spent 43"), or "" while there is budget
        left; the first reason stays in `exceeded`."""
        seconds = round(time.time() - self.started)
        for key, used, limit in (("task_calls", self.calls, self.plan.task_calls),
                                 ("task_seconds", seconds, self.plan.task_seconds),
                                 ("task_cost", round(self.cost, 4), self.plan.task_cost)):
            if limit and used >= limit:
                self.exceeded = self.exceeded or f"{key} = {limit:g}, spent {used:g}"
                return self.exceeded
        return ""


def _files(patterns: list[str], base: Path) -> list[str]:
    """The files of some patterns, from `base` (the working folder, or a copy of it), as
    paths relative to it."""
    return sorted({Path(f).relative_to(base).as_posix() for pattern in patterns
                   for f in glob.glob(str(base / pattern), recursive=True) if Path(f).is_file()})


class _Workspace:
    """The code of a plan in one folder, with its tools: the working folder, or the copy of a
    parallel attempt (`base`, its number)."""

    def __init__(self, dev: "Developer", base: Path | None = None, number: int = 0):
        from atlas.code import BuildTools, CodeTools
        self.dev, self.base, self.number = dev, base, number
        self.code = CodeTools.from_config(dev.cfg, base=base)
        self.build = BuildTools.from_config(dev.cfg, self.code, base=base)
        self.files = _Files(self.code)
        self.budget = _Budget(dev.plan)      # of the attempt that runs here now

    def context(self, read_only: bool = False):
        """What the tools of an agent receive: their file and build tools on this folder
        (`read_only`: the file tools change nothing, for the reviewer)."""
        from atlas.agents.runtime import context_for
        from atlas.code import BuildTools, CodeTools
        context = context_for(self.dev.cfg)
        if self.base is not None or read_only:
            context._code_tools = CodeTools.from_config(self.dev.cfg, read_only=read_only, base=self.base)
            context._build_tools = BuildTools.from_config(self.dev.cfg, context._code_tools, base=self.base)
            context._build_tools.hook()
        return context

    def evaluate(self) -> dict:
        """{"check": {file: [errors]} of the files with errors, "tests": {test: {passed, score, output, code_at}}}"""
        check = {}
        for f in _files(self.dev.plan.check, self.code.base):
            res = self.build.check_code(f)
            if not res.get("ok"):
                check[f] = res.get("errors") or [{"message": res.get("error") or res.get("output", "")[-300:]}]
        tests = {}
        for t in self.dev.tests:
            res = self.build.run_test(t)
            output = res.get("output") or res.get("error") or json.dumps(res.get("errors", []))
            tests[t] = {"passed": bool(res.get("passed")), "score": self.dev._score(res), "output": output,
                        "code_at": res.get("code_at", [])}
        return {"check": check, "tests": tests}


class Developer:
    def __init__(self, plan: Plan, cfg: Config, client: Any = None,
                 on_step: Callable[[dict], None] | None = None):
        self.plan, self.cfg, self.client = plan, cfg, client
        self.on_step = on_step or (lambda step: None)
        self.main = _Workspace(self)
        self.code, self.build, self.files = self.main.code, self.main.build, self.main.files
        self.tests = _files(plan.tests, self.code.base)
        from atlas.agents.runtime import definition       # each agent exists, or a ConfigError now and not after a task
        for helper in (plan.planner, plan.reviewer):
            if helper:
                definition(helper, cfg)
        if plan.agent_command and not shutil.which(plan.agent_command[0]):
            raise ConfigError(f"plan {plan.name}: agent_command: {plan.agent_command[0]} is not found")
        compiler = bool(self.build.check_command or self.build.check_commands)
        agents = () if plan.agent_command else (("agent", plan.agent, ["check_code"] * compiler),
                                                ("fix_agent", plan.fix_agent, ["check_code"] * compiler + ["run_test"]))
        for key, name, needs in agents:                  # an agent of outside has its own tools
            tools = definition(name, cfg).tools
            if not set(tools) & set(WRITES):
                raise ConfigError(f"plan {plan.name}: {key} {name} has no tool that changes the code "
                                  f"({', '.join(WRITES)})")
            missing = [t for t in needs if t not in tools]
            if missing:
                raise ConfigError(f"plan {plan.name}: {key} {name} needs {' and '.join(missing)} among its tools: "
                                  "the prompts of develop tell it to check every file it changes"
                                  + (" and to run the failing test" if "run_test" in missing else ""))
        outside = [r for r in self.code.roots if self.code.base not in (r, *r.parents)]
        copies = "propose" if plan.propose else "attempts" if plan.attempts > 1 else "parallel" if plan.parallel > 1 else ""
        if copies and outside:
            raise ConfigError(f"plan {plan.name}: {copies} needs the code roots inside the folder of "
                              f"{cfg.path.name}, to copy them (not {self.code._show(outside[0])})")
        if not self.tests:
            raise ConfigError(f"plan {plan.name}: no file matches its tests {plan.tests}")
        for t in self.tests:
            p, err = self.code._resolve(t)
            if err:
                raise ConfigError(f"plan {plan.name}: run_test cannot run the test {t}: {err['error']}")
            if not self.code.protected_file(p):
                raise ConfigError(f"plan {plan.name}: the test {t} is not protected ([sources.code] protected, "
                                  "e.g. [\"tests/**\"]): an agent could change what judges its code")
        if copies:
            self._copyable(copies)
        if plan.check and not (self.build.check_command or self.build.check_commands):
            raise ConfigError(f"plan {plan.name} checks {plan.check}, but [sources.code] has no check_command")
        unknown = [t for t in self.tests if Path(t).suffix.lstrip(".").lower() not in self.build.test_commands
                   and not self.build.test_command]
        if unknown:
            raise ConfigError(f"plan {plan.name}: no command runs {unknown[0]} ([sources.code] test_command or "
                              f"test_commands)")
        self.graph = self._graph()
        anchored: dict[str, dict] = {}
        for task in plan.tasks:                  # the requirements, in the graph, as the graph names them
            ids = []
            for r in task["requirements"]:
                t = self.graph.trace(r)
                if "error" in t:
                    similar = f" (similar: {', '.join(t['similar'][:5])})" if t.get("similar") else ""
                    raise ConfigError(f"plan {plan.name}, task {task['id']}: requirement {r!r} is not in the graph of "
                                      f"{cfg.workspace / 'atlas.db'}{similar}")
                if t["id"] not in ids:
                    ids.append(t["id"])
            task["requirements"] = ids
        for task in plan.tasks:                  # each pattern of advances, the tests it names
            found = []
            for pattern in task["advances"]:
                named = [t for t in self.tests if fnmatch.fnmatch(t, pattern) or Path(t).name == pattern]
                if not named:
                    raise ConfigError(f"plan {plan.name}, task {task['id']}: advances {pattern!r} is none of its "
                                      f"tests")
                found += [t for t in named if t not in found]
            from_graph = not found and bool(task["requirements"])
            task["advances"] = found or self.verifying(task["requirements"])
            if task["requirements"]:
                anchored[task["id"]] = {"requirements": task["requirements"], "advances": task["advances"],
                                        **({"advances_from": "requirements"} if from_graph else {})}
        self.report: dict = {"plan": plan.name, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": [],
                             "tests": self.tests, **({"requirements": anchored} if anchored else {})}
        self.path = cfg.workspace / "reports" / f"develop-{plan.name}"
        self.errors: dict[str, dict] = {}      # the errors of the compiler, by their message without names
        self._answers: dict[int, str] = {}     # the whole answer of each step (the report keeps its start)
        self._lock = threading.RLock()         # the report and the calls log, shared by parallel attempts
        self._started, self._wall_before = time.time(), 0     # the wall time of this run, and of earlier ones
        self.parallel = min(plan.parallel, MAX_PARALLEL)
        self.warnings = [f"parallel = {plan.parallel} goes as {MAX_PARALLEL}: the request limit is not shared yet "
                         "with the other processes of atlas (serve, run), and more agents at once would take it "
                         "from them"] * (plan.parallel > MAX_PARALLEL)

    def load(self) -> str:
        """A warning if the agents at once (parallel x attempts) would fill [network]
        requests_per_minute, each asking for a turn every TURN_SECONDS (or as often as the runs
        of the report took, with --resume); "" if not, or with no limit."""
        from atlas.llm import from_config
        rpm = from_config(self.cfg).llm.net.requests_per_minute
        agents = self.parallel * max(1, self.plan.attempts)
        if not rpm or agents <= 1:
            return ""
        runs = [s for s in self.report["steps"] if s.get("calls") and s.get("seconds")]
        turn = sum(s["seconds"] for s in runs) / sum(s["calls"] + 1 for s in runs) if runs else TURN_SECONDS
        wanted = agents * 60 / max(turn, 1)
        if wanted <= rpm:
            return ""
        return (f"{agents} agents at once (parallel {self.parallel} x attempts {self.plan.attempts}) ask for about "
                f"{wanted:.0f} requests per minute, a turn every {turn:.0f} s"
                + (" (measured)" if runs else "") + f", more than [network] requests_per_minute = {rpm}: they "
                "will wait for each other, and atlas serve or atlas run here get fewer")

    # --- the requirements of the tasks ------------------------------------------------------
    def _graph(self):
        """The graph of the workspace, if a task names requirements (None otherwise); a
        ConfigError if atlas run has not built it."""
        if not any(task["requirements"] for task in self.plan.tasks):
            return None
        from atlas.graph import KnowledgeGraph
        db = self.cfg.workspace / "atlas.db"
        graph = KnowledgeGraph.from_config(self.cfg) if db.exists() else None
        if graph is None or not any(d.get("kind") == "requirement" for _, d in graph.G.nodes(data=True)):
            raise ConfigError(f"plan {self.plan.name}: its tasks name requirements, but {db} has no graph with "
                              f"requirements: atlas run first")
        return graph

    def verifying(self, requirements: list[str]) -> list[str]:
        """The tests of the plan that verify some of `requirements`, in the order of the plan:
        those that cite them (trace verified_by, "tag"), and those of the test procedures linked
        to them with `verifies` (STP-0101 verifies SRS-0113): those that cite the procedure, and
        those named after it (test_stp_0101.py, as atlas tests names them)."""
        if not requirements:
            return []
        from atlas.code.procedures import slugs
        files, names = set(), set()

        def cited_by(r: str) -> None:
            for v in self.graph.trace(r).get("verified_by", []):
                if v["how"] == "tag" and self.graph.G.nodes[v["id"]].get("file"):
                    files.add(Path(self.graph.G.nodes[v["id"]]["file"]).resolve())

        for r in requirements:
            cited_by(r)
            for link in self.graph.trace(r).get("links_in", []):
                if link["type"].lower() == "verifies":
                    cited_by(link["id"])
                    names.add(slugs(link["id"])["slug"])
        named = lambda test: any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", Path(test).stem.lower())
                                 for n in names)
        return [t for t in self.tests if Path(t).resolve() in files or named(t)]

    def requirement(self, r: str) -> str:
        """What goes in front of a task about requirement `r`: its text, the sections that state
        it and the code that cites it, at most plan.requirement_chars characters."""
        t = self.graph.trace(r)
        lines = [f"{r}" + (f" ({t['module']})" if t.get("module") else "") + ": "
                 + (t.get("text") or "(no text in DOORS)")]
        for d in t["documents"][:SHOWN_SECTIONS]:
            lines.append(f"  Stated in {d.get('document')} › {d.get('section') or ''} {d.get('title') or ''}".rstrip()
                         + (f": {d['text']}" if d.get("text") else ""))
        code = t["implemented_by"]["items"]
        if code:
            lines.append("  Code that already cites it: " + ", ".join(
                f"{c['id']} ({c.get('file', '')}" + (f", lines {c['lines']}" if c.get("lines") else "") + ")"
                for c in code[:SHOWN_CODE]) + (" and more" if t["implemented_by"]["count"] > SHOWN_CODE else ""))
        else:
            lines.append("  No code cites it yet.")
        from atlas.agents import context
        similar = getattr(context, "similar_requirements", None)     # 3.4 of the analysis, when it is there
        if similar is not None:
            for x in (similar(self.graph, r, k=SIMILAR).get("similar") or []):
                lines.append(f"  Like it, already implemented: {x['id']} in {x['code']}"
                             + (f" ({x['signature']})" if x.get("signature") else ""))
        text, most = "\n".join(lines), max(200, self.plan.requirement_chars)
        return text if len(text) <= most else text[:most - 1].rstrip() + "…"

    def task_text(self, task: dict) -> str:
        """The text of a task with its requirements in front (see the module)."""
        if not task["requirements"]:
            return task["text"]
        return REQUIREMENTS.format(tag=self._tag(), task=task["text"],
                                   blocks="\n\n".join(self.requirement(r) for r in task["requirements"]))

    def _tag(self) -> str:
        return ((self.cfg["sources"].get("code") or {}).get("req_tag") or "Req:").strip()

    def untagged(self, requirements: list[str], before: dict[Path, bytes], ws: _Workspace | None = None) -> list[str]:
        """Those of `requirements` that no subprogram (or declaration) changed since `before`
        cites (in the working folder, or in `ws`), with the analyzers of [sources.code]
        languages, without the pipeline. A declaration in a spec counts when its subprogram
        changed in the body."""
        ws = ws or self.main
        from atlas import plugins
        sc = self.cfg["sources"].get("code") or {}
        by_ext = {}
        for lang in sc.get("languages", ["ada"]):
            try:
                analyzer = plugins.get("code_analyzer", lang, self.cfg["plugins"]["disabled"])(sc)
            except LookupError:
                continue
            by_ext.update({ext: analyzer for ext in analyzer.extensions})
        now, units = ws.files.snapshot(), []
        for p in sorted(set(before) | set(now)):
            if p not in now or before.get(p) == now[p] or p.suffix.lower() not in by_ext:
                continue
            try:
                unit = by_ext[p.suffix.lower()].analyze_file(Path(ws.code._show(p)), now[p])
            except Exception as e:               # an analyzer that cannot read it: nothing cited there
                log.warning("develop: %s not analysed: %s", ws.code._show(p), e)
                continue
            units.append((unit, _changed_lines(before.get(p, b""), now[p])))
        elements = lambda unit: [*unit.subprograms, *unit.declarations]

        def span(e: dict) -> tuple[int, int]:            # with the comment above it, where its tag is
            start = e.get("start_line") or e.get("line") or 0
            return start - (e.get("doc") or "").count("\n") - 1, e.get("end_line") or start
        changed = {e["name"].lower() for unit, lines in units for e in elements(unit)
                   if any(span(e)[0] <= n <= span(e)[1] for n in lines)}
        cited = {r.upper() for unit, _ in units for e in elements(unit) if e["name"].lower() in changed
                 for r in e.get("reqs", [])}
        return [r for r in requirements if r.upper() not in cited]

    # --- what the code does --------------------------------------------------------------
    def _score(self, result: dict) -> float:
        if result.get("passed"):
            return math.inf
        found = re.findall(self.plan.progress, result.get("output", "")) if self.plan.progress else []
        return float(found[-1]) if found else 0.0

    def evaluate(self, ws: _Workspace | None = None) -> dict:
        """What the code of the working folder (or of `ws`) does: see _Workspace.evaluate."""
        return (ws or self.main).evaluate()

    @staticmethod
    def passing(state: dict) -> int:
        return sum(t["passed"] for t in state["tests"].values())

    def broken(self, before: dict, after: dict) -> list[str]:
        """What `after` broke of what `before` did: files that no longer compile, tests that
        stop sooner."""
        out = [f"{f} does not compile: {_message(errors[0])}" for f, errors in after["check"].items()
               if f not in before["check"]]
        for t, a in after["tests"].items():
            b = before["tests"].get(t)
            if b and a["score"] < b["score"]:
                was = "passed" if b["passed"] else f"got to {b['score']:g}"
                now = f"got to {a['score']:g}" if self.plan.progress else "fails"
                out.append(f"{t} {was} and now {now}: {_last_lines(a['output'])}")
        return out

    # --- the agent -------------------------------------------------------------------------
    def run(self, agent: str, prompt: str, kind: str, task: str, ws: _Workspace | None = None,
            read_only: bool = False) -> dict:
        """A run of the agent, started again with a fresh conversation, at most `restarts`
        times, when it stops before finishing (see the module): the step of the last one."""
        ws = ws or self.main
        before = ws.files.snapshot() if self.plan.restarts else {}
        step = self._run_once(agent, prompt, kind, task, ws, read_only)
        for _ in range(self.plan.restarts):
            if not step.get("stopped") or step.get("error") or ws.budget.spent():
                break
            changes, check = self._changes(before, ws)
            step["outcome"] = "restarted"
            self.save()
            self.on_step(step)
            why = (" without changing the code in its last steps" if step.get("stalled") else
                   f" when its {self.plan.command_timeout:g} s ran out" if step.get("timed_out") else "")
            step = self._run_once(agent, RESTART.format(prompt=prompt, calls=step["calls"], why=why,
                                                        changes=data_block(changes, "diff") if changes
                                                        else "(nothing)", check=check),
                                  f"{kind}-restart", task, ws, read_only)
        return step

    def _changes(self, before: dict, ws: _Workspace) -> tuple[str, str]:
        """(the diff of the files changed since `before`, what check_code says of them now)."""
        now, parts, checks = ws.files.snapshot(), [], []
        for p in sorted(set(before) | set(now)):
            if before.get(p) == now.get(p):
                continue
            shown = ws.code._show(p)
            old = (before.get(p) or b"").decode("utf-8", "replace").splitlines(keepends=True)
            new = (now.get(p) or b"").decode("utf-8", "replace").splitlines(keepends=True)
            parts.append("".join(difflib.unified_diff(old, new, fromfile=shown, tofile=shown, n=1)))
            if p in now and (ws.build.check_commands.get(p.suffix.lower().lstrip(".")) or ws.build.check_command):
                res = ws.build.check_code(shown)
                errors = res.get("errors") or []
                if errors:
                    checks.append(f"check_code says of {shown}:\n" + "\n".join(
                        f"  line {e.get('line')}: {e.get('message')}" for e in errors[:5]))
        diff = "\n".join(parts)
        if len(diff) > MAX_CHANGES:
            diff = diff[:MAX_CHANGES] + "\n… (more changes: read the files)"
        return diff, ("\n".join(checks) + "\n") if checks else ""

    def _run_once(self, agent: str, prompt: str, kind: str, task: str, ws: _Workspace,
                  read_only: bool = False) -> dict:
        from atlas.agents.runtime import run_agent
        t0 = time.time()
        step = {"n": len(self.report["steps"]) + 1, "kind": kind, "task": task, "agent": agent,
                **({"attempt": ws.number} if ws.number else {})}
        res: dict = {"calls": []}
        record: list = []
        try:
            if self.plan.agent_command and kind not in ("plan", "review"):    # the planner and the reviewer
                res = self._command(prompt, ws, record)                       # are agents of atlas
            else:
                res = run_agent(agent, prompt, self.cfg, ws.context(read_only), client=self.client, record=record)
            step.update({"calls": len(res["calls"]), "stopped": res["stopped"], "tokens": res.get("tokens"),
                         **({"stalled": True} if res.get("stalled") else {}),
                         **{k: res[k] for k in ("timed_out", "protected", "error") if res.get(k)},
                         **({"cost": res["cost"]} if "cost" in res else {}), "answer": res["answer"][:MAX_ANSWER]})
        except Exception as e:                   # the model server failed: the task did not happen
            step.update({"calls": 0, "stopped": True, "error": f"{type(e).__name__}: {e}"[:MAX_ANSWER], "answer": ""})
        step["seconds"] = round(time.time() - t0)
        ws.budget.spend(step)
        use, errors = tool_use(record)
        with self._lock:
            step["n"] = len(self.report["steps"]) + 1
            step.update(use)
            for e in errors:
                key = _shape(e.get("message", ""))
                seen = self.errors.setdefault(key, {"message": e.get("message", ""), "count": 0, "hint": False})
                seen["count"] += 1
                seen["hint"] = seen["hint"] or bool(e.get("hint"))
            self.report["steps"].append(step)
            self._answers[step["n"]] = res.get("answer", "")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(f"{self.path}.calls.jsonl", "a", encoding="utf-8") as log:   # what it did, to study it
                log.write(json.dumps({"n": step["n"], "kind": kind, "task": task,
                                      **({"attempt": ws.number} if ws.number else {}), "prompt": prompt,
                                      "calls": record, "answer": res.get("answer", "")},
                                     ensure_ascii=False, default=str) + "\n")
        return step

    def _command(self, prompt: str, ws: _Workspace, record: list) -> dict:
        """A run of the agent of agent_command in the folder of `ws` (see the module): what
        run_agent returns, and the protected files it changed, put back."""
        template = self.plan.agent_command
        command = [a.replace("{prompt}", prompt) for a in template]
        stdin = None if any("{prompt}" in a for a in template) else prompt
        protected = self._protected(ws)
        t0 = time.time()
        try:
            output, errors, code = _execute(command, ws.code.base, stdin, self.plan.command_timeout)
        finally:                                 # also after a Ctrl+C
            put_back = self._put_back(protected, ws)
        result = {"exit_code": code, "output": output, **({"stderr": errors} if errors.strip() else {}),
                  **({"protected": put_back} if put_back else {})}
        record.append({"name": "agent_command", "args": {"command": template}, "result": result,
                       "seconds": round(time.time() - t0, 2)})
        error = (f"{template[0]} exited with {code}: {_last_lines(errors or output)}" if code else "")
        return {"calls": [record[-1]], "stopped": code is None, "timed_out": code is None, "tokens": None,
                "answer": output.strip()[-MAX_ANSWER:], "protected": put_back, "error": error}

    def _protected(self, ws: _Workspace) -> dict[Path, bytes]:
        """The protected files of the code roots of `ws` and the tests of the plan, with their content."""
        code = ws.code
        files = {p for root in code.roots for p in (root.rglob("*") if root.is_dir() else [])
                 if p.is_file() and code.protected_file(p) and not code._excluded(p)}
        files |= {code.base / t for t in _files(self.plan.tests, code.base)}
        return {p: p.read_bytes() for p in files}

    def _put_back(self, before: dict[Path, bytes], ws: _Workspace) -> list[str]:
        """Puts the protected files back as they were in `before`: those that changed, and
        those that appeared or went away."""
        now = self._protected(ws)
        changed = sorted(p for p in before.keys() | now.keys() if before.get(p) != now.get(p))
        for p in changed:
            if p in before:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(before[p])
            else:
                p.unlink()
        return [ws.code._show(p) for p in changed]

    def compiled(self, agent: str, task: str, before: dict, after: dict, ws: _Workspace | None = None) -> dict:
        """`after`, or the state after the build fixes of what check finds that `before` had not."""
        new_errors = lambda state: set(state["check"]) - set(before["check"])
        for _ in range(self.plan.build_fixes):
            if not new_errors(after) or (ws or self.main).budget.spent():
                break
            errors = "\n".join(f"{f}:{e.get('line', '')}: {_message(e)}" for f, es in after["check"].items()
                               for e in es[:10])
            fix = self.run(agent, BUILD_FIX_PROMPT.format(errors=data_block(errors, "check_code")), "build-fix",
                           task, ws)
            after = self.evaluate(ws)
            fix["outcome"] = "errors remain" if new_errors(after) else "compiles"
            self._done(fix, after)
        return after

    @staticmethod
    def not_advanced(tests: list[str], before: dict, after: dict) -> list[str]:
        """Those of `tests` that fail where they failed before (one that passes has advanced)."""
        return [t for t in tests if not after["tests"][t]["passed"]
                and after["tests"][t]["score"] <= before["tests"][t]["score"]]

    def advanced(self, agent: str, task: str, advances: list[str], before: dict, after: dict,
                 ws: _Workspace | None = None) -> dict:
        """The turns that make the tests of `advances` get further (see the module): the state
        after them. A turn that breaks something is undone, and the turns end there."""
        ws = ws or self.main
        for _ in range(self.plan.advance_fixes):
            stuck = self.not_advanced(advances, before, after)
            if not stuck or ws.budget.spent():
                break
            result = after["tests"][stuck[0]]
            snapshot = ws.files.snapshot()
            fix = self.run(agent, self.plan.advance_prompt.format(
                test=stuck[0], output=_output(stuck[0], result), code=_cited(result)), "advance-fix", task, ws)
            fixed = self.compiled(agent, task, after, self.evaluate(ws), ws)
            broken = self.broken(after, fixed)
            if broken:
                fix.update(outcome="undone", undone=ws.files.restore(snapshot), broke=broken)
                self._done(fix, after)
                break
            after = fixed
            fix["outcome"] = "not yet" if self.not_advanced(advances, before, after) else "advanced"
            self._done(fix, after)
        return after

    def reviewed(self, agent: str, prompt: str, task: str, snapshot: dict, before: dict, after: dict,
                 ws: _Workspace) -> dict:
        """The review of a task that compiles and breaks nothing (see the module): the reviewer
        reads the task and the diff since `snapshot`; its issues go back to the agent at most
        review_fixes times (0: they are only reported). The state after them; a fix that breaks
        something is undone."""
        for turn in range(max(1, self.plan.review_fixes)):
            diff, _ = self._changes(snapshot, ws)
            if not diff.strip():
                break
            review = self.run(self.plan.reviewer, REVIEW_PROMPT.format(task=prompt, diff=diff), "review", task, ws,
                              read_only=True)
            verdict = _verdict(self._answers.get(review["n"], ""))
            review["outcome"] = "no verdict" if verdict is None else "issues" if verdict else "ok"
            if verdict:
                review["issues"] = verdict
            self._done(review, after)
            if not verdict or turn >= self.plan.review_fixes:
                break
            fix_snapshot = ws.files.snapshot()
            issues = "\n".join(f"- {i.get('file', '?')}" + (f", line {i['line']}" if i.get("line") else "")
                               + f": {i.get('issue', '')}" for i in verdict)
            fix = self.run(agent, REVIEW_FIX_PROMPT.format(task=prompt, issues=issues), "review-fix", task, ws)
            fixed = self.compiled(agent, task, after, self.evaluate(ws), ws)
            broken = self.broken(after, fixed)
            if broken:
                fix.update(outcome="undone", undone=ws.files.restore(fix_snapshot), broke=broken)
                self._done(fix, after)
                break
            after = fixed
            fix["outcome"] = "kept"
            self._done(fix, after)
        return after

    def attempt(self, agent: str, prompt: str, kind: str, task: str, before: dict,
                advances: list[str] = (), ws: _Workspace | None = None) -> tuple[dict, dict, list]:
        """One change of the agent with its build fixes, the turns that make the tests of
        `advances` get further and, with a reviewer, its review: (step, state after, what it
        broke). What breaks something is undone, and the state is `before` again. No more runs
        start once its budget is spent (task_calls, task_seconds, task_cost)."""
        ws = ws or self.main
        ws.budget = _Budget(self.plan)
        snapshot, t0, since = ws.files.snapshot(), time.time(), len(self.report["steps"])
        try:
            step = self.run(agent, prompt, kind, task, ws)
            if self.plan.stray == "delete":
                deleted = self.strays(snapshot, ws)
                if deleted:
                    step["deleted"] = deleted
            after = self.compiled(agent, task, before, self.evaluate(ws), ws)
            broken = self.broken(before, after)
            if not broken and advances:
                after = self.advanced(agent, task, list(advances), before, after, ws)
                stuck = self.not_advanced(list(advances), before, after)
                if stuck:
                    step["not_advanced"] = stuck
            compiles = not (set(after["check"]) - set(before["check"]))
            if not broken and compiles and self.plan.reviewer and kind in ("task", "task-retry"):
                after = self.reviewed(agent, prompt, task, snapshot, before, after, ws)
            if broken:
                step["undone"] = ws.files.restore(snapshot)
                step["broke"] = broken
                after = before
            else:
                created = sorted(ws.code._show(p) for p in ws.files.snapshot() if p not in snapshot)
                if created:
                    step["created"] = created
        except BaseException as e:               # Ctrl+C: nothing is left half done
            self._interrupted(agent, kind, task, ws, snapshot, t0, since, e)
            raise
        if ws.budget.exceeded:
            step["budget_exceeded"] = ws.budget.exceeded
        return step, after, broken

    def _interrupted(self, agent: str, kind: str, task: str, ws: _Workspace, snapshot: dict, t0: float,
                     since: int, e: BaseException, copies: bool = False) -> None:
        """An attempt cut short: its files back as they were, and its steps `interrupted` (those
        of its runs since step `since` that had no outcome yet, or a new one), for --resume to
        do it again. `copies`: the runs were the parallel attempts, each in its copy."""
        undone = ws.files.restore(snapshot)
        why = {} if isinstance(e, KeyboardInterrupt) else {"error": f"{type(e).__name__}: {e}"[:MAX_ANSWER]}
        with self._lock:
            steps = [s for s in self.report["steps"][since:] if s["task"] == task and "outcome" not in s
                     and (copies or s.get("attempt", 0) == ws.number)]
            if not steps:
                steps = [{"n": len(self.report["steps"]) + 1, "kind": kind, "task": task, "agent": agent,
                          **({"attempt": ws.number} if ws.number else {}), "calls": 0,
                          "seconds": round(time.time() - t0)}]
                self.report["steps"].append(steps[0])
            for s in steps:
                s.update(outcome="interrupted", **why)
            if undone:
                steps[0]["undone"] = undone
        self.save()
        for s in steps:
            self.on_step(s)

    def strays(self, snapshot: dict, ws: _Workspace) -> list[str]:
        """Deletes the files created since `snapshot` that are tests ([sources.code]
        test_patterns) and not protected, and returns them: a test of the agent among the code.
        Files only, never a folder, nor a new file that is not a test."""
        patterns = (self.cfg["sources"].get("code") or {}).get("test_patterns", [])
        deleted = []
        for p in sorted(set(ws.files.snapshot()) - set(snapshot)):
            shown = ws.code._show(p)
            if _is_test(shown, patterns) and not ws.code.protected_file(p):
                p.unlink()
                deleted.append(shown)
        return deleted

    def attempts(self, agent: str, prompt: str, kind: str, task: str, before: dict,
                 advances: list[str] = (), home: _Workspace | None = None) -> tuple[dict, dict, list]:
        """attempt(), or with plan.attempts > 1 that many at once, each in a copy of the working
        folder (or of `home`, the copy of a task with parallel); the best one that breaks nothing
        wins and its files come to that folder (see the module). The others end "not chosen", or
        "undone" if they broke something."""
        home = home or self.main
        if self.plan.attempts <= 1:
            return self.attempt(agent, prompt, kind, task, before, advances, home)
        prefix = "" if home is self.main else f"{home.base.name}-"
        copies = [self._copy(f"{prefix}{n}", n, home) for n in range(1, self.plan.attempts + 1)]
        snapshot, t0, since = home.files.snapshot(), time.time(), len(self.report["steps"])
        try:
            with ThreadPoolExecutor(max_workers=len(copies)) as pool:
                results = list(pool.map(lambda ws: self.attempt(agent, prompt, kind, task, before, advances, ws),
                                        copies))
            best = self._best(results)
            for i, (step, after, broken) in enumerate(results):
                if i != (best if best is not None else 0):
                    step["outcome"] = "undone" if broken else "not chosen"
                    self._done(step, after)
            if best is None:                                 # every one broke something: as one undone
                return results[0]
            winner = copies[best]
            home.files.restore({home.code.base / p.relative_to(winner.code.base): data
                                for p, data in winner.files.snapshot().items()})
            after = self.evaluate(home)
            step = results[best][0]
            step["chosen"] = f"{best + 1} of {len(copies)}"
            return step, after, self.broken(before, after)
        except BaseException as e:               # Ctrl+C, here after the runs in progress end
            self._interrupted(agent, kind, task, home, snapshot, t0, since, e, copies=True)
            raise
        finally:
            for ws in copies:
                shutil.rmtree(ws.base, ignore_errors=True)

    def _copy(self, name: str, number: int = 0, home: _Workspace | None = None,
              folder: str = "attempts") -> _Workspace:
        """A copy of the working folder (or of `home`) in <workspace>/<folder>/<name>, without
        .git, the workspace and __pycache__, and with the build folder, where the tests run, as
        its own: for a parallel attempt (its `number`), for a task with parallel, or ("0") for
        propose."""
        source, target = (home or self.main).code.base, self.cfg.workspace / folder / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, symlinks=True, ignore=self._skip())
        build, home_build = target / ".atlas" / "build", (home or self.main).build.build_dir
        if home_build.is_dir() and not build.exists():    # the build folder of BuildTools in a copy
            shutil.copytree(home_build, build, symlinks=True)
        return _Workspace(self, target, number)

    def _skip(self) -> Callable[[str, list[str]], set[str]]:
        """What a copy of the working folder leaves out (for shutil.copytree)."""
        workspace = self.cfg.workspace.resolve()

        def skip(folder: str, names: list[str]) -> set[str]:
            out = {n for n in names if n in (".git", "__pycache__")}
            if Path(folder).resolve() == workspace.parent:
                out.add(workspace.name)
            return out
        return skip

    def _copyable(self, why: str) -> None:
        """A ConfigError if the working folder cannot be copied for `why` (propose, attempts):
        tests that name it by its absolute path (they would test its code, not the copy's), or
        more than max_copy_mb with the build folder."""
        base = self.code.base
        here = {str(base), str(base.resolve()), base.resolve().as_posix()}
        for t in self.tests:
            if any(h in (base / t).read_text(encoding="utf-8", errors="replace") for h in here):
                raise ConfigError(f"plan {self.plan.name}: {why} runs the tests in a copy of the working folder, and "
                                  f"{t} names it by its absolute path ({base}): make its paths relative to the "
                                  f"test file")
        limit, size, skip = self.plan.max_copy_mb * 2**20, 0, self._skip()
        for folder in {self.code.base, self.build.build_dir.resolve()}:
            for root, dirs, files in os.walk(folder):
                left = skip(root, dirs + files)
                dirs[:] = [d for d in dirs if d not in left]
                size += sum(os.lstat(os.path.join(root, f)).st_size for f in files if f not in left)
                if size > limit:
                    raise ConfigError(f"plan {self.plan.name}: {why} copies the working folder, and it has more than "
                                      f"{self.plan.max_copy_mb} MB (without .git and the workspace, with the build "
                                      f"folder): raise max_copy_mb in the plan, or leave {why} out")

    def _best(self, results: list[tuple[dict, dict, list]]) -> int | None:
        """The index of the best attempt that broke nothing: more tests passing, then further
        in the others, fewer files that do not compile, fewer calls. None if all broke something."""
        good = [i for i, (_, _, broken) in enumerate(results) if not broken]
        if not good:
            return None

        def key(i: int) -> tuple:
            step, after, _ = results[i]
            further = sum(t["score"] for t in after["tests"].values() if t["score"] != math.inf)
            return self.passing(after), further, -len(after["check"]), -step.get("calls", 0)
        return max(good, key=key)

    def _done(self, step: dict, state: dict) -> None:
        step["tests_passed"] = self.passing(state)
        self.save()
        self.on_step(step)

    # --- the plan --------------------------------------------------------------------------
    def check(self) -> dict:
        """What the plan starts from, without a model and without changing anything: check_code
        on the files of `check`, the tests run once, and the tasks too long for a small model.
        The plan itself was checked when the Developer was made (see the module)."""
        state = self.evaluate()
        return {"plan": self.plan.name, "tasks": len(self.plan.tasks),
                "agents": {k: v for k, v in (("agent", self.plan.agent), ("fix_agent", self.plan.fix_agent),
                                             ("planner", self.plan.planner)) if v},
                "tests_passed": self.passing(state),
                "tests": {t: "passes" if r["passed"] else _last_lines(r["output"]) for t, r in state["tests"].items()},
                "check_errors": {f: [f"line {e.get('line', '?')}: {_message(e)}" for e in errors[:3]]
                                 for f, errors in state["check"].items()},
                "warnings": [f"task {t['id']} has {len(t['text'])} characters, more than {MAX_TASK_CHARS}: a small "
                             "model does better with smaller tasks" for t in self.plan.tasks
                             if len(t["text"]) > MAX_TASK_CHARS] + self.warnings + [w for w in [self.load()] if w]}

    @staticmethod
    def _outcome(step: dict, broken: list) -> str:
        """Of a task: undone if it broke something; error if the run failed (the model server)
        and nothing broke, which is not a task done; kept otherwise."""
        return "undone" if broken else "error" if step.get("error") else "kept"

    def tasks(self, state: dict, done: set[str] = frozenset()) -> dict:
        """The tasks of the plan, but those of `done` (finished in an earlier run, --resume),
        each once those of its `after` are finished: in the working folder one by one, or with
        parallel those that can at once, each in a copy (see the module). What each one leaves
        in the code goes to its patch."""
        pending = [t for t in self.plan.tasks if t["id"] not in done]
        finished = {t["id"] for t in self.plan.tasks} - {t["id"] for t in pending}
        again: list[dict] = []          # undone when they came to the working folder: alone, on the code of now
        running: dict = {}              # future -> (task, its copy, its files then, merges then, steps then)
        merged: list[tuple[str, set]] = []          # (task, files) of each one brought from its copy
        started, spent, conflicts = time.time(), 0.0, 0
        pool = ThreadPoolExecutor(max_workers=self.parallel) if self.parallel > 1 else None
        try:
            while pending or running or again:
                ready = [t for t in pending if all(a in finished for a in t["after"])]
                if not running and (again or len(ready) <= 1 or pool is None):
                    task = again.pop(0) if again else ready[0]
                    if task in pending:
                        pending.remove(task)
                    t0, before = time.time(), self.main.files.snapshot()
                    step, state = self._task(task, state)
                    self._patched(step, before, task["id"])          # with the tags of its tag-fix
                    if step.get("untagged") and self.plan.require_tags:
                        self.save()                      # shown before its tag-fix (_task)
                    else:
                        self._done(step, state)
                    finished.add(task["id"])
                    spent += time.time() - t0
                    continue
                while not again and ready and len(running) < self.parallel:
                    task = ready.pop(0)
                    pending.remove(task)
                    ws = self._copy(re.sub(r"[^\w.-]", "_", task["id"]), home=self.main, folder="tasks")
                    with self._lock:
                        since = len(self.report["steps"])
                    running[pool.submit(self._timed, task, state, ws)] = (task, ws, self._relative(ws),
                                                                          len(merged), since)
                ended, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in [f for f in running if f in ended]:
                    step, _, seconds = future.result()
                    task, ws, base, at, _ = running.pop(future)
                    state, brought = self._merge(task, ws, base, step, state, merged[at:])
                    shutil.rmtree(ws.base, ignore_errors=True)
                    if brought is None:                 # its time is not that of the task: it goes again
                        conflicts += 1
                        again.append(task)
                    else:
                        spent += seconds
                        merged.append((task["id"], brought))
                        finished.add(task["id"])
        except BaseException:                    # Ctrl+C: what the copies did is not brought, and goes again
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            self._unmerged([(task["id"], since) for task, _, _, _, since in running.values()])
            raise
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
                shutil.rmtree(self.cfg.workspace / "tasks", ignore_errors=True)
        if self.plan.parallel > 1:
            wall = time.time() - started
            self.report["schedule"] = {"parallel": self.parallel, "tasks_seconds": round(spent),
                                       "wall_seconds": round(wall), "speedup": round(spent / wall, 2) if wall else 1.0,
                                       "conflicts": conflicts}
            self.save()
        return state

    def _task(self, task: dict, state: dict, ws: _Workspace | None = None) -> tuple[dict, dict]:
        """A task in the working folder (or in `ws`, its copy): its plan, its attempts, its
        retry if it broke something and, kept with requirements, which of them no changed
        subprogram cites (and its tag-fix with require_tags). (its last step, with its
        outcome, the state after it)."""
        before = (ws or self.main).files.snapshot()
        text = self.planned(task, state, ws)
        step, state, broken = self.attempts(self.plan.agent, text, "task", task["id"], state, task["advances"], ws)
        step["outcome"] = self._outcome(step, broken)
        if broken:
            self._done(step, state)
            step, state, broken = self.attempts(self.plan.agent, text + RETRY.format(
                broken=data_block("\n".join(f"- {b}" for b in broken), "the tests")), "task-retry", task["id"],
                state, task["advances"], ws)
            step["outcome"] = self._outcome(step, broken)
        if step["outcome"] == "kept" and task["requirements"]:
            step["untagged"] = self.untagged(task["requirements"], before, ws)
        if step.get("untagged") and self.plan.require_tags:
            self._done(step, state)
            state = self.tag_fix(task, step["untagged"], before, state, ws)
        return step, state

    def _timed(self, task: dict, state: dict, ws: _Workspace) -> tuple[dict, dict, float]:
        """_task in its copy, with its time."""
        t0 = time.time()
        step, state = self._task(task, state, ws)
        return step, state, time.time() - t0

    @staticmethod
    def _relative(ws: _Workspace) -> dict[Path, bytes]:
        """The files an agent can change in a folder, by their path relative to it."""
        return {p.relative_to(ws.code.base): data for p, data in ws.files.snapshot().items()}

    def _merge(self, task: dict, ws: _Workspace, base: dict, step: dict, state: dict,
               since: list[tuple[str, set]]) -> tuple[dict, set | None]:
        """The files that a task changed in its copy (`ws`, from `base`) brought to the working
        folder, and the tests run there: (the state now, the files brought). None instead of
        the files when it cannot be: a task of `since` (those brought after it started) changed
        one of them, or together they break what worked; the step says so, and the working
        folder stays as it was."""
        final = self._relative(ws)
        changed = {p for p in set(base) | set(final) if base.get(p) != final.get(p)}
        before = self.main.files.snapshot()
        now = {p.relative_to(self.code.base): data for p, data in before.items()}
        clash = sorted(p.as_posix() for p in changed if now.get(p) != base.get(p))
        broke: list[str] = []
        if changed and not clash:
            try:
                files = {p: data for p, data in now.items() if p not in changed}
                files.update({p: final[p] for p in changed if p in final})
                self.main.files.restore({self.code.base / p: data for p, data in files.items()})
                after = self.evaluate()
                broke = self.broken(state, after)
            except BaseException:
                self.main.files.restore(before)
                raise
            if not broke:
                self._patched(step, before, task["id"])
                self._done(step, after)
                return after, changed
            self.main.files.restore(before)
        elif not changed:
            self._done(step, state)
            return state, changed
        others = sorted({t for t, files in since if not clash or {p.as_posix() for p in files} & set(clash)})
        step.update(outcome="conflict", conflict={"files": clash or sorted(p.as_posix() for p in changed),
                                                  "with": others}, **({"broke": broke} if broke else {}))
        self._done(step, state)
        return state, None

    def _unmerged(self, tasks: list[tuple[str, int]]) -> None:
        """The tasks whose copies were not brought to the working folder (Ctrl+C): their steps
        since they started that were kept, or had no outcome yet, are `interrupted`, for
        --resume to do them again."""
        with self._lock:
            cut = [s for task, since in tasks for s in self.report["steps"][since:]
                   if s["task"] == task and s.get("outcome") in (None, "kept")]
            for s in cut:
                s["outcome"] = "interrupted"
        self.save()
        for s in cut:
            self.on_step(s)

    def finished(self, task: str) -> bool:
        """Whether the steps of the report say that a task was finished: kept, or undone after
        its retry (it had both its chances). One with an error, interrupted or undone before its
        retry is not."""
        ends = [s for s in self.report["steps"] if s["task"] == task and s["kind"].startswith("task")]
        return any(s.get("outcome") == "kept" and not s.get("error") for s in ends) or \
            any(s.get("outcome") == "undone" and s["kind"].startswith("task-retry") for s in ends)

    def tag_fix(self, task: dict, missing: list[str], before: dict[Path, bytes], state: dict,
                ws: _Workspace | None = None) -> dict:
        """The turn that tags the requirements of a kept task that no changed subprogram cites
        (require_tags), as one more attempt (its budget, undone if it breaks something), in the
        working folder or in `ws`: the state after it, or `state` if it broke something."""
        fix, after, broken = self.attempt(self.plan.agent, TAGS_PROMPT.format(missing=", ".join(missing),
                                                                             tag=self._tag(), first=missing[0]),
                                          "tag-fix", task["id"], state, ws=ws)
        if broken:
            fix["outcome"] = "undone"
            self._done(fix, state)
            return state
        fix["untagged"] = self.untagged(task["requirements"], before, ws)
        fix["outcome"] = "untagged" if fix["untagged"] else "tagged"
        self._done(fix, after)
        return after

    def planned(self, task: dict, state: dict, ws: _Workspace | None = None) -> str:
        """The text of a task for the agent, with its requirements in front: with the plan of
        the planner, if the plan has one and it answered."""
        text = self.task_text(task)
        if not self.plan.planner:
            return text
        ws = ws or self.main
        ws.budget = _Budget(self.plan)
        step = self.run(self.plan.planner, text, "plan", task["id"], ws)
        step["outcome"] = "planned" if step.get("answer") else "no plan"
        self._done(step, state)
        plan = self._answers.get(step["n"], "")
        return PLANNED.format(task=text, plan=plan) if plan.strip() else text

    def rounds(self, state: dict) -> dict:
        """The rounds (see the module), after those of an earlier run (--resume): the steps
        that have their `round` number say how far they got."""
        stuck, attempts, escalated = {}, {}, self.report.setdefault("for_a_person", {})
        target, kept_turn = None, False

        def count(target: str, step: dict) -> bool:
            """Whether the test keeps the turn; a test that does not get further is stuck once more."""
            outcome = step["outcome"]
            if outcome in ("fixed", "further"):
                stuck[target] = 0
            elif outcome != "error":                    # the model server failed: not a try of the agent
                stuck[target] = stuck.get(target, 0) + 1
                attempts.setdefault(target, []).append(
                    f"{step.get('answer') or step.get('error') or '(no answer)'} -> {outcome}"
                    + (f" ({'; '.join(step['broke'])[:300]})" if step.get("broke") else ""))
            return outcome == "further"

        earlier = [s for s in self.report["steps"] if "round" in s]
        for s in earlier:
            target = s["task"]
            kept_turn = count(target, s)
        for number in range(len(earlier) + 1, self.plan.fix_rounds + 1):
            failing = [t for t in self.tests if not state["tests"][t]["passed"] and t not in escalated]
            if not failing:
                break
            if not (kept_turn and target in failing):           # the next failing test after the last one
                after_last = [t for t in failing if target and self.tests.index(t) > self.tests.index(target)]
                target = (after_last or failing)[0]
            result = state["tests"][target]
            tried = attempts.get(target, [])
            prompt = self.plan.fix_prompt.format(
                test=target, output=_output(target, result), code=_cited(result),
                attempts=("\nEarlier attempts on this test, which did not fix it:\n" +
                          "\n".join(f"- {a}" for a in tried[-3:])) if tried else "")
            before = self.main.files.snapshot()
            step, after, broken = self.attempts(self.plan.fix_agent, prompt, "round", target, state)
            before_score, after_score = result["score"], after["tests"][target]["score"]
            if broken:
                outcome = "undone"
            elif after["tests"][target]["passed"]:
                outcome = "fixed"
            elif after_score > before_score:
                outcome = "further"
            elif step.get("error"):
                outcome = "error"
            else:
                outcome = "no progress"
            step.update(outcome=outcome, round=number)
            kept_turn = count(target, step)
            if outcome not in ("fixed", "further", "error") and stuck[target] >= self.plan.stuck_rounds:
                escalated[target] = {"reason": f"no progress in {stuck[target]} rounds",
                                     "output": after["tests"][target]["output"][-MAX_OUTPUT:],
                                     "attempts": attempts[target]}
            state = after
            self._patched(step, before, f"round-{number}")
            self._done(step, state)
        return state

    def run_plan(self, resume: bool = False) -> dict:
        """The tasks, then the rounds; with `resume`, from the report of an earlier run (see
        the module). Returns the report."""
        self._started = time.time()
        done = self._resume() if resume else set()
        if not resume:
            Path(f"{self.path}.calls.jsonl").unlink(missing_ok=True)
            shutil.rmtree(self.path, ignore_errors=True)               # the patches of an earlier run
        warnings = self.warnings + [w for w in [self.load()] if w]
        if warnings:
            self.report["warnings"] = warnings
        if self.plan.propose:                                        # the working folder is not touched
            copy = self.cfg.workspace / "attempts" / "0"             # with --resume, the copy of the earlier run
            self.main = _Workspace(self, copy) if resume and copy.is_dir() else self._copy("0")
            self.code, self.build, self.files = self.main.code, self.main.build, self.main.files
            self.report["proposal"] = {"copy": str(self.main.base), "patches": str(self.path)}
        state = self.evaluate()
        self.report.setdefault("at_start", {"tests_passed": self.passing(state),
                                            "check_errors": sorted(state["check"])})
        state = self.tasks(state, done)
        self.report["after_tasks"] = {"tests_passed": self.passing(state)}
        state = self.rounds(state)
        self.report["final"] = {
            "tests_passed": self.passing(state), "tests": len(self.tests),
            "failing": {t: _last_lines(r["output"]) for t, r in state["tests"].items() if not r["passed"]},
            "check_errors": sorted(state["check"]),
            "errors": sum(1 for s in self.report["steps"] if s.get("error"))}
        self.report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.hints_to_write()
        self.save()
        return self.report

    def _resume(self) -> set[str]:
        """The report of an earlier run, to go on from it: its steps, the errors of the compiler
        and the tests for a person stay. The ids of the tasks it finished."""
        path = self.path.with_suffix(".json")
        if not path.exists():
            return set()
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"--resume: cannot read {path}: {e}") from None
        self.errors = {_shape(e["message"]): e for e in old.pop("compiler_errors", [])}
        self._wall_before = (old.get("totals") or {}).get("wall_seconds", 0)
        for key in ("final", "finished", "totals", "schedule", "warnings"):
            old.pop(key, None)
        old["resumed"] = old.get("resumed", []) + [time.strftime("%Y-%m-%d %H:%M:%S")]
        self.report = {**self.report, **old, "tests": self.tests}
        return {t["id"] for t in self.plan.tasks if self.finished(t["id"])}

    def _patched(self, step: dict, before: dict, name: str) -> None:
        """What changed in the working folder since `before`, as a patch: <name>.patch in
        <reports>/develop-<plan>/, and at the end of all.patch there (see patch: without binary
        files); `patch` in the step is its path from the reports folder. Nothing when nothing
        changed."""
        text = patch(before, self.main.files.snapshot(), self.code._show)
        if not text:
            return
        used = {s.get("patch") for s in self.report["steps"]}
        base = re.sub(r"[^\w.-]", "_", name)
        file, k = f"{base}.patch", 1
        while f"{self.path.name}/{file}" in used:
            k += 1
            file = f"{base}-{k}.patch"
        self.path.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8", "surrogateescape")
        (self.path / file).write_bytes(data)
        with open(self.path / "all.patch", "ab") as all_patches:
            all_patches.write(data)
        step["patch"] = f"{self.path.name}/{file}"

    def hints_to_write(self) -> None:
        """The drafts of hints of the errors without one (atlas/agents/hints.py), in
        reports/hints-<plan>.toml and, short, in the report: never used as they are."""
        calls = Path(f"{self.path}.calls.jsonl")
        if not calls.is_file():
            return
        path, drafts = write_hints_draft(calls)
        if drafts:
            self.report["hints_to_write"] = {
                "file": path.name, "errors": [{"count": d["count"], "message": d["message"], "match": d["match"],
                                               **({"fixed_in_run": d["fix"]["run"]} if d["fix"] else {})}
                                              for d in drafts]}

    # --- the report ------------------------------------------------------------------------
    def save(self) -> None:
        with self._lock:
            steps = self.report["steps"]
            total = lambda key, field: sum((s.get(key) or {}).get(field, 0) for s in steps)
            self.report["totals"] = {
                "seconds": sum(s["seconds"] for s in steps), "calls": sum(s["calls"] for s in steps),
                "wall_seconds": self._wall_before + round(time.time() - self._started),
                "prompt_tokens": total("tokens", "prompt"), "completion_tokens": total("tokens", "completion"),
                "edits": {"made": total("edits", "made"), "failed": total("edits", "failed")},
                "compiles": {"ok": total("compiles", "ok"), "failed": total("compiles", "failed")},
                **({"cost": round(sum(s.get("cost", 0) for s in steps), 4)} if any("cost" in s for s in steps) else {})}
            self.report["compiler_errors"] = sorted(self.errors.values(), key=lambda e: -e["count"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.with_suffix(".json").write_text(json.dumps(self.report, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
            self.path.with_suffix(".md").write_text(markdown(self.report), encoding="utf-8")
            answered = [s for s in steps if self._answers.get(s["n"])]
            if answered:
                self.path.mkdir(parents=True, exist_ok=True)
                (self.path / "answers.md").write_text(answers(self.report, self._answers), encoding="utf-8")


def patch(before: dict[Path, bytes], after: dict[Path, bytes], show: Callable[[Path], str]) -> str:
    """The unified diff from `before` to `after` (files and their bytes), with 3 lines of
    context and the paths of `show` after a/ and b/ (/dev/null for a new or a removed file), for
    `git apply` or `patch -p1`. A binary file is left out; the bytes that are not UTF-8 stay
    (surrogateescape: encode the text back with it)."""
    lines = lambda data: [l for l in re.split(r"(?<=\n)", (data or b"").decode("utf-8", "surrogateescape")) if l]
    out = []
    for p in sorted(set(before) | set(after)):
        old, new = before.get(p), after.get(p)
        if old == new or b"\0" in (old or b"") or b"\0" in (new or b""):
            continue
        name = show(p)
        for line in difflib.unified_diff(lines(old), lines(new), n=3,
                                         fromfile=f"a/{name}" if old is not None else "/dev/null",
                                         tofile=f"b/{name}" if new is not None else "/dev/null"):
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def answers(report: dict, texts: dict[int, str]) -> str:
    """The whole answer of each run of an agent, by step, for the links of the report."""
    out = [f"# atlas develop: {report['plan']}, the answers of the agents", ""]
    for s in report["steps"]:
        if texts.get(s["n"]):
            kind = s["kind"] + (f" #{s['attempt']}" if s.get("attempt") else "")
            out += [f'<a id="step-{s["n"]}"></a>', "", f"## {s['n']}. {kind} {s['task']} ({s['agent']})", "",
                    texts[s["n"]].strip(), ""]
    return "\n".join(out)


def tool_use(record: list[dict]) -> tuple[dict, list[dict]]:
    """What the tool calls of a run did ({"edits": {made, failed}, "compiles": {ok, failed}},
    and "warnings" of guard.py if any) and the errors of the compiler it saw: of check_code, of
    the check after a change and of the syntax check."""
    edits, compiles, errors, warnings = Counter(), Counter(), [], []
    for call in record:
        res = call.get("result") if isinstance(call.get("result"), dict) else {}
        warnings += [f"{call.get('name')}: {w}" for w in res.get("warnings", [])
                     if isinstance(w, str) and f"{call.get('name')}: {w}" not in warnings]
        checks = []
        if call.get("name") in WRITES:
            edits["failed" if "error" in res else "made"] += 1
            errors += res.get("syntax_errors", [])
            checks = [res["check"]] if isinstance(res.get("check"), dict) else []
        elif call.get("name") == "check_code" and "ok" in res:
            checks = [res]
        for c in checks:
            compiles["ok" if c.get("ok") else "failed"] += 1
            errors += c.get("errors", [])
    return {"edits": {"made": edits["made"], "failed": edits["failed"]},
            "compiles": {"ok": compiles["ok"], "failed": compiles["failed"]},
            **({"warnings": warnings} if warnings else {})}, errors


def _changed_lines(old: bytes, new: bytes) -> set[int]:
    """The lines of `new` (from 1) that are not in `old`, and those next to where lines of
    `old` were removed."""
    a = old.decode("utf-8", "replace").splitlines()
    b = new.decode("utf-8", "replace").splitlines()
    out: set[int] = set()
    for op, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.update(range(j1 + 1, max(j2, j1 + 1) + 1))
    return out


def _is_test(file: str, patterns: list[str]) -> bool:
    """Whether a file matches one of [sources.code] test_patterns ("**/tests/**"), as the graph says."""
    return any(fnmatch.fnmatch(file, p) or fnmatch.fnmatch("/" + file, p) for p in patterns)


def _verdict(answer: str) -> list[dict] | None:
    """The issues of the answer of a reviewer ([] when it says ok), or None when the answer has
    no verdict in JSON. An issue is {file, line, issue}; a plain text is taken as the issue."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(answer[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("ok", "issues"):
        return None
    issues = data.get("issues") or []
    if data["verdict"] == "ok" or not isinstance(issues, list):
        return []
    return [i if isinstance(i, dict) else {"issue": str(i)} for i in issues if i]


def _output(test: str, result: dict) -> str:
    """What a test printed, its end, for a prompt."""
    return data_block(result["output"][-MAX_OUTPUT:], f"the output of {test}")


def _execute(command: list[str], cwd: Path, stdin: str | None, timeout: float) -> tuple[str, str, int | None]:
    """Runs an agent of outside: (what it printed, its errors, its exit code). The exit code
    is None when it ran out of time, and then its whole process group is killed."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else
             {"start_new_session": True})
    proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", **group)
    try:
        output, errors = proc.communicate(stdin, timeout=timeout or None)
        return output, errors, proc.returncode
    except subprocess.TimeoutExpired:
        _kill(proc)
        output, errors = proc.communicate()
        return output or "", errors or "", None
    except BaseException:                    # Ctrl+C: the agent does not go on alone
        _kill(proc)
        raise


def _kill(proc: subprocess.Popen) -> None:
    """Kills a process and those it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, 9)
    except OSError:
        proc.kill()



def _cited(result: dict) -> str:
    """The code that the output of a test cites, for a prompt."""
    code = "\n".join(f"{c['file']}, line {c['line']}:\n{c['source']}" for c in result["code_at"])
    return f"\nThe code it cites:\n{data_block(code, 'code_at')}\n" if code else ""


def _message(error: dict) -> str:
    return str(error.get("message", error))


FAILURE = re.compile(r"\b(?:Error|ERROR|raised|failed|FAILED)\b|^\S*Error:")


def _last_lines(output: str, n: int = 2) -> str:
    """What a failing test says, in a line: its lines that tell the failure (an assertion, an
    exception), or else its last ones; a traceback of the server goes after them."""
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    failure = [l for l in lines if FAILURE.search(l)]
    return " | ".join(failure[:n] if failure else lines[-n:])[:300]


def markdown(report: dict) -> str:
    t = report["totals"]
    out = [f"# atlas develop: {report['plan']}", "",
           f"Started {report['started']}" + (f", finished {report['finished']}" if report.get("finished") else
                                              ", running") + ".", ""]
    folder = f"develop-{report['plan']}"
    if report.get("final"):
        f = report["final"]
        out += [f"**{f['tests_passed']} of {f['tests']} tests pass.**"
                + (f" {f['errors']} agent runs failed with an error." if f.get("errors") else ""), ""]
    if report.get("proposal"):
        out += [f"A proposal: the working folder was not changed. The patches are in [{folder}]({folder}/), "
                f"[all.patch]({folder}/all.patch) with every change (`git apply` or `patch -p1` in the working "
                f"folder); the code as the agents left it, in {report['proposal']['copy']}, until the next "
                f"atlas develop.", ""]
    edits, compiles = t.get("edits", {}), t.get("compiles", {})
    out += [f"{len(report['steps'])} agent runs, {t['seconds'] // 60} min of runs ({t.get('wall_seconds', 0) // 60} "
            f"min of wall time, with the tests and the attempts at once), {t['calls']} tool calls, "
            f"{t['prompt_tokens'] + t['completion_tokens']} tokens" + (f", ${t['cost']}" if "cost" in t else "") + ".",
            f"Edits: {edits.get('made', 0)} made, {edits.get('failed', 0)} failed. Compilations: "
            f"{compiles.get('ok', 0)} without errors, {compiles.get('failed', 0)} with errors."]
    patches = [s["patch"] for s in report["steps"] if s.get("patch")]
    if patches and not report.get("proposal"):
        all_patches = patches[0].split("/")[0] + "/all.patch"
        out += [f"What the tasks and rounds left in the code: a patch in each step, and all of them in "
                f"[{all_patches}]({all_patches}) (`git apply`, or `patch -p1`)."]
    schedule = report.get("schedule")
    if schedule:
        out += [f"Tasks at most {schedule['parallel']} at once: {schedule['tasks_seconds'] / 60:.1f} min of tasks one "
                f"after another in {schedule['wall_seconds'] / 60:.1f} min of wall time, "
                f"{schedule['speedup']:g}x as fast"
                + (f"; {schedule['conflicts']} undone for a conflict and done again alone" if schedule["conflicts"]
                   else "") + "."]
    if report.get("resumed"):
        out += [f"Resumed {', '.join(report['resumed'])}."]
    out += [f"Warning: {w}" for w in report.get("warnings", [])]
    out += ["", "| # | kind | task or test | agent | min | calls | edits (failed) | compilations (errors) | outcome | "
                "tests passing | patch, answer |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["steps"]:
        outcome = s.get("outcome", "")
        if s.get("error"):
            outcome = f"{outcome} (error: {s['error'][:80]})"
        if s.get("budget_exceeded"):
            outcome = f"{outcome} (budget spent: {s['budget_exceeded']})"
        if s.get("conflict"):
            c = s["conflict"]
            met = f", with {', '.join(c['with'])}" if c["with"] else ""
            outcome = f"{outcome} ({', '.join(c['files'])}{met}; done again alone)"
        e, c = s.get("edits") or {}, s.get("compiles") or {}
        stop = (" (stalled)" if s.get("stalled") else " (timed out)" if s.get("timed_out") else
                " (max_steps)" if s.get("stopped") else "")
        kind = s["kind"] + (f" #{s['attempt']}" if s.get("attempt") else "")
        links = ([f"[patch]({s['patch']})"] if s.get("patch") else []) + \
                ([f"[answer]({folder}/answers.md#step-{s['n']})"] if s.get("answer") else [])
        out.append(f"| {s['n']} | {kind} | {s['task']} | {s['agent']} | {s['seconds'] / 60:.1f} | {s['calls']}"
                   f"{stop} | {e.get('made', 0) + e.get('failed', 0)} "
                   f"({e.get('failed', 0)}) | {c.get('ok', 0) + c.get('failed', 0)} ({c.get('failed', 0)}) | "
                   f"{outcome} | {s.get('tests_passed', '')} | {' '.join(links)} |")
    undone = [s for s in report["steps"] if s.get("broke")]
    if undone:
        out += ["", "## Undone", ""]
        out += [f"- {s['n']} {s['task']}: " + "; ".join(s["broke"])[:400] for s in undone]
    untagged = [s for s in report["steps"] if s.get("untagged")]
    if untagged:
        out += ["", "## Untagged", "", "Kept, but no subprogram they changed cites these requirements of the task:", ""]
        out += [f"- {s['n']} {s['task']}: {', '.join(s['untagged'])}" for s in untagged]
    touched = [s for s in report["steps"] if s.get("protected")]
    if touched:
        out += ["", "## Protected files put back", "", "The agent changed these protected files; they were put "
                "back as they were:", ""]
        out += [f"- {s['n']} {s['task']}: {', '.join(s['protected'])}" for s in touched]
    behind = [s for s in report["steps"] if s.get("not_advanced")]
    if behind:
        out += ["", "## Did not advance", "", "Kept, but these tests did not get further with them:", ""]
        out += [f"- {s['n']} {s['task']}: {', '.join(s['not_advanced'])}" for s in behind]
    reviews = [s for s in report["steps"] if s.get("issues")]
    if reviews:
        out += ["", "## Review", "", "What the reviewer found in tasks that compiled and broke nothing:", ""]
        out += [f"- {s['n']} {s['task']}: " + "; ".join(
            f"{i.get('file', '?')}" + (f":{i['line']}" if i.get("line") else "") + f" {i.get('issue', '')}"
            for i in s["issues"])[:400] for s in reviews]
    warned = [s for s in report["steps"] if s.get("warnings")]
    if warned:
        out += ["", "## Instructions inside the data", "", "What these tools returned has phrases written to "
                "steer a model; check that the agent did not follow them:", ""]
        out += [f"- {s['n']} {s['task']}: {w}" for s in warned for w in s["warnings"]]
    person = report.get("for_a_person") or {}
    if person:
        out += ["", "## For a person", "", "The agent made no progress on these tests:", ""]
        for test, p in person.items():
            out += [f"### {test}", "", p["reason"] + ".", "", "```text", p["output"].strip(), "```", "",
                    "Attempts:", ""] + [f"- {a}" for a in p["attempts"]] + [""]
    if report.get("final", {}).get("failing"):
        out += ["", "## Failing at the end", ""] + [f"- {k}: {v}" for k, v in report["final"]["failing"].items()]
    errors = report.get("compiler_errors") or []
    if errors:
        out += ["", "## Compiler errors", "", "The errors the agents met, the most frequent first. One without a hint "
                "can get one in a file of [sources.code] hints.", "", "| times | message | hint |", "|---|---|---|"]
        out += [f"| {e['count']} | {e['message'].replace('|', '/')[:120]} | {'yes' if e['hint'] else '**no**'} |"
                for e in errors[:SHOWN_ERRORS]]
    new = [s for s in report["steps"] if s.get("created") or s.get("deleted")]
    if new:
        out += ["", "## New files", "", "The files that kept tasks created, and those deleted as tests of the agent "
                "among the code (stray = \"delete\"):", ""]
        out += [f"- {s['n']} {s['task']}: " + "; ".join(
            part for part in (", ".join(s.get("created", [])), "deleted " + ", ".join(s["deleted"])
                              if s.get("deleted") else "") if part) for s in new]
    drafts = report.get("hints_to_write") or {}
    if drafts.get("errors"):
        out += ["", "## Hints to write", "", f"The errors that came without a hint, as drafts in {drafts['file']}: "
                "write the hints worth having there and copy them to a file of [sources.code] hints.", "",
                "| times | message | fixed in run |", "|---|---|---|"]
        out += [f"| {e['count']} | {e['message'].replace('|', '/')[:120]} | {e.get('fixed_in_run', '')} |"
                for e in drafts["errors"][:SHOWN_ERRORS]]
    return "\n".join(out) + "\n"


def develop(plan_path: str | Path, cfg: Config, client: Any = None,
            on_step: Callable[[dict], None] | None = None, resume: bool = False) -> dict:
    """Runs a plan (see the module): its tasks, then the rounds that fix failing tests; with
    `resume`, from where an earlier run of it stopped. Returns the report, also written to
    <workspace>/reports/develop-<plan>.json and .md."""
    return Developer(load_plan(plan_path), cfg, client, on_step).run_plan(resume)
