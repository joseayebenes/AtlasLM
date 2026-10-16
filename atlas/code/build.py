"""The compiler and the tests, for coding agents: what a model cannot see by reading the code.

    check_code   compiles one file of the project without producing code (a semantic check)
                 and returns the errors and warnings of the compiler, each with its place
                 (the same message in many lines, once, with those lines)
    run_test     builds a test procedure of the project and runs it: passed or not, and its
                 output; if it does not build, the errors of the compiler. A test in a language
                 that is not built (a Python script) is just run
    run_program  runs the program of the project with some lines as its input and returns
                 what it wrote: to try what the code does before and after a change
    library_spec the public declarations of a library unit: of the runtime of the compiler
                 ([sources.code] library: GNAT's adainclude) or of the project (library.py)
    search_reference  the manuals of the language and of the compiler ([sources.code]
                 reference) and the declarations of the library, by words (reference.py)

When an output cites places of the code ("controller.adb:103", the place of an exception, or
'File "x.py", line 12'), the lines around them come with it, in `code_at`: a small model that
gets "length check failed at controller.adb:103" does not always go and read that line.
Only the files an agent can change are shown (not the protected ones: the tests).

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

`check_commands` replaces check_command for the files of an extension: GNAT finds more errors
when it generates code (a String assigned one of another length is a warning then, and an
error with -gnatwE), but it cannot generate code for a package spec alone, so
{ ads = ["gcc", "-c", "-gnatc", "{include}", "{file}"] } with a check_command without -gnatc.

`run_command` runs what test_command built (by default the executable itself, "{exe}"); a
wrapper there can start what the test needs first, e.g. ["with_server", "{exe}"] for a test
that talks to a server. `test_commands` runs the tests of other languages as they are, by
extension, with no build: { py = ["with_server", "python3", "{file}"] }. `program_command` is
what run_program runs, with the lines on its standard input: the program itself, or a script
that passes them on to it (e.g. ["with_server", "session"], a client that sends each line to the
server and prints the answer).

`tool_path` goes before PATH for these commands (globs allowed; a relative folder is relative
to where atlas runs, like the code roots), so the toolchain and the project's own scripts are
used without touching the shell. A project with a GPR file would use gprbuild instead. The commands
have the trust of atlas.toml itself; run_test runs code, so it is only offered when asked for.

The file tools of the same context get two hooks from here (CodeTools.for_context):
- a change that breaks the syntax of a file that had none is not made. `syntax_commands`
  checks the syntax by extension, on a copy of the new text in <workspace>/build/syntax
  (GNAT: { adb = ["gcc", "-c", "-gnats", "{file}"] }); Python needs no command (ast). A
  `begin` left without its `end` by an edit of gemma-4-26b went through 5 compilations of a
  37-minute run before it was undone;
- with `check_on_edit` (true by default), every change returns what check_code says of the
  file, in `check`: one turn less, and an agent cannot say it compiles without seeing it.

Each error of the compiler gets a `hint` when one of the hints matches it: those of the files
of [sources.code] hints first, then those of hints.toml (GNAT). A rule in the instructions of
the agent was not enough: the one on String bounds was there, and the error came 16 times in a
run; the hint comes with the error, when it is needed. The report of atlas develop says which
errors came without a hint, to add one after each run. An error that names something that
does not exist brings what does: '"Arguments" not declared in "Command_Line"' comes with what
Ada.Command_Line declares like it (`declared_in`), and '"LF" is undefined' with the units that
declare LF (`found_in`).
"""
from __future__ import annotations

import ast
import glob
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from atlas.code.library import Library, with_clauses
from atlas.code.reference import Reference
from atlas.code.tools import CodeTools, rebased
from atlas.config import ConfigError
from atlas.plugins import register_methods

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

__all__ = ["BuildTools", "BUILD_TOOL_NAMES", "HINTS", "hint_for", "load_hints", "parse_messages"]

HINTS = Path(__file__).parent / "hints.toml"

