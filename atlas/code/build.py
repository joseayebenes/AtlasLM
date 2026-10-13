"""The compiler and the tests, for coding agents: what a model cannot see by reading the code.

    check_code   compiles one file of the project without producing code (a semantic check)
                 and returns the errors and warnings of the compiler, each with its place
                 (the same message in many lines, once, with those lines)
    run_test     builds a test procedure of the project and runs it: passed or not, and its
                 output; if it does not build, the errors of the compiler. A test in a language
                 that is not built (a Python script) is just run

The commands come from [sources.code], as lists of arguments (no shell), and nothing is tied
to a compiler. Placeholders:

    {file}      the absolute path of the file
    {include}   one -I<folder> per folder of the code roots that holds source files
    {build}     <workspace>/build, where the compiler leaves what it produces
    {exe}       the executable of run_test: <workspace>/build/<name of the file>

With GNAT (installed with Alire, `alr toolchain --select gnat_native gprbuild`):

    check_command = ["gcc", "-c", "-gnatc", "{include}", "{file}"]
    test_command = ["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"]
    tool_path = ["~/.local/share/alire/toolchains/gnat_native_*/bin"]

`run_command` runs what test_command built (by default the executable itself, "{exe}"); a
wrapper there can start what the test needs first, e.g. ["with_server", "{exe}"] for a test
that talks to a server. `test_commands` runs the tests of other languages as they are, by
extension, with no build: { py = ["with_server", "python3", "{file}"] }.

`tool_path` goes before PATH for these commands (globs allowed; a relative folder is relative
to where atlas runs, like the code roots), so the toolchain and the project's own scripts are
used without touching the shell. A project with a GPR file would use gprbuild instead. The commands
have the trust of atlas.toml itself; run_test runs code, so it is only offered when asked for.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from atlas.code.tools import CodeTools
from atlas.plugins import register_methods

__all__ = ["BuildTools", "BUILD_TOOL_NAMES", "parse_messages"]

BUILD_TOOL_NAMES = ("check_code", "run_test")
MAX_MESSAGES = 10          # of each kind: the first errors are the ones to fix, and a small model's context is small
MAX_OUTPUT_CHARS = 3000
# "tracks.adb:16:04: error: ..." (GNAT, gcc, clang); the kind is optional
MESSAGE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?:(?P<kind>error|warning|info|"
                     r"style|note)\s*:\s*)?(?P<text>.+)$", re.M | re.I)


def parse_messages(output: str) -> dict:
    """The errors and warnings of a compiler output, each {file, line, column, message}. The
    next messages of the same place go in its `details` (GNAT gives hints there: "use clause
    would make operation legal"), and the same message of the same file goes once, with the
    other lines where it is in `also_at` (one fix, many places). At most MAX_MESSAGES of each
    kind, and how many more there were."""
    out: dict = {"errors": [], "warnings": []}
    places: dict = {}                 # place -> its item, or None when it repeats another one
    same, more = {}, {"errors": 0, "warnings": 0}
    for m in MESSAGE.finditer(output):
        kind = (m["kind"] or "error").lower()
        key = "warnings" if kind in ("warning", "info", "style", "note") else "errors"
        file, text = Path(m["file"]).name, m["text"].strip()
        place = (key, file, m["line"], m["col"])
        if place in places:
            item = places[place]
            if item is not None and text != item["message"] and text not in item.get("details", []):
                item.setdefault("details", []).append(text)
            continue
        if (key, file, text) in same:
            same[key, file, text].setdefault("also_at", []).append(int(m["line"]))
            places[place] = None
            continue
        if len(out[key]) == MAX_MESSAGES:
            more[key] += 1
            places[place] = None
            continue
        item = {"file": file, "line": int(m["line"]), "column": int(m["col"]) if m["col"] else None, "message": text}
        same[key, file, text] = places[place] = item
        out[key].append(item)
    out.update({f"more_{k}": n for k, n in more.items() if n})
    return out


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else "…" + text[-MAX_OUTPUT_CHARS:]


class BuildTools:
    def __init__(self, files: CodeTools, build_dir: str | Path, check_command: Iterable[str] = (),
                 test_command: Iterable[str] = (), tool_path: Iterable[str] = (), timeout: int = 120,
                 run_command: Iterable[str] = ("{exe}",), test_commands: dict | None = None):
        self.files = files                    # the confinement of the file tools: same roots and exclusions
        self.build_dir = Path(build_dir)
        self.check_command, self.test_command = list(check_command), list(test_command)
        self.run_command = list(run_command) or ["{exe}"]
        self.test_commands = {k.lower().lstrip("."): list(v) for k, v in (test_commands or {}).items()}
        self.tool_path = [p for pattern in tool_path            # the commands run in build_dir: absolute paths
                          for p in sorted(glob.glob(os.path.abspath(os.path.expanduser(pattern))), reverse=True)]
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, files: CodeTools | None = None) -> "BuildTools":
        sc = cfg["sources"].get("code") or {}
        return cls(files or CodeTools.from_config(cfg), cfg.workspace / "build", sc.get("check_command", []),
                   sc.get("test_command", []), sc.get("tool_path", []), int(sc.get("command_timeout", 120)),
                   sc.get("run_command", ["{exe}"]), sc.get("test_commands", {}))

    @classmethod
    def for_context(cls, context: Any) -> "BuildTools":
        tools = getattr(context, "_build_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg, CodeTools.for_context(context))
            setattr(context, "_build_tools", tools)
        return tools

    def _include(self) -> list[str]:
        folders = sorted({p.parent for root in self.files.roots if root.is_dir() for p in root.rglob("*")
                          if p.is_file() and not self.files._excluded(p)})
        return [f"-I{f}" for f in folders]

    def _command(self, template: list[str], file: Path) -> list[str]:
        values = {"{file}": str(file), "{build}": str(self.build_dir), "{exe}": str(self.build_dir / file.stem)}
        out: list[str] = []
        for arg in template:
            if arg == "{include}":
                out += self._include()
            else:
                for key, value in values.items():
                    arg = arg.replace(key, value)
                out.append(arg)
        return out

    def _run(self, args: list[str]) -> subprocess.CompletedProcess | dict:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PATH": os.pathsep.join([*self.tool_path, os.environ.get("PATH", "")])}
        try:
            return subprocess.run(args, cwd=self.build_dir, env=env, capture_output=True, text=True,
                                  timeout=self.timeout)
        except FileNotFoundError:
            return {"error": f"{args[0]} not found: install it or set [sources.code] tool_path"}
        except subprocess.TimeoutExpired:
            return {"error": f"{args[0]} took more than {self.timeout} s ([sources.code] command_timeout)"}

    def _source(self, path: str) -> tuple[Path | None, dict | None]:
        p, err = self.files._resolve(path)
        if err:
            return None, err
        if not p.is_file():
            return None, {"error": f"{path} does not exist"}
        return p, None

    def check_code(self, path: str) -> dict:
        """Compiles one source file of the project without producing code, as the compiler
        of the project does, and returns its errors and warnings, each with file, line,
        column and message. Use it on every file you change or create, and fix every error,
        before saying the change is done."""
        if not self.check_command:
            return {"error": "no compiler configured ([sources.code] check_command)"}
        p, err = self._source(path)
        if err:
            return err
        res = self._run(self._command(self.check_command, p))
        if isinstance(res, dict):
            return res
        output = res.stdout + res.stderr
        found = parse_messages(output)
        ok = res.returncode == 0 and not found["errors"]
        return {"file": self.files._show(p), "ok": ok, **found,
                **({"output": _tail(output)} if not ok and not found["errors"] else {})}

    def run_test(self, path: str) -> dict:
        """Builds a test of the project (an Ada test procedure: a main subprogram, one per
        file) and runs it; a test that is not built, such as a Python script, is just run.
        It passes when it ends with exit code 0; a failed check that raises an exception
        makes it fail, with the message in `output`. If it does not build, returns the errors
        of the compiler; if it builds, its warnings too. It runs code: use it on tests of the
        project only."""
        p, err = self._source(path)
        if err:
            return err
        direct = self.test_commands.get(p.suffix.lower().lstrip("."))
        if direct:                                    # not built: run as it is
            ran = self._run(self._command(direct, p))
            if isinstance(ran, dict):
                return {"file": self.files._show(p), "passed": False, **ran}
            return {"file": self.files._show(p), "passed": ran.returncode == 0, "exit_code": ran.returncode,
                    "output": _tail(ran.stdout + ran.stderr)}
        if not self.test_command:
            return {"error": "no test command configured ([sources.code] test_command)"}
        res = self._run(self._command(self.test_command, p))
        if isinstance(res, dict):
            return res
        found = parse_messages(res.stdout + res.stderr)
        exe = self.build_dir / p.stem
        if res.returncode != 0 or found["errors"] or not exe.exists():
            return {"file": self.files._show(p), "built": False, **found,
                    **({"output": _tail(res.stdout + res.stderr)} if not found["errors"] else {})}
        warnings = {"warnings": found["warnings"]} if found["warnings"] else {}
        ran = self._run(self._command(self.run_command, p))
        if isinstance(ran, dict):
            return {"file": self.files._show(p), "built": True, "passed": False, **ran, **warnings}
        return {"file": self.files._show(p), "built": True, "passed": ran.returncode == 0,
                "exit_code": ran.returncode, "output": _tail(ran.stdout + ran.stderr), **warnings}


register_methods(BuildTools, BUILD_TOOL_NAMES, writes=("run_test",))
