"""atlas develop: an agent writes the code task by task, and the tests say what it got right.

    atlas develop tasks/server.toml atlas.toml

The plan is a TOML file:

    agent = "implementer"                    # the agent of the tasks
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
    fix_prompt = "..."                       # optional: {test}, {output}, {code}, {attempts}
    advance_prompt = "..."                   # optional: {test}, {output}, {code}

    [[tasks]]
    id = "controller"                        # optional: T01, T02...
    text = "Create the package Controller of SRS-0191 ..."
    advances = ["tests/python/test_stp_0101.py"]   # optional: the tests it makes get further

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
   first, with whether a hint came with them ([sources.code] hints: one more after each run).
   develop-<plan>.calls.jsonl keeps the prompt, the tool calls with their results and the
   answer of every run of an agent.

With attempts > 1, each task (and its retry) goes to that many runs at once, each in its own
copy of the working folder (<workspace>/attempts/<n>: the code roots, the scripts of the
project, its own build folder), with its build fixes and the turns of `advances`. Of those that
break nothing, the one with more tests passing, and further, wins, and its files are copied to
the working folder. A small model gets one in N right more often than one right in a row
(Agentless, S*); the request limit leaves room (a turn of gemma-4-26b takes ~30 s: 2 requests
per minute of 28). The code roots must be inside the working folder.

A run that stops without finishing (max_steps, or stall_steps of the agent: that many tool
calls without changing the code) starts again with a fresh conversation, at most `restarts`
times: the same prompt, the diff of what it changed, which stays, and what check_code says
now. A long conversation of failed edits does not get better with more turns.

Undo puts back the files of the code roots that an agent can change (neither excluded nor
protected); nothing is committed. The tests should be protected ([sources.code] protected).
"""
from __future__ import annotations

import difflib
import fnmatch
import glob
import json
import math
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from atlas.config import Config, ConfigError

__all__ = ["Plan", "develop", "load_plan"]

MAX_OUTPUT = 1500          # characters of a test output in a prompt
MAX_CHANGES = 3000         # characters of the diff of what a stopped run changed, in the prompt of its restart
MAX_ANSWER = 400           # characters of an answer of the agent kept for the report and the attempts
WRITES = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")
SHOWN_ERRORS = 15          # errors of the compiler in the Markdown report

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
    check: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    progress: str = ""
    build_fixes: int = 2
    advance_fixes: int = 2
    fix_rounds: int = 12
    stuck_rounds: int = 2
    restarts: int = 1
    attempts: int = 1
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
    for t in tasks:
        unknown = sorted(set(t) - {"id", "text", "advances"})
        advances = t.get("advances", [])
        if unknown or not isinstance(advances, list) or not all(isinstance(a, str) for a in advances):
            raise ConfigError(f"plan {path}: a task has id, text and advances (a list of tests)"
                              + (f", not {unknown}" if unknown else ""))
    tasks = [{"id": str(t.get("id") or f"T{i:02d}"), "text": t["text"], "advances": t.get("advances", [])}
             for i, t in enumerate(tasks, 1)]
    if data.get("progress"):
        try:
            re.compile(data["progress"])
        except re.error as e:
            raise ConfigError(f"plan {path}: progress is not a valid regular expression: {e}") from None
    plan = Plan(name=path.stem, tasks=tasks, **data)
    plan.fix_agent = plan.fix_agent or plan.agent
    if not plan.tests:
        raise ConfigError(f"plan {path}: no tests: they are what says whether the code works")
    return plan


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


