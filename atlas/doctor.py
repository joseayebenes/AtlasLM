"""What can fail on a new machine, found before a run: `atlas doctor`.

`diagnose(cfg)` reads the configuration and the machine and returns a list of Finding
(level "ok", "warn" or "fail", where, message, fix). It only reads: it creates no
workspace, no database and no build folder, and the only request to the model is the last
check, the same one as `atlas check` (atlas.llm.check_service), which `connect=False` skips.

    Python and platform; the extras that the configured steps and tools import; each path of
    [[sources.docs]] and [sources.code] roots and how many files it gives (0 = warn); the
    DOORS exchange database (schema version, modules, filled_at); the commands of
    [sources.code] (check_command, test_command, syntax_commands, language_server...) found
    with shutil.which on the PATH that tool_path extends; the ${VAR} of the TOML this process
    lacks; the workspace; the connection.
"""
from __future__ import annotations

import fnmatch
import glob
import importlib.util
import os
import platform
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from atlas import plugins
from atlas.config import Config, unset_hint

LEVELS = ("ok", "warn", "fail")
TESTED_PYTHON = ((3, 10), (3, 13))          # the versions of constraints.txt and of the tests


@dataclass
class Finding:
    level: str          # ok | warn | fail
    where: str          # the section or the thing checked: "python", "sources.code", "connection"...
    message: str
    fix: str = ""


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):       # a parent package missing
        return False


def _steps(cfg: Config) -> set[str]:
    return set(cfg["pipeline"]["steps"])


def _enabled(section: dict | None) -> bool:
    return bool(section) and section.get("enabled", True)


# --------------------------------------------------------------------------
# Python, platform and extras
# --------------------------------------------------------------------------

def _files(n: int) -> str:
    return f"{n} file" + ("" if n == 1 else "s")


def _python(cfg: Config) -> list[Finding]:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro} on {platform.platform()}"
    if v[:2] < TESTED_PYTHON[0]:
        return [Finding("fail", "python", text, "atlas needs Python 3.10 or later")]
    if v[:2] > TESTED_PYTHON[1]:
        return [Finding("warn", "python", f"{text}: not tested (3.10 to 3.13)",
                        "install Python 3.12, the version of the Windows machines")]
    return [Finding("ok", "python", text)]


def _extras(cfg: Config) -> list[Finding]:
    out = []
    steps, sources = _steps(cfg), cfg["sources"]
    code = sources.get("code")
    if "code" in steps and _enabled(code):
        for lang in code.get("languages", ["ada"]):
            if lang == "ada" and not (_has("tree_sitter") and _has("tree_sitter_ada")):
                out.append(Finding("fail", "extras", "tree_sitter_ada is not installed: the Ada code is not read",
                                   "pip install atlaslm[code] (from the wheelhouse)"))
    if "graph" in steps and not _has("networkx"):
        out.append(Finding("fail", "extras", "networkx is not installed: the graph step fails",
                           "pip install atlaslm[code]"))
    if ("docs" in steps or "index" in steps) and any(_enabled(d) for d in sources.get("docs", [])):
        suffixes = {f.suffix.lower() for d in sources.get("docs", []) if _enabled(d)
                    for p in d.get("paths", []) for f in _doc_files(Path(p), _doc_patterns(d))}
        if ".pdf" in suffixes and not _has("pymupdf") and not _has("fitz"):
            out.append(Finding("fail" if not _has("markitdown") else "warn", "extras",
                               "pymupdf is not installed: " + ("the PDFs are not read" if not _has("markitdown")
                                                               else "the PDFs are read with markitdown, much slower"),
                               "pip install atlaslm[docs]"))
        office = suffixes & {".docx", ".pptx", ".xlsx", ".html"}
        if office and not _has("markitdown"):
            out.append(Finding("fail", "extras", f"markitdown is not installed: the {', '.join(sorted(office))} "
                                                 "files are not read", "pip install atlaslm[docs]"))
    if cfg["mcp"]["enabled"] and not _has("mcp"):
        out.append(Finding("warn", "extras", "mcp is not installed: atlas serve does not start",
                           "pip install atlaslm[mcp]"))
    agents = cfg["agents"]
    wants_agents = (agents.get("model") or "eval" in agents or any(isinstance(v, dict) for v in agents.values())
                    or any(_relative(cfg, d).is_dir() for d in agents.get("dirs", [])))
    if not _has("google.adk"):
        out.append(Finding("warn" if wants_agents else "ok", "extras",
                           "google.adk is not installed: atlas agent, develop, verify, comply, link and the "
                           "agents of atlas serve do not work",
                           "pip install atlaslm[agents]" if wants_agents else ""))
    ca = [s.get("ca_cert", "") for s in (cfg.get("network") or {}, (cfg.get("llm") or {}).get("network") or {},
                                         (cfg.get("embeddings") or {}).get("network") or {})]
    if "system" in (c.lower() for c in ca) and not _has("truststore"):
        out.append(Finding("fail", "extras", 'ca_cert = "system" needs truststore, which is not installed',
                           "pip install truststore (it comes with atlas)"))
    if not out:
        out.append(Finding("ok", "extras", "the extras that the configured steps and tools import are installed"))
    return out


