"""atlas develop: an agent writes the code task by task, and the tests say what it got right.

    atlas develop tasks/server.toml atlas.toml

The plan is a TOML file:

    agent = "implementer"                    # the agent of the tasks
    fix_agent = "fixer"                      # the agent that fixes a failing test (default: agent)
    check = ["src/*.adb"]                    # check_code on these files after each task
    tests = ["tests/python/test_*.py"]       # the acceptance tests, run with run_test, in this order
    progress = 'Step\\s+(\\d+)'                # optional: how far a failing test got (the last number)
    build_fixes = 2                          # turns to fix what check reports, after a task
    fix_rounds = 12                          # rounds to fix failing tests, after the tasks
    stuck_rounds = 2                         # rounds without progress before a test goes to a person
    fix_prompt = "..."                       # optional: {test}, {output}, {code}, {attempts}

    [[tasks]]
    id = "controller"                        # optional: T01, T02...
    text = "Create the package Controller of SRS-0191 ..."

What it does comes from what went wrong when gemma-4-26b and devstral wrote a server with no
one to help them: code that compiled and did not work, tests changed to pass, failures tried
again and again in the same way, and changes that broke what worked.

1. Each task goes to the agent; then check_code runs on the files of `check`, and the tests run.
   When check finds errors, they go back to the agent, at most `build_fixes` times.
2. A task that leaves errors, or makes a test fail sooner than before (a test that passed
   fails; with `progress`, a test stops at an earlier step), is undone: the files go back to
   how they were. It is tried once more, told what it broke; if it breaks it again, it stays
   undone. The code never gets worse.
3. Then the rounds: the fix agent gets one failing test, what it printed and the code it cites,
   and the earlier attempts on it. A round that breaks something is undone. A test that does
   not get further in `stuck_rounds` rounds is left for a person: the agent does not insist on
   what it cannot fix. While a test gets further it keeps the turn; when not, the next one.
4. A report after each step, in <workspace>/reports/develop-<plan>.json and .md: what each
   task and round did, its time, tokens and cost, the tests that pass and those that need a
   person, with their output.

Undo puts back the files of the code roots that an agent can change (neither excluded nor
protected); nothing is committed. The tests should be protected ([sources.code] protected).
"""
from __future__ import annotations

import glob
import json
import math
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from atlas.config import Config, ConfigError

__all__ = ["Plan", "develop", "load_plan"]

MAX_OUTPUT = 1500          # characters of a test output in a prompt
MAX_ANSWER = 400           # characters of an answer of the agent kept for the report and the attempts

FIX_PROMPT = """The test {test} fails. What it printed:

{output}
{code}
Find the cause in the code and fix it. The test is right and cannot be changed: change the code it tests. \
Check every file you change with check_code, and run the test with run_test before you finish.
{attempts}"""
BUILD_FIX_PROMPT = """The code does not compile after the last change. check_code says:

{errors}

Fix these errors without changing what the code does, and check the files again with check_code."""
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
    fix_rounds: int = 12
    stuck_rounds: int = 2
    fix_prompt: str = FIX_PROMPT


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
    tasks = [{"id": str(t.get("id") or f"T{i:02d}"), "text": t["text"]} for i, t in enumerate(tasks, 1)]
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


def _files(patterns: list[str]) -> list[str]:
    return sorted({f for pattern in patterns for f in glob.glob(pattern, recursive=True) if Path(f).is_file()})