def _files(patterns: list[str], base: Path | None = None) -> list[str]:
    """The files of some patterns, from the working folder or from `base` (a copy of it), as
    paths relative to it."""
    if base is None:
        return sorted({f for pattern in patterns for f in glob.glob(pattern, recursive=True) if Path(f).is_file()})
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

    def context(self):
        """What the tools of an agent receive: their file and build tools on this folder."""
        from atlas.agents.runtime import context_for
        from atlas.code import BuildTools, CodeTools
        context = context_for(self.dev.cfg)
        if self.base is not None:
            context._code_tools = CodeTools.from_config(self.dev.cfg, base=self.base)
            context._build_tools = BuildTools.from_config(self.dev.cfg, context._code_tools, base=self.base)
            context._build_tools.hook()
        return context

    def evaluate(self) -> dict:
        """{"check": {file: [errors]} of the files with errors, "tests": {test: {passed, score, output, code_at}}}"""
        check = {}
        for f in _files(self.dev.plan.check, self.base):
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
        self.tests = _files(plan.tests)
        outside = [r for r in self.code.roots if Path.cwd().resolve() not in (r, *r.parents)]
        if plan.attempts > 1 and outside:
            raise ConfigError(f"plan {plan.name}: attempts needs the code roots inside the folder where atlas runs, "
                              f"to copy them (not {self.code._show(outside[0])})")
        if not self.tests:
            raise ConfigError(f"plan {plan.name}: no file matches its tests {plan.tests}")
        if plan.check and not (self.build.check_command or self.build.check_commands):
            raise ConfigError(f"plan {plan.name} checks {plan.check}, but [sources.code] has no check_command")
        unknown = [t for t in self.tests if Path(t).suffix.lstrip(".").lower() not in self.build.test_commands
                   and not self.build.test_command]
        if unknown:
            raise ConfigError(f"plan {plan.name}: no command runs {unknown[0]} ([sources.code] test_command or "
                              f"test_commands)")
        for task in plan.tasks:                  # each pattern of advances, the tests it names
            found = []
            for pattern in task["advances"]:
                named = [t for t in self.tests if fnmatch.fnmatch(t, pattern) or Path(t).name == pattern]
                if not named:
                    raise ConfigError(f"plan {plan.name}, task {task['id']}: advances {pattern!r} is none of its "
                                      f"tests")
                found += [t for t in named if t not in found]
            task["advances"] = found
        self.report: dict = {"plan": plan.name, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": [],
                             "tests": self.tests}
        self.path = cfg.workspace / "reports" / f"develop-{plan.name}"
        self.errors: dict[str, dict] = {}      # the errors of the compiler, by their message without names
        self._lock = threading.RLock()         # the report and the calls log, shared by parallel attempts

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
    def run(self, agent: str, prompt: str, kind: str, task: str, ws: _Workspace | None = None) -> dict:
        """A run of the agent, started again with a fresh conversation, at most `restarts`
        times, when it stops before finishing (see the module): the step of the last one."""
        ws = ws or self.main
        before = ws.files.snapshot() if self.plan.restarts else {}
        step = self._run_once(agent, prompt, kind, task, ws)
        for _ in range(self.plan.restarts):
            if not step.get("stopped") or step.get("error"):
                break
            changes, check = self._changes(before, ws)
            step["outcome"] = "restarted"
            self.save()
            self.on_step(step)
            why = " without changing the code in its last steps" if step.get("stalled") else ""
            step = self._run_once(agent, RESTART.format(prompt=prompt, calls=step["calls"], why=why,
                                                        changes=changes or "(nothing)", check=check),
                                  f"{kind}-restart", task, ws)
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

    def _run_once(self, agent: str, prompt: str, kind: str, task: str, ws: _Workspace) -> dict:
        from atlas.agents.runtime import run_agent
        t0 = time.time()
        step = {"n": len(self.report["steps"]) + 1, "kind": kind, "task": task, "agent": agent,
                **({"attempt": ws.number} if ws.number else {})}
        res: dict = {"calls": []}
        record: list = []
        try:
            res = run_agent(agent, prompt, self.cfg, ws.context(), client=self.client, record=record)
            step.update({"calls": len(res["calls"]), "stopped": res["stopped"], "tokens": res.get("tokens"),
                         **({"stalled": True} if res.get("stalled") else {}),
                         **({"cost": res["cost"]} if "cost" in res else {}), "answer": res["answer"][:MAX_ANSWER]})
        except Exception as e:                   # the model server failed: the task did not happen
            step.update({"calls": 0, "stopped": True, "error": f"{type(e).__name__}: {e}"[:MAX_ANSWER], "answer": ""})
        step["seconds"] = round(time.time() - t0)
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(f"{self.path}.calls.jsonl", "a", encoding="utf-8") as log:   # what it did, to study it
                log.write(json.dumps({"n": step["n"], "kind": kind, "task": task,
                                      **({"attempt": ws.number} if ws.number else {}), "prompt": prompt,
                                      "calls": record, "answer": res.get("answer", "")},
                                     ensure_ascii=False, default=str) + "\n")
        return step

    def compiled(self, agent: str, task: str, before: dict, after: dict, ws: _Workspace | None = None) -> dict:
        """`after`, or the state after the build fixes of what check finds that `before` had not."""
        new_errors = lambda state: set(state["check"]) - set(before["check"])
        for _ in range(self.plan.build_fixes):
            if not new_errors(after):
                break
            errors = "\n".join(f"{f}:{e.get('line', '')}: {_message(e)}" for f, es in after["check"].items()
                               for e in es[:10])
            fix = self.run(agent, BUILD_FIX_PROMPT.format(errors=errors), "build-fix", task, ws)
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
        for _ in range(self.plan.advance_fixes):
            stuck = self.not_advanced(advances, before, after)
            if not stuck:
                break
            result = after["tests"][stuck[0]]
            ws = ws or self.main
            snapshot = ws.files.snapshot()
            fix = self.run(agent, self.plan.advance_prompt.format(
                test=stuck[0], output=result["output"][-MAX_OUTPUT:].strip(), code=_cited(result)), "advance-fix",
                task, ws)
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

    def attempt(self, agent: str, prompt: str, kind: str, task: str, before: dict,
                advances: list[str] = (), ws: _Workspace | None = None) -> tuple[dict, dict, list]:
        """One change of the agent with its build fixes and the turns that make the tests of
        `advances` get further: (step, state after, what it broke). What breaks something is
        undone, and the state is `before` again."""
        ws = ws or self.main
        snapshot = ws.files.snapshot()
        step = self.run(agent, prompt, kind, task, ws)
        after = self.compiled(agent, task, before, self.evaluate(ws), ws)
        broken = self.broken(before, after)
        if not broken and advances:
            after = self.advanced(agent, task, list(advances), before, after, ws)
            stuck = self.not_advanced(list(advances), before, after)
            if stuck:
                step["not_advanced"] = stuck
        if broken:
            step["undone"] = ws.files.restore(snapshot)
            step["broke"] = broken
            after = before
        return step, after, broken

    def attempts(self, agent: str, prompt: str, kind: str, task: str, before: dict,
                 advances: list[str] = ()) -> tuple[dict, dict, list]:
        """attempt(), or with plan.attempts > 1 that many at once, each in a copy of the working
        folder; the best one that breaks nothing wins and its files come to the working folder
        (see the module). The others end "not chosen", or "undone" if they broke something."""
        if self.plan.attempts <= 1:
            return self.attempt(agent, prompt, kind, task, before, advances)
        copies = [self._copy(n) for n in range(1, self.plan.attempts + 1)]
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
            self.main.files.restore({self.code.base / p.relative_to(winner.code.base): data
                                     for p, data in winner.files.snapshot().items()})
            after = self.evaluate()
            step = results[best][0]
            step["chosen"] = f"{best + 1} of {len(copies)}"
            return step, after, self.broken(before, after)
        finally:
            for ws in copies:
                shutil.rmtree(ws.base, ignore_errors=True)

    def _copy(self, number: int) -> _Workspace:
        """A copy of the working folder for a parallel attempt, without .git, the workspace and
        __pycache__."""
        source, target = self.code.base, self.cfg.workspace / "attempts" / str(number)
        shutil.rmtree(target, ignore_errors=True)
        workspace = self.cfg.workspace.resolve()

        def skip(folder: str, names: list[str]) -> set[str]:
            out = {n for n in names if n in (".git", "__pycache__")}
            if Path(folder).resolve() == workspace.parent:
                out.add(workspace.name)
            return out

        shutil.copytree(source, target, symlinks=True, ignore=skip)
        return _Workspace(self, target, number)

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
    def tasks(self, state: dict) -> dict:
        for task in self.plan.tasks:
            step, state, broken = self.attempts(self.plan.agent, task["text"], "task", task["id"], state,
                                                task["advances"])
            step["outcome"] = "undone" if broken else "kept"
            self._done(step, state)
            if broken:
                retry, state, broken = self.attempts(self.plan.agent, task["text"] + RETRY.format(
                    broken="\n".join(f"- {b}" for b in broken)), "task-retry", task["id"], state, task["advances"])
                retry["outcome"] = "undone" if broken else "kept"
                self._done(retry, state)
        return state

    def rounds(self, state: dict) -> dict:
        stuck, attempts, escalated = {}, {}, self.report.setdefault("for_a_person", {})
        target, kept_turn = None, False
        for _ in range(self.plan.fix_rounds):
            failing = [t for t in self.tests if not state["tests"][t]["passed"] and t not in escalated]
            if not failing:
                break
            if not (kept_turn and target in failing):           # the next failing test after the last one
                after_last = [t for t in failing if target and self.tests.index(t) > self.tests.index(target)]
                target = (after_last or failing)[0]
            result = state["tests"][target]
            tried = attempts.get(target, [])
            prompt = self.plan.fix_prompt.format(
                test=target, output=result["output"][-MAX_OUTPUT:].strip(), code=_cited(result),
                attempts=("\nEarlier attempts on this test, which did not fix it:\n" +
                          "\n".join(f"- {a}" for a in tried[-3:])) if tried else "")
            step, after, broken = self.attempt(self.plan.fix_agent, prompt, "round", target, state)
            before_score, after_score = result["score"], after["tests"][target]["score"]
            if broken:
                outcome = "undone"
            elif after["tests"][target]["passed"]:
                outcome = "fixed"
            elif after_score > before_score:
                outcome = "further"
            else:
                outcome = "no progress"
            step["outcome"] = outcome
            kept_turn = outcome == "further"
            if outcome in ("fixed", "further"):
                stuck[target] = 0
            else:
                stuck[target] = stuck.get(target, 0) + 1
                attempts.setdefault(target, []).append(
                    f"{step.get('answer') or step.get('error') or '(no answer)'} -> {outcome}"
                    + (f" ({'; '.join(broken)[:300]})" if broken else ""))
                if stuck[target] >= self.plan.stuck_rounds:
                    escalated[target] = {"reason": f"no progress in {stuck[target]} rounds",
                                         "output": after["tests"][target]["output"][-MAX_OUTPUT:],
                                         "attempts": attempts[target]}
            state = after
            self._done(step, state)
        return state

    def run_plan(self) -> dict:
        Path(f"{self.path}.calls.jsonl").unlink(missing_ok=True)
        state = self.evaluate()
        self.report["at_start"] = {"tests_passed": self.passing(state), "check_errors": sorted(state["check"])}
        state = self.tasks(state)
        self.report["after_tasks"] = {"tests_passed": self.passing(state)}
        state = self.rounds(state)
        self.report["final"] = {
            "tests_passed": self.passing(state), "tests": len(self.tests),
            "failing": {t: _last_lines(r["output"]) for t, r in state["tests"].items() if not r["passed"]},
            "check_errors": sorted(state["check"])}
        self.report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        return self.report

    # --- the report ------------------------------------------------------------------------
    def save(self) -> None:
        with self._lock:
            steps = self.report["steps"]
            total = lambda key, field: sum((s.get(key) or {}).get(field, 0) for s in steps)
            self.report["totals"] = {
                "seconds": sum(s["seconds"] for s in steps), "calls": sum(s["calls"] for s in steps),
                "prompt_tokens": total("tokens", "prompt"), "completion_tokens": total("tokens", "completion"),
                "edits": {"made": total("edits", "made"), "failed": total("edits", "failed")},
                "compiles": {"ok": total("compiles", "ok"), "failed": total("compiles", "failed")},
                **({"cost": round(sum(s.get("cost", 0) for s in steps), 4)} if any("cost" in s for s in steps) else {})}
            self.report["compiler_errors"] = sorted(self.errors.values(), key=lambda e: -e["count"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.with_suffix(".json").write_text(json.dumps(self.report, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
            self.path.with_suffix(".md").write_text(markdown(self.report), encoding="utf-8")


def tool_use(record: list[dict]) -> tuple[dict, list[dict]]:
    """What the tool calls of a run did ({"edits": {made, failed}, "compiles": {ok, failed}})
    and the errors of the compiler it saw: of check_code, of the check after a change and of
    the syntax check."""
    edits, compiles, errors = Counter(), Counter(), []
    for call in record:
        res = call.get("result") if isinstance(call.get("result"), dict) else {}
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
            "compiles": {"ok": compiles["ok"], "failed": compiles["failed"]}}, errors


def _shape(message: str) -> str:
    """A message of the compiler without its names and numbers: the same error in other places."""
    return re.sub(r"\d+", "N", re.sub(r'"[^"]*"', '"X"', message)).strip()


def _cited(result: dict) -> str:
    """The code that the output of a test cites, for a prompt."""
    code = "\n".join(f"{c['file']}, line {c['line']}:\n{c['source']}" for c in result["code_at"])
    return f"\nThe code it cites:\n{code}\n" if code else ""


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
    if report.get("final"):
        f = report["final"]
        out += [f"**{f['tests_passed']} of {f['tests']} tests pass.**", ""]
    edits, compiles = t.get("edits", {}), t.get("compiles", {})
    out += [f"{len(report['steps'])} agent runs, {t['seconds'] // 60} min, {t['calls']} tool calls, "
            f"{t['prompt_tokens'] + t['completion_tokens']} tokens" + (f", ${t['cost']}" if "cost" in t else "") + ".",
            f"Edits: {edits.get('made', 0)} made, {edits.get('failed', 0)} failed. Compilations: "
            f"{compiles.get('ok', 0)} without errors, {compiles.get('failed', 0)} with errors.",
            "", "| # | kind | task or test | agent | min | calls | edits (failed) | compilations (errors) | outcome | "
                "tests passing |", "|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["steps"]:
        outcome = s.get("outcome", "")
        if s.get("error"):
            outcome = f"{outcome} (error: {s['error'][:80]})"
        e, c = s.get("edits") or {}, s.get("compiles") or {}
        stop = " (stalled)" if s.get("stalled") else " (max_steps)" if s.get("stopped") else ""
        kind = s["kind"] + (f" #{s['attempt']}" if s.get("attempt") else "")
        out.append(f"| {s['n']} | {kind} | {s['task']} | {s['agent']} | {s['seconds'] / 60:.1f} | {s['calls']}"
                   f"{stop} | {e.get('made', 0) + e.get('failed', 0)} "
                   f"({e.get('failed', 0)}) | {c.get('ok', 0) + c.get('failed', 0)} ({c.get('failed', 0)}) | "
                   f"{outcome} | {s.get('tests_passed', '')} |")
    undone = [s for s in report["steps"] if s.get("broke")]
    if undone:
        out += ["", "## Undone", ""]
        out += [f"- {s['n']} {s['task']}: " + "; ".join(s["broke"])[:400] for s in undone]
    behind = [s for s in report["steps"] if s.get("not_advanced")]
    if behind:
        out += ["", "## Did not advance", "", "Kept, but these tests did not get further with them:", ""]
        out += [f"- {s['n']} {s['task']}: {', '.join(s['not_advanced'])}" for s in behind]
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
    return "\n".join(out) + "\n"


def develop(plan_path: str | Path, cfg: Config, client: Any = None,
            on_step: Callable[[dict], None] | None = None) -> dict:
    """Runs a plan (see the module): its tasks, then the rounds that fix failing tests.
    Returns the report, also written to <workspace>/reports/develop-<plan>.json and .md."""
    return Developer(load_plan(plan_path), cfg, client, on_step).run_plan()