def _relative(cfg: Config, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else cfg.path.parent / path


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def _doc_patterns(source: dict) -> list[str]:
    return source.get("patterns") or [f"*{ext}" for name in plugins.available("extractor")
                                      for ext in plugins.get("extractor", name).extensions]


def _doc_files(p: Path, patterns: list[str]) -> list[Path]:
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted({f for pat in patterns for f in p.rglob(pat) if f.is_file()})
    return []


def _docs(cfg: Config) -> list[Finding]:
    out = []
    if "docs" not in _steps(cfg):
        return out
    for d in cfg["sources"].get("docs", []):
        if not _enabled(d):
            continue
        where = f"sources.docs '{d['name']}'"
        patterns = _doc_patterns(d)
        for root in d["paths"]:
            p = Path(root)
            if not p.exists():
                out.append(Finding("fail", where, f"{root} does not exist",
                                   f"check the path (it is relative to where atlas runs: {Path.cwd()})"))
                continue
            n = len(_doc_files(p, patterns))
            out.append(Finding("ok" if n else "warn", where, f"{root}: {_files(n)} ({', '.join(patterns)})",
                               "" if n else "no file matches patterns: check them"))
    return out


def _code(cfg: Config) -> list[Finding]:
    sc = cfg["sources"].get("code")
    if not _enabled(sc) or "code" not in _steps(cfg):
        return []
    extensions = set()
    for lang in sc.get("languages", ["ada"]):
        try:
            extensions.update(plugins.get("code_analyzer", lang, cfg["plugins"]["disabled"]).extensions)
        except LookupError as e:
            return [Finding("fail", "sources.code", str(e), "pip install atlaslm[code], or fix languages")]
    out, exclude = [], sc.get("exclude", [])
    if not sc.get("roots"):
        return [Finding("warn", "sources.code", "roots is empty: no code is read", "set [sources.code] roots")]
    for root in sc["roots"]:
        p = Path(root)
        if not p.is_dir():
            out.append(Finding("fail", "sources.code", f"{root} does not exist",
                               f"check roots (relative to where atlas runs: {Path.cwd()})"))
            continue
        n = sum(1 for f in p.rglob("*") if f.is_file() and f.suffix.lower() in extensions
                and not any(fnmatch.fnmatch(str(f).replace("\\", "/"), pat) for pat in exclude))
        out.append(Finding("ok" if n else "warn", "sources.code",
                           f"{root}: {_files(n)} ({', '.join(sorted(extensions))})",
                           "" if n else "no source file there: check roots, languages and exclude"))
    return out


def _doors(cfg: Config) -> list[Finding]:
    sd = cfg["sources"].get("doors")
    if not _enabled(sd) or "doors" not in _steps(cfg):
        return []
    adapter = sd.get("adapter", "database")
    if adapter == "exchange":
        from atlas.doors.exchange import ExchangeError, open_exchange
        path = (sd.get("exchange") or {}).get("path", "")
        where = "sources.doors.exchange"
        if not path:
            return [Finding("fail", where, "path is missing", "set [sources.doors.exchange] path")]
        try:
            conn = open_exchange(path)
        except (FileNotFoundError, ExchangeError, sqlite3.Error) as e:
            return [Finding("fail", where, str(e), "fill it with tools/doors_export, or fix the path")]
        try:
            meta = dict(conn.execute("SELECT key, value FROM exchange_meta").fetchall())
            modules = {r["module"]: r["filled_at"] for r in conn.execute("SELECT module, filled_at FROM exchange_modules")}
            counts = dict(conn.execute("SELECT module, COUNT(*) FROM exchange_requirements GROUP BY module").fetchall())
        finally:
            conn.close()
        last = max(modules.values(), default=meta.get("filled_at", "?"))
        out = [Finding("ok" if modules else "warn", where,
                       f"{path}: schema {meta.get('schema_version')}, {len(modules)} modules, "
                       f"{sum(counts.values())} objects, filled by {meta.get('filler', '?')} on {last}",
                       "" if modules else "the database has no module: run tools/doors_export")]
        missing = sorted(set(sd.get("modules") or []) - set(modules))
        if missing:
            out.append(Finding("warn", where, f"modules not in the database: {', '.join(missing)}",
                               "export them, or fix [sources.doors] modules"))
        return out
    if adapter == "database":
        db = sd.get("database") or {}
        driver = db.get("driver", "sqlite")
        if driver == "sqlite":
            path = Path(db.get("path", "")).expanduser()
            return [Finding("ok" if path.is_file() else "fail", "sources.doors.database",
                            f"{path} " + ("exists" if path.is_file() else "does not exist"),
                            "" if path.is_file() else "fix [sources.doors.database] path")]
        if driver == "odbc":
            return [Finding("ok" if _has("pyodbc") else "fail", "sources.doors.database",
                            "odbc driver: pyodbc " + ("installed" if _has("pyodbc") else "not installed"),
                            "" if _has("pyodbc") else "pip install pyodbc")]
    return [Finding("ok", "sources.doors", f"adapter {adapter} (not checked)")]


# --------------------------------------------------------------------------
# Commands, environment, workspace, connection
# --------------------------------------------------------------------------

def _commands(cfg: Config) -> list[Finding]:
    sc = cfg["sources"].get("code") or {}
    out, folders = [], []
    for pattern in sc.get("tool_path", []):        # expanded like BuildTools
        found = sorted(glob.glob(os.path.abspath(os.path.expanduser(pattern))), reverse=True)
        out.append(Finding("ok" if found else "warn", "sources.code tool_path",
                           f"{pattern}: " + (", ".join(found[:3]) if found else "no folder matches"),
                           "" if found else "fix the pattern, or install the toolchain"))
        folders += found
    search = os.pathsep.join([*folders, os.environ.get("PATH", "")])
    commands = [(key, sc.get(key) or []) for key in ("check_command", "test_command", "program_command")]
    for key in ("check_commands", "test_commands", "syntax_commands"):
        commands += [(f"{key}.{ext}", command or []) for ext, command in (sc.get(key) or {}).items()]
    server = sc.get("language_server") or []
    if server:
        found = sorted(glob.glob(os.path.expanduser(server[0])), reverse=True)    # as atlas.code.lsp
        commands.append(("language_server", [found[0] if found else os.path.expanduser(server[0])]))
    for key, command in commands:
        if not command or "{" in command[0]:
            continue
        where = shutil.which(command[0], path=search)
        out.append(Finding("ok" if where else "fail", f"sources.code {key}",
                           f"{command[0]}: " + (where or "not found on the PATH nor in tool_path"),
                           "" if where else "install it, or add its folder to [sources.code] tool_path"))
    return out


def _environment(cfg: Config) -> list[Finding]:
    if cfg.unset:
        return [Finding("warn", "environment", unset_hint(cfg), "set them before running atlas")]
    return [Finding("ok", "environment", "every ${VAR} of the file without a default is set")]


def _workspace(cfg: Config) -> list[Finding]:
    ws = cfg.workspace
    if ws.is_dir():
        if not os.access(ws, os.W_OK):
            return [Finding("fail", "workspace", f"{ws} is not writable", "change [project] workspace")]
        db = ws / "atlas.db"
        return [Finding("ok", "workspace", f"{ws}" + (f" (atlas.db of {db.stat().st_size // 1024} KB)"
                                                      if db.exists() else " (empty: atlas run fills it)"))]
    parent = next((p for p in ws.parents if p.exists()), None)
    if parent is None or not os.access(parent, os.W_OK):
        return [Finding("fail", "workspace", f"{ws} cannot be created ({parent} is not writable)",
                        "change [project] workspace")]
    return [Finding("ok", "workspace", f"{ws} does not exist yet: atlas run creates it")]


def _connection(cfg: Config) -> list[Finding]:
    from atlas.llm import check_service, from_config, mask_credentials
    conn, out = from_config(cfg), []
    for name, service in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        try:
            res = check_service(name, service)
        except SystemExit as e:             # the TLS setup gives up: a ca_cert that does not exist...
            res = {"ok": False, "detail": str(e.code)}
        out.append(Finding("ok" if res["ok"] else "fail", f"connection {name.lower()}",
                           f"{service.model} at {mask_credentials(service.base_url)}: {res['detail']}",
                           "" if res["ok"] else "atlas check gives the details"))
        if res.get("warning"):
            out.append(Finding("warn", f"connection {name.lower()}", res["warning"]))
    return out


def diagnose(cfg: Config, connect: bool = True) -> list[Finding]:
    """Everything that can be checked of `cfg` on this machine without changing anything.
    With `connect`, the last checks are one request to the chat model and one to the
    embeddings."""
    plugins.load_plugins(cfg["plugins"]["modules"])
    out: list[Finding] = []
    for part in (_python, _extras, _docs, _code, _doors, _commands, _environment, _workspace):
        out += part(cfg)
    if connect:
        out += _connection(cfg)
    return out


def print_findings(findings: list[Finding], file=None) -> None:
    file = file or sys.stdout
    width = max((len(f.where) for f in findings), default=0)
    for f in findings:
        print(f"{f.level.upper():4s}  {f.where:{width}s}  {f.message}", file=file)
        if f.fix and f.level != "ok":
            print(f"{'':4s}  {'':{width}s}  -> {f.fix}", file=file)
    counts = {level: sum(f.level == level for f in findings) for level in LEVELS}
    plural = lambda n, word: f"{n} {word}" + ("" if n == 1 else "s")
    print(f"\n{counts['ok']} ok, {plural(counts['warn'], 'warning')}, {plural(counts['fail'], 'failure')}", file=file)