BUILD_TOOL_NAMES = ("check_code", "run_test", "run_program", "library_spec", "search_reference")
MAX_MESSAGES = 10          # of each kind: the first errors are the ones to fix, and a small model's context is small
MAX_EDIT_ERRORS = 5        # errors of check_code in the answer of a change
MAX_OUTPUT_CHARS = 3000
MAX_INPUT_LINES = 100      # of run_program
MAX_CITED, CITED_CONTEXT = 4, 3     # places of the code shown from an output, and lines around each one
# "controller.adb:103" (GNAT, gcc and most tools) or 'File "/x/client.py", line 12' (Python)
CITED = re.compile(r'File "(?P<py>[^"]+)", line (?P<py_line>\d+)|(?P<file>[\w./\\~-]+\.[A-Za-z]\w*):(?P<line>\d+)')
NOT_DECLARED = re.compile(r'"(?P<name>[^"]+)" not declared in "(?P<package>[^"]+)"')
UNKNOWN = re.compile(r'"(?P<name>[^"]+)" is (?:undefined|not visible)')
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


def load_hints(paths: Iterable[str | Path]) -> list[tuple[re.Pattern, str]]:
    """The hints of these files, in order: each [[hint]] has `match`, a regular expression,
    and `hint`, the text, with {group} for the named groups of match."""
    out = []
    for path in paths:
        path = Path(path)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"hints {path}: {e}") from None
        for i, h in enumerate(data.get("hint", []), 1):
            if not (isinstance(h, dict) and isinstance(h.get("match"), str) and isinstance(h.get("hint"), str)):
                raise ConfigError(f"hints {path}: [[hint]] {i} needs match and hint, two texts")
            try:
                out.append((re.compile(h["match"]), " ".join(h["hint"].split())))
            except re.error as e:
                raise ConfigError(f"hints {path}: [[hint]] {i}: match is not a valid regular expression: {e}") from None
    return out


class _Keep(dict):
    def __missing__(self, key: str) -> str:            # a group that did not match stays as it was
        return "{" + key + "}"


def hint_for(hints: list[tuple[re.Pattern, str]], message: str, details: Iterable[str] = ()) -> str:
    """The text of the first hint that matches the message or one of its details, or ""."""
    for rx, text in hints:
        for candidate in (message, *details):
            m = rx.search(candidate or "")
            if m:
                return text.format_map(_Keep({k: v for k, v in m.groupdict().items() if v is not None}))
    return ""