class Developer:
    def __init__(self, plan: Plan, cfg: Config, client: Any = None,
                 on_step: Callable[[dict], None] | None = None):
        from atlas.code import BuildTools, CodeTools
        self.plan, self.cfg, self.client = plan, cfg, client
        self.on_step = on_step or (lambda step: None)
        self.code = CodeTools.from_config(cfg)
        self.build = BuildTools.from_config(cfg, self.code)
        self.files = _Files(self.code)
        self.tests = _files(plan.tests)
        if not self.tests:
            raise ConfigError(f"plan {plan.name}: no file matches its tests {plan.tests}")
        if plan.check and not (self.build.check_command or self.build.check_commands):
            raise ConfigError(f"plan {plan.name} checks {plan.check}, but [sources.code] has no check_command")
        unknown = [t for t in self.tests if Path(t).suffix.lstrip(".").lower() not in self.build.test_commands
                   and not self.build.test_command]
        if unknown:
            raise ConfigError(f"plan {plan.name}: no command runs {unknown[0]} ([sources.code] test_command or "
                              f"test_commands)")
        self.report: dict = {"plan": plan.name, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": [],
                             "tests": self.tests}
        self.path = cfg.workspace / "reports" / f"develop-{plan.name}"

    # --- what the code does --------------------------------------------------------------
    def _score(self, result: dict) -> float:
        if result.get("passed"):
            return math.inf
        found = re.findall(self.plan.progress, result.get("output", "")) if self.plan.progress else []
        return float(found[-1]) if found else 0.0

    def evaluate(self) -> dict:
        """{"check": {file: [errors]} of the files with errors, "tests": {test: {passed, score, output, code_at}}}"""
        check = {}
        for f in _files(self.plan.check):
            res = self.build.check_code(f)
            if not res.get("ok"):
                check[f] = res.get("errors") or [{"message": res.get("error") or res.get("output", "")[-300:]}]
        tests = {}
        for t in self.tests:
            res = self.build.run_test(t)
            output = res.get("output") or res.get("error") or json.dumps(res.get("errors", []))
            tests[t] = {"passed": bool(res.get("passed")), "score": self._score(res), "output": output,
                        "code_at": res.get("code_at", [])}
        return {"check": check, "tests": tests}

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
    def run(self, agent: str, prompt: str, kind: str, task: str) -> dict:
        from atlas.agents.runtime import context_for, run_agent
        t0 = time.time()
        step = {"n": len(self.report["steps"]) + 1, "kind": kind, "task": task, "agent": agent}
        try:
            res = run_agent(agent, prompt, self.cfg, context_for(self.cfg), client=self.client)
            step.update({"calls": len(res["calls"]), "stopped": res["stopped"], "tokens": res.get("tokens"),
                         **({"cost": res["cost"]} if "cost" in res else {}), "answer": res["answer"][:MAX_ANSWER]})
        except Exception as e:                   # the model server failed: the task did not happen
            step.update({"calls": 0, "stopped": True, "error": f"{type(e).__name__}: {e}"[:MAX_ANSWER], "answer": ""})
        step["seconds"] = round(time.time() - t0)
        self.report["steps"].append(step)
        return step

    def attempt(self, agent: str, prompt: str, kind: str, task: str, before: dict) -> tuple[dict, dict, list]:
        """One change of the agent with its build fixes: (step, state after, what it broke).
        What breaks something is undone, and the state is `before` again."""
        snapshot = self.files.snapshot()
        step = self.run(agent, prompt, kind, task)
        after = self.evaluate()
        new_errors = lambda state: set(state["check"]) - set(before["check"])
        for _ in range(self.plan.build_fixes):
            if not new_errors(after):
                break
            errors = "\n".join(f"{f}:{e.get('line', '')}: {_message(e)}" for f, es in after["check"].items()
                               for e in es[:10])
            fix = self.run(agent, BUILD_FIX_PROMPT.format(errors=errors), "build-fix", task)
            after = self.evaluate()
            fix["outcome"] = "errors remain" if new_errors(after) else "compiles"
            self._done(fix, after)
        broken = self.broken(before, after)
        if broken:
            step["undone"] = self.files.restore(snapshot)
            step["broke"] = broken
            after = before
        return step, after, broken

    def _done(self, step: dict, state: dict) -> None:
        step["tests_passed"] = self.passing(state)
        self.save()
        self.on_step(step)

    # --- the plan --------------------------------------------------------------------------
    def tasks(self, state: dict) -> dict:
        for task in self.plan.tasks:
            step, state, broken = self.attempt(self.plan.agent, task["text"], "task", task["id"], state)
            step["outcome"] = "undone" if broken else "kept"
            self._done(step, state)
            if broken:
                retry, state, broken = self.attempt(self.plan.agent, task["text"] + RETRY.format(
                    broken="\n".join(f"- {b}" for b in broken)), "task-retry", task["id"], state)
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
            code = "\n".join(f"{c['file']}, line {c['line']}:\n{c['source']}" for c in result["code_at"])
            tried = attempts.get(target, [])
            prompt = self.plan.fix_prompt.format(
                test=target, output=result["output"][-MAX_OUTPUT:].strip(),
                code=f"\nThe code it cites:\n{code}\n" if code else "",
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
        steps = self.report["steps"]
        self.report["totals"] = {
            "seconds": sum(s["seconds"] for s in steps), "calls": sum(s["calls"] for s in steps),
            "prompt_tokens": sum((s.get("tokens") or {}).get("prompt", 0) for s in steps),
            "completion_tokens": sum((s.get("tokens") or {}).get("completion", 0) for s in steps),
            **({"cost": round(sum(s.get("cost", 0) for s in steps), 4)} if any("cost" in s for s in steps) else {})}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.with_suffix(".json").write_text(json.dumps(self.report, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
        self.path.with_suffix(".md").write_text(markdown(self.report), encoding="utf-8")


def _message(error: dict) -> str:
    return str(error.get("message", error))


def _last_lines(output: str, n: int = 2) -> str:
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:300]


def markdown(report: dict) -> str:
    t = report["totals"]
    out = [f"# atlas develop: {report['plan']}", "",
           f"Started {report['started']}" + (f", finished {report['finished']}" if report.get("finished") else
                                              ", running") + ".", ""]
    if report.get("final"):
        f = report["final"]
        out += [f"**{f['tests_passed']} of {f['tests']} tests pass.**", ""]
    out += [f"{len(report['steps'])} agent runs, {t['seconds'] // 60} min, {t['calls']} tool calls, "
            f"{t['prompt_tokens'] + t['completion_tokens']} tokens" + (f", ${t['cost']}" if "cost" in t else "") + ".",
            "", "| # | kind | task or test | agent | min | calls | outcome | tests passing |", "|---|---|---|---|---|---|---|---|"]
    for s in report["steps"]:
        outcome = s.get("outcome", "")
        if s.get("error"):
            outcome = f"{outcome} (error: {s['error'][:80]})"
        out.append(f"| {s['n']} | {s['kind']} | {s['task']} | {s['agent']} | {s['seconds'] / 60:.1f} | {s['calls']}"
                   f"{' (max_steps)' if s.get('stopped') else ''} | {outcome} | {s.get('tests_passed', '')} |")
    undone = [s for s in report["steps"] if s.get("broke")]
    if undone:
        out += ["", "## Undone", ""]
        out += [f"- {s['n']} {s['task']}: " + "; ".join(s["broke"])[:400] for s in undone]
    person = report.get("for_a_person") or {}
    if person:
        out += ["", "## For a person", "", "The agent made no progress on these tests:", ""]
        for test, p in person.items():
            out += [f"### {test}", "", p["reason"] + ".", "", "```text", p["output"].strip(), "```", "",
                    "Attempts:", ""] + [f"- {a}" for a in p["attempts"]] + [""]
    if report.get("final", {}).get("failing"):
        out += ["", "## Failing at the end", ""] + [f"- {k}: {v}" for k, v in report["final"]["failing"].items()]
    return "\n".join(out) + "\n"


def develop(plan_path: str | Path, cfg: Config, client: Any = None,
            on_step: Callable[[dict], None] | None = None) -> dict:
    """Runs a plan (see the module): its tasks, then the rounds that fix failing tests.
    Returns the report, also written to <workspace>/reports/develop-<plan>.json and .md."""
    return Developer(load_plan(plan_path), cfg, client, on_step).run_plan()
