"""The judge of atlas develop for an editor: run_tests and judge_changes.

atlas develop judges each change of its agents without a model: what compiles, which tests
pass and how far the others get, and what got worse. These two tools give that judgement to
the agent of an editor (OpenCode, VS Code), which changes the code with its own tools:

    run_tests       the tests of a plan of atlas develop (or those [tests] writes), run:
                    by test, whether it passes, how far it got and what it said
    judge_changes   what changed since the last call (or against git), whether those files
                    compile, and which tests got further or went back since the last call

They run code (the tests, the compiler), as run_test: they are only offered when asked for
([mcp] tools, or the tools of an agent), and the MCP server marks them as destructive so
that the editor asks first. They change no code; what they remember between calls (the
hashes of the files, the state of each test and the last plan) is in <workspace>/judge.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from typing import Any

from hephesto.build import BuildTools
from hephesto.edit import CodeTools
from atlas.plugins import register_methods

__all__ = ["JUDGE_TOOL_NAMES", "JudgeTools"]

JUDGE_TOOL_NAMES = ("run_tests", "judge_changes")
SHOWN_ERRORS = 5           # errors of the compiler by file


class JudgeTools:
    def __init__(self, cfg, files: CodeTools, build: BuildTools):
        self.cfg, self.files, self.build = cfg, files, build
        self.state_path = cfg.workspace / "judge.json"

    @classmethod
    def for_context(cls, context: Any) -> "JudgeTools":
        """One instance per context, over its file and build tools."""
        tools = getattr(context, "_judge_tools", None)
        if tools is None:
            tools = cls(context.cfg, CodeTools.for_context(context), BuildTools.for_context(context))
            context._judge_tools = tools
        return tools

    # --- what is remembered between calls -------------------------------------------------
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- the tests ----------------------------------------------------------------------
    def _tests(self, plan: str) -> tuple[str, list[str], str] | dict:
        """(what they are, the tests, the progress pattern) of a plan of atlas develop, or of
        [tests] without one; or {"error"}."""
        from hephesto.develop import _files
        from atlas.config import ConfigError
        base = self.files.base
        if plan:
            p = (base / plan).resolve()
            if base not in p.parents or not p.is_file():
                return {"error": f"{plan} is not a plan file inside {base}"}
            from hephesto.develop import load_plan
            try:
                loaded = load_plan(p)
            except ConfigError as e:
                return {"error": str(e)}
            return p.relative_to(base).as_posix(), _files(loaded.tests, base), loaded.progress
        languages = self.cfg["tests"]["languages"]
        patterns = [re.sub(r"\{[^}]*\}", "*", spec["path"]) for spec in languages.values()]
        if not patterns:
            return {"error": "no plan, and no [tests] languages in atlas.toml: which tests to run? Give a plan of "
                             "atlas develop"}
        return "[tests]", _files(patterns, base), ""

    def _run(self, tests: list[str], progress: str) -> dict[str, dict]:
        """Each test run: {"passed", "score", "output", "code_at"}, as atlas develop sees it."""
        out = {}
        for t in tests:
            res = self.build.run_test(t)
            output = res.get("output") or res.get("error") or json.dumps(res.get("errors", []))
            if res.get("passed"):
                score = math.inf
            else:
                found = re.findall(progress, res.get("output", "")) if progress else []
                score = float(found[-1]) if found else 0.0
            out[t] = {"passed": bool(res.get("passed")), "score": score, "output": output,
                      "code_at": res.get("code_at", [])}
        return out

    @staticmethod
    def _shown(result: dict, progress: str) -> dict:
        """A test for the client: whether it passes, how far it got, what it said."""
        from hephesto.develop import _last_lines
        if result["passed"]:
            return {"passed": True}
        cited = result["code_at"][:1]
        return {"passed": False, **({"progress": result["score"]} if progress else {}),
                "output": _last_lines(result["output"]),
                **({"code_at": f"{cited[0]['file']}:{cited[0]['line']}"} if cited else {})}

    @staticmethod
    def _stored(results: dict[str, dict]) -> dict:
        return {t: {"passed": r["passed"], "score": None if r["score"] == math.inf else r["score"]}
                for t, r in results.items()}

    def run_tests(self, plan: str = "") -> dict:
        """Runs the acceptance tests of a plan of atlas develop (`plan`: its TOML file, such as
        "tasks/server.toml"), or those `atlas tests` writes ([tests] of atlas.toml) without a
        plan. Returns, by test, whether it passes and, when it fails, how far it got (the
        `progress` of the plan) and what it said. It runs code: the tests of the project.
        judge_changes uses the same tests afterwards."""
        found = self._tests(plan)
        if isinstance(found, dict):
            return found
        name, tests, progress = found
        if not tests:
            return {"error": f"no test file matches the tests of {name}"}
        results = self._run(tests, progress)
        state = self._state()
        state.update(plan=plan, tests=self._stored(results))
        self._save(state)
        return {"tests_of": name, "passed": sum(r["passed"] for r in results.values()), "of": len(tests),
                "tests": {t: self._shown(r, progress) for t, r in results.items()}}

    # --- what changed -------------------------------------------------------------------
    def _snapshot(self) -> dict[str, str]:
        """The hash of every file of the code roots (not excluded), by its path."""
        out = {}
        for root in self.files.roots:
            for p in (root.rglob("*") if root.is_dir() else []):
                if p.is_file() and not self.files._excluded(p):
                    out[self.files._show(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def _git_changes(self) -> list[str] | None:
        """The files of the code roots that differ from the last commit, new ones included;
        None if the folder is not a git repository with commits."""
        base = self.files.base

        def git(*args: str) -> list[str] | None:
            try:
                ran = subprocess.run(["git", *args], cwd=base, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return [f for f in ran.stdout.split("\0") if f] if ran.returncode == 0 else None

        changed = git("diff", "--name-only", "--relative", "-z", "HEAD")
        new = git("ls-files", "--others", "--exclude-standard", "-z")
        if changed is None or new is None:
            return None
        inside = lambda f: any(r == (base / f).resolve() or r in (base / f).resolve().parents
                               for r in self.files.roots) and not self.files._excluded((base / f).resolve())
        return sorted({f for f in changed + new if inside(f)})

    def judge_changes(self, paths: list[str] | None = None) -> dict:
        """Judges the changes to the code as atlas develop does: which files changed (against
        the last commit if the folder is a git repository; if not, since the last call), whether
        those files (or `paths`, files of the code) compile with check_code, and which tests
        pass and which got further or went back since the last call of run_tests or of this
        tool. The tests are those of the last plan given to run_tests, or [tests] of
        atlas.toml. It runs code: the compiler and the tests of the project."""
        state, now = self._state(), self._snapshot()
        git = self._git_changes()
        if git is not None:
            since, changed = "the last commit (git)", git
        elif "files" in state:
            before = state["files"]
            since = "the last call"
            changed = sorted(f for f in before.keys() | now.keys() if before.get(f) != now.get(f))
        else:
            since, changed = "nothing: this is the first call, from now on changes are against it", []
        out: dict = {"changed_since": since, "changed": [f for f in changed if f in now],
                     **({"deleted": deleted} if (deleted := [f for f in changed if f not in now]) else {})}
        compiles, errors = [], {}
        for f in (paths if paths else out["changed"]):
            p, err = self.files._resolve(f)
            if err or p is None or not p.is_file():
                errors[f] = [(err or {}).get("error") or "not a file"]
                continue
            if not (self.build.check_commands.get(p.suffix.lower().lstrip(".")) or self.build.check_command):
                continue
            res = self.build.check_code(f)
            if res.get("ok"):
                compiles.append(f)
            else:
                shown = [f"line {e.get('line')}: {e.get('message')}" for e in res.get("errors", [])[:SHOWN_ERRORS]]
                errors[f] = shown or [res.get("error") or res.get("output", "")[-300:]]
        out.update(compiles=compiles, **({"does_not_compile": errors} if errors else {}))
        state["files"] = now
        found = self._tests(state.get("plan", ""))
        if isinstance(found, dict):
            out["tests"] = found
        else:
            name, tests, progress = found
            results = self._run(tests, progress)
            before = state.get("tests", {})
            score = lambda s: math.inf if s["score"] is None else s["score"]
            advanced, back = [], []
            for t, r in results.items():
                if t not in before:
                    continue
                was = score(before[t])
                if r["score"] > was:
                    advanced.append(t)
                elif r["score"] < was:
                    back.append({"test": t, "was": "passed" if was == math.inf else f"got to {was:g}",
                                 "now": self._shown(r, progress)})
            out["tests"] = {"tests_of": name, "passed": sum(r["passed"] for r in results.values()), "of": len(tests),
                            "advanced": advanced, "went_back": back,
                            "failing": {t: self._shown(r, progress) for t, r in results.items() if not r["passed"]}}
            if not before:
                out["tests"]["note"] = "first run of these tests: the next call says what got further or went back"
            state["tests"] = self._stored(results)
        self._save(state)
        return out


register_methods(JudgeTools, JUDGE_TOOL_NAMES, writes=JUDGE_TOOL_NAMES)