def _relative(cfg, path: str) -> Path:
    """A path of [sources.code] that names a file of the project: relative to atlas.toml."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else cfg.path.parent / p


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else "…" + text[-MAX_OUTPUT_CHARS:]


def cited_places(output: str) -> list[tuple[str, int]]:
    """The places of the code an output cites, (file as written, line), in order, once each."""
    found = []
    for m in CITED.finditer(output):
        place = (m["py"], int(m["py_line"])) if m["py"] else (m["file"], int(m["line"]))
        if place not in found:
            found.append(place)
    return found


class BuildTools:
    def __init__(self, files: CodeTools, build_dir: str | Path, check_command: Iterable[str] = (),
                 test_command: Iterable[str] = (), tool_path: Iterable[str] = (), timeout: int = 120,
                 run_command: Iterable[str] = ("{exe}",), test_commands: dict | None = None,
                 check_commands: dict | None = None, program_command: Iterable[str] = (),
                 syntax_commands: dict | None = None, check_on_edit: bool = True,
                 hints: list[tuple[re.Pattern, str]] | None = None, library: Iterable[str] = (),
                 reference: Iterable[str] = (), reference_db: str | Path | None = None):
        self.files = files                    # the confinement of the file tools: same roots and exclusions
        self.hints = hints if hints is not None else load_hints([HINTS])
        self.library = Library(library, files.roots)
        self.reference = Reference(reference_db or Path(build_dir).parent / "reference.db", reference, self.library)
        self.build_dir = Path(build_dir)
        self.check_command, self.test_command = list(check_command), list(test_command)
        self.run_command = list(run_command) or ["{exe}"]
        by_extension = lambda d: {k.lower().lstrip("."): list(v) for k, v in (d or {}).items()}
        self.test_commands, self.check_commands = by_extension(test_commands), by_extension(check_commands)
        self.syntax_commands = by_extension(syntax_commands)
        self.check_on_edit = check_on_edit
        self.program_command = list(program_command)
        self.tool_path = [p for pattern in tool_path            # the commands run in build_dir: absolute paths
                          for p in sorted(glob.glob(os.path.abspath(os.path.expanduser(pattern))), reverse=True)]
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, files: CodeTools | None = None, base: str | Path | None = None) -> "BuildTools":
        """Over [sources.code]. With `base`, a copy of the working folder (a parallel attempt of
        atlas develop): its own build folder, and the folders of tool_path in the copy (the
        scripts of the project build what is in it); the manuals and the library are shared."""
        sc = cfg["sources"].get("code") or {}
        tool_path = sc.get("tool_path", [])
        build_dir = cfg.workspace / "build"
        if base is not None:
            tool_path = [rebased(p, base) if Path(p).expanduser().is_absolute() else str(Path(base) / p)
                         for p in tool_path]          # relative ones are relative to where atlas runs: the copy
            build_dir = Path(base) / ".atlas" / "build"
        return cls(files or CodeTools.from_config(cfg, base=base), build_dir, sc.get("check_command", []),
                   sc.get("test_command", []), tool_path, int(sc.get("command_timeout", 120)),
                   sc.get("run_command", ["{exe}"]), sc.get("test_commands", {}), sc.get("check_commands", {}),
                   sc.get("program_command", []), sc.get("syntax_commands", {}), sc.get("check_on_edit", True),
                   load_hints([*(_relative(cfg, h) for h in sc.get("hints", [])), HINTS]), sc.get("library", []),
                   sc.get("reference", []), cfg.workspace / "reference.db")

    @classmethod
    def for_context(cls, context: Any) -> "BuildTools":
        """One instance per context, over its CodeTools, to which it gives the syntax check
        and check_code after each change (see the module)."""
        tools = getattr(context, "_build_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg, CodeTools.for_context(context))
            setattr(context, "_build_tools", tools)
            tools.hook()
        return tools

    def hook(self) -> None:
        """Gives the file tools the syntax check and, with check_on_edit, check_code after
        each change."""
        self.files.syntax = self.syntax_errors
        self.files.after_change = self.after_change if self.check_on_edit else None

    def syntax_errors(self, p: Path, text: str) -> list[dict] | None:
        """The syntax errors of `text` as the content of the file `p`: with the command of its
        extension in syntax_commands, run on a copy, or with ast for Python. None when nothing
        checks the syntax of that extension (or its command is missing); an empty command
        turns the check of its extension off, Python's too."""
        ext = p.suffix.lower().lstrip(".")
        command = self.syntax_commands.get(ext)
        if ext in self.syntax_commands and not command:
            return None
        if command:
            copy = self.build_dir / "syntax" / p.name
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(text, encoding="utf-8")
            ran = self._run(self._command(command, copy))
            if isinstance(ran, dict):
                return None
            output = ran.stdout + ran.stderr
            errors = parse_messages(output)["errors"]
            if ran.returncode != 0 and not errors:
                errors = [{"line": None, "message": _tail(output.strip())[-300:]}]
            return self._hinted([{k: v for k, v in e.items() if k != "file" and v is not None} for e in errors],
                                text=text)
        if ext == "py":
            try:
                ast.parse(text)
            except SyntaxError as e:
                return [{"line": e.lineno, "column": e.offset, "message": e.msg}]
            return []
        return None

    def after_change(self, p: Path) -> dict | None:
        """What check_code says of a file just changed, short: ok, and its first errors."""
        if not (self.check_commands.get(p.suffix.lower().lstrip(".")) or self.check_command):
            return None
        res = self.check_code(self.files._show(p))
        if "ok" not in res:                                  # the compiler is missing: nothing to say
            return None
        errors = [{k: v for k, v in e.items() if k != "file" and v is not None} for e in res.get("errors", [])]
        more = len(errors) - MAX_EDIT_ERRORS + res.get("more_errors", 0)
        return {"ok": res["ok"], **({"errors": errors[:MAX_EDIT_ERRORS]} if errors else {}),
                **({"more_errors": more} if more > 0 else {}),
                **({"output": res["output"][-500:]} if not res["ok"] and not errors and res.get("output") else {})}

    def _include(self) -> list[str]:
        folders = sorted({p.parent for root in self.files.roots if root.is_dir() for p in root.rglob("*")
                          if p.is_file() and not self.files._excluded(p)})
        return [f"-I{f}" for f in folders]

    def _command(self, template: list[str], file: Path | None) -> list[str]:
        values = {"{build}": str(self.build_dir)}
        if file is not None:
            values.update({"{file}": str(file), "{exe}": str(self.build_dir / file.stem)})
        out: list[str] = []
        for arg in template:
            if arg == "{include}":
                out += self._include()
            else:
                for key, value in values.items():
                    arg = arg.replace(key, value)
                out.append(arg)
        return out

    def _run(self, args: list[str], input: str | None = None) -> subprocess.CompletedProcess | dict:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PATH": os.pathsep.join([*self.tool_path, os.environ.get("PATH", "")])}
        try:
            return subprocess.run(args, cwd=self.build_dir, env=env, capture_output=True, text=True,
                                  timeout=self.timeout, input=input,
                                  stdin=None if input is not None else subprocess.DEVNULL)
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

    def _hinted(self, errors: list[dict], source: Path | None = None, text: str | None = None) -> list[dict]:
        """The errors, each with the `hint` that matches it, if any, and what the library has
        for one that names something that does not exist (`declared_in`, `found_in`). `source`
        (or its `text`): the file of the errors, whose with clauses say which package a name
        like "Command_Line" is."""
        withs = None
        for e in errors:
            message = e.get("message", "")
            hint = hint_for(self.hints, message, e.get("details", []))
            if hint:
                e["hint"] = hint
            m = NOT_DECLARED.search(message) or UNKNOWN.search(message)
            if m is None:
                continue
            if withs is None:
                if text is None and source is not None and source.is_file():
                    text = source.read_text(encoding="utf-8", errors="replace")
                withs = with_clauses(text or "")
            try:
                if m.re is NOT_DECLARED:
                    found = self.library.close_to(m["package"], m["name"], withs)
                    if found:
                        e["declared_in"] = found
                else:
                    found = self.library.declaring(m["name"])
                    if found:
                        e["found_in"] = found
            except (OSError, ImportError):             # no tree-sitter-ada, or a file that went away
                pass
        return errors

    def library_spec(self, unit: str, name: str = "") -> dict:
        """The public declarations of a library unit: a package of the runtime of the
        compiler (Ada.Command_Line, Ada.Strings.Unbounded, GNAT.Sockets...) or a spec of the
        project: its subprograms with their parameters, its types, constants and exceptions,
        with their comment. `name` keeps those whose name contains it. Look here before using
        a subprogram you have not used in this project: do not guess names or parameters."""
        try:
            return self.library.spec(unit, name, shown=self._shown)
        except ImportError as e:
            return {"error": f"library_spec needs tree-sitter-ada: {e}"}

    def search_reference(self, query: str, k: int = 5) -> dict:
        """Searches by words the manuals of the language and of the compiler (the reference
        manual and the user's guide of GNAT) and the declarations of the library: which
        package has a subprogram for something ("split a string by a separator"), what a
        pragma, an attribute or a switch of the compiler does. Returns `k` passages, each with
        where it comes from (a manual and its section, or Unit.Name), the best first."""
        if not (self.reference.patterns or self.library.folders):
            return {"error": "no manuals and no library to search: [sources.code] reference and library"}
        try:
            return {"passages": self.reference.search(query, k)}
        except ImportError as e:
            return {"error": f"search_reference needs tree-sitter-ada for the library: {e}"}

    def _shown(self, p: Path) -> str:
        """A spec as an answer shows it: from the working folder if it is in the project, else
        its name (the runtime of the compiler cannot be read with read_file)."""
        shown = self.files._show(p)
        return shown if not Path(shown).is_absolute() else p.name

    def code_at(self, output: str) -> list[dict]:
        """The lines around the places of the code that `output` cites, in the files that an
        agent can change (in the roots, neither excluded nor protected), MAX_CITED at most."""
        out, index = [], None
        for name, line in cited_places(output):
            p, _ = self.files._resolve(name)
            if p is None or not p.is_file():               # "controller.adb": by its name, if only one has it
                if index is None:
                    index = {}
                    for root in self.files.roots:
                        for f in (root.rglob("*") if root.is_dir() else []):
                            if f.is_file():
                                index.setdefault(f.name, []).append(f)
                same = index.get(Path(name).name, [])
                p = same[0] if len(same) == 1 else None
            if p is None or self.files._excluded(p) or self.files.protected_file(p) or \
                    any(c["file"] == self.files._show(p) and c["line"] == line for c in out):
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if not 1 <= line <= len(lines):
                continue
            lo, hi = max(1, line - CITED_CONTEXT), min(len(lines), line + CITED_CONTEXT)
            out.append({"file": self.files._show(p), "line": line,
                        "source": "\n".join(f"{'>' if i == line else ' '}{i:4d} | {lines[i - 1]}"
                                            for i in range(lo, hi + 1))})
            if len(out) == MAX_CITED:
                break
        return out

    def _ran(self, ran: subprocess.CompletedProcess) -> dict:
        """exit_code, output and the code it cites."""
        output = ran.stdout + ran.stderr
        cited = self.code_at(output)
        return {"exit_code": ran.returncode, "output": _tail(output), **({"code_at": cited} if cited else {})}

    def check_code(self, path: str) -> dict:
        """Compiles one source file of the project without producing code, as the compiler
        of the project does, and returns its errors and warnings, each with file, line,
        column and message. Use it on every file you change or create, and fix every error,
        before saying the change is done."""
        p, err = self._source(path)
        if err:
            return err
        command = self.check_commands.get(p.suffix.lower().lstrip("."), self.check_command)
        if not command:
            return {"error": "no compiler configured ([sources.code] check_command)"}
        res = self._run(self._command(command, p))
        if isinstance(res, dict):
            return res
        output = res.stdout + res.stderr
        found = parse_messages(output)
        self._hinted(found["errors"], p)
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
            return {"file": self.files._show(p), "passed": ran.returncode == 0, **self._ran(ran)}
        if not self.test_command:
            return {"error": "no test command configured ([sources.code] test_command)"}
        res = self._run(self._command(self.test_command, p))
        if isinstance(res, dict):
            return res
        found = parse_messages(res.stdout + res.stderr)
        self._hinted(found["errors"], p)
        exe = self.build_dir / p.stem
        if res.returncode != 0 or found["errors"] or not exe.exists():
            return {"file": self.files._show(p), "built": False, **found,
                    **({"output": _tail(res.stdout + res.stderr)} if not found["errors"] else {})}
        warnings = {"warnings": found["warnings"]} if found["warnings"] else {}
        ran = self._run(self._command(self.run_command, p))
        if isinstance(ran, dict):
            return {"file": self.files._show(p), "built": True, "passed": False, **ran, **warnings}
        return {"file": self.files._show(p), "built": True, "passed": ran.returncode == 0, **self._ran(ran),
                **warnings}

    def run_program(self, input_lines: list[str]) -> dict:
        """Runs the program of the project with `input_lines` as its input, one line each,
        and returns what it wrote (`output`) and its exit code; the lines of the code its
        output cites (the place of an exception) are in `code_at`. Use it to see what the
        code does with an input: to find why a test fails, and to check a fix. It runs code."""
        if not self.program_command:
            return {"error": "no program to run configured ([sources.code] program_command)"}
        if not isinstance(input_lines, list) or not all(isinstance(l, str) for l in input_lines):
            return {"error": "input_lines must be a list of strings, one per line"}
        if len(input_lines) > MAX_INPUT_LINES:
            return {"error": f"at most {MAX_INPUT_LINES} input lines"}
        ran = self._run(self._command(self.program_command, None), "".join(f"{l}\n" for l in input_lines))
        return ran if isinstance(ran, dict) else self._ran(ran)


register_methods(BuildTools, BUILD_TOOL_NAMES, writes=("run_test", "run_program"))
