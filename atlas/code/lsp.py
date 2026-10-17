"""Tools of a language server for coding agents: what the compiler knows of a name.

    find_definition   where a name used in a line is declared: file, line and that line
    find_references   where a name is used in the project
    describe_symbol   what a name is: its declaration (profile, type) and its comment

[sources.code] language_server is the command of the server, with globs: the Ada Language
Server of the Ada extension of VS Code, ["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"].
For Ada, atlas writes a GPR project in <workspace>/build with the folders of the code roots
that hold Ada sources (or uses language_server_project), and the server finds the runtime of
GNAT through the tool_path of the project. The model gives a line and a name; the column is
looked for in that line. A server starts on the first call and stays up while the process
runs: the first answer of the ALS takes about a second, the next ones milliseconds.

The same idea as Serena (replace_symbol_body, find_symbol over LSP) and as monitor-guided
decoding, which guides the model with the language server as it writes (+19-25 % of code that
compiles, at every model size): through an API that does not let atlas into the decoding, the
agent asks the server instead of guessing.
"""
from __future__ import annotations

import atexit
import glob
import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from atlas.plugins import register_methods

__all__ = ["LanguageServer", "LanguageTools", "LSP_TOOL_NAMES"]

LSP_TOOL_NAMES = ("find_definition", "find_references", "describe_symbol")
TIMEOUT = 60               # seconds for an answer of the server
MAX_REFERENCES = 30
MAX_HOVER = 1500           # characters of describe_symbol
ADA = (".ads", ".adb", ".ada")

_servers: dict[tuple, "LanguageServer"] = {}
_servers_lock = threading.Lock()


def _uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


class LanguageServer:
    """A language server over stdio (JSON-RPC with Content-Length headers): requests, the
    files it has open, and the requests of the server, answered with what it can use."""

    def __init__(self, command: list[str], root: Path, settings: dict | None = None, env: dict | None = None):
        self.command, self.root, self.settings = command, root, settings or {}
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.answers: dict[int, queue.Queue] = {}
        self.opened: dict[Path, tuple[int, str]] = {}
        self.next_id = 0
        self.lock = threading.RLock()

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.root, env=self.env)
        threading.Thread(target=self._read, daemon=True, name="atlas-lsp").start()
        self.request("initialize", {"processId": os.getpid(), "rootUri": _uri(self.root), "capabilities": {},
                                    "initializationOptions": self.settings})
        self.notify("initialized", {})
        if self.settings:
            self.notify("workspace/didChangeConfiguration", {"settings": self.settings})

    def _send(self, message: dict) -> None:
        data = json.dumps(message).encode("utf-8")
        with self.lock:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
            self.proc.stdin.flush()

    def _read(self) -> None:
        out = self.proc.stdout
        while True:
            length = None
            while True:                                  # the headers, up to a blank line
                line = out.readline()
                if not line:
                    return
                if not line.strip():
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length is None:
                continue
            message = json.loads(out.read(length))
            if "method" in message and "id" in message:  # a request of the server
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": self._answer(message)})
            elif "id" in message and message["id"] in self.answers:
                self.answers[message["id"]].put(message)

    def _answer(self, message: dict) -> Any:
        if message["method"] == "workspace/configuration":
            items = (message.get("params") or {}).get("items", [])
            return [self.settings.get(i.get("section", ""), None) if isinstance(i, dict) else None for i in items]
        return None

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float = TIMEOUT) -> Any:
        with self.lock:
            self.next_id += 1
            n = self.next_id
            self.answers[n] = queue.Queue()
        self._send({"jsonrpc": "2.0", "id": n, "method": method, "params": params})
        try:
            message = self.answers[n].get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"the language server did not answer {method} in {timeout:g} s") from None
        finally:
            self.answers.pop(n, None)
        if "error" in message:
            raise RuntimeError(f"the language server failed: {message['error'].get('message', message['error'])}")
        return message.get("result")

    def sync(self, path: Path, text: str) -> None:
        """The server sees `text` as the content of `path`: opened, or changed."""
        version, seen = self.opened.get(path, (0, None))
        if seen == text:
            return
        if version == 0:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": _uri(path), "languageId": "ada"
                                                 if path.suffix.lower() in ADA else path.suffix.lstrip("."),
                                                 "version": 1, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": _uri(path), "version": version + 1},
                                                   "contentChanges": [{"text": text}]})
        self.opened[path] = (version + 1, text)

    def close(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.request("shutdown", {}, timeout=5)
            self.notify("exit", {})
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


@atexit.register
def _close_all() -> None:
    for server in list(_servers.values()):
        server.close()


def _command(patterns: list[str]) -> list[str]:
    """The command of the server, its program found by glob (the last version first)."""
    if not patterns:
        return []
    found = sorted(glob.glob(os.path.expanduser(patterns[0])), reverse=True)
    return [found[0] if found else os.path.expanduser(patterns[0]), *patterns[1:]]


class LanguageTools:
    def __init__(self, build, command: list[str], project: str = ""):
        self.build = build                       # BuildTools: the files and their confinement, tool_path, build folder
        self.files = build.files
        self.command = _command(command)
        self.project = project

    @classmethod
    def from_config(cls, cfg, build=None) -> "LanguageTools":
        from atlas.code.build import BuildTools
        sc = cfg["sources"].get("code") or {}
        project = sc.get("language_server_project", "")
        return cls(build or BuildTools.from_config(cfg), sc.get("language_server", []),
                   str(cfg.resolve(project)) if project else "")

    @classmethod
    def for_context(cls, context: Any) -> "LanguageTools":
        tools = getattr(context, "_language_tools", None)
        if tools is None:
            from atlas.code.build import BuildTools
            tools = cls.from_config(context.cfg, BuildTools.for_context(context))
            context._language_tools = tools
        return tools

    # --- the server ---------------------------------------------------------------------------
    def _gpr(self) -> str:
        """A GPR project with the folders of the code roots that hold Ada sources."""
        if self.project:
            return self.project
        folders = sorted({p.parent for root in self.files.roots if root.is_dir() for p in root.rglob("*")
                          if p.suffix.lower() in ADA and not self.files._excluded(p)})
        self.build.build_dir.mkdir(parents=True, exist_ok=True)
        gpr = self.build.build_dir / "atlas_lsp.gpr"
        dirs = ", ".join(f'"{f.as_posix()}"' for f in folders)
        text = (f"project Atlas_Lsp is\n   for Source_Dirs use ({dirs});\n"
                f'   for Object_Dir use "{(self.build.build_dir / "lsp").as_posix()}";\nend Atlas_Lsp;\n')
        if not gpr.exists() or gpr.read_text(encoding="utf-8") != text:
            gpr.write_text(text, encoding="utf-8")
        return str(gpr)

    def server(self) -> LanguageServer:
        root = self.files.base
        key = (tuple(self.command), str(root))
        with _servers_lock:
            server = _servers.get(key)
            if server is None or server.proc is None or server.proc.poll() is not None:
                from atlas.code.build import on_path
                env = {**os.environ, "PATH": os.pathsep.join([*self.build.tool_path, os.environ.get("PATH", "")])}
                server = LanguageServer(on_path(self.command, env["PATH"]), root, {"ada": {"projectFile": self._gpr()}}, env)
                server.start()
                _servers[key] = server
        return server

    # --- the tools ----------------------------------------------------------------------------
    def _position(self, path: str, line: int, name: str) -> tuple[Path | None, dict]:
        """The file and the LSP position of `name` in `line` (1-based), or why not."""
        if not self.command:
            return None, {"error": "no language server configured ([sources.code] language_server)"}
        p, err = self.files._resolve(path)
        if err:
            return None, err
        if not p.is_file():
            return None, {"error": f"{path} does not exist"}
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if not 1 <= line <= len(lines):
            return None, {"error": f"{path} has {len(lines)} lines"}
        short = name.strip().split(".")[-1]
        m = re.search(rf"(?<![\w']){re.escape(short)}(?!\w)", lines[line - 1], re.IGNORECASE)
        if m is None:
            return None, {"error": f"{short} is not in line {line} of {path}: {lines[line - 1].strip()[:200]!r}. Give "
                                   "the line where the name is written"}
        try:
            self.server().sync(p, text)
        except (OSError, TimeoutError, RuntimeError) as e:
            return None, {"error": f"the language server ({self.command[0]}) did not start: {e}"}
        return p, {"textDocument": {"uri": _uri(p)}, "position": {"line": line - 1, "character": m.start() + 1}}

    def _place(self, location: dict) -> dict:
        """A location of the server as {file, line, text}: the file from the working folder when
        it is in the project, else its name (the runtime of the compiler)."""
        uri = location.get("uri") or location.get("targetUri", "")
        span = location.get("range") or location.get("targetSelectionRange") or {}
        line = span.get("start", {}).get("line", 0) + 1
        p = _path(uri)
        try:
            source = p.read_text(encoding="utf-8", errors="replace").split("\n")[line - 1].strip()
        except (OSError, IndexError):
            source = ""
        inside = next((r for r in self.files.roots if str(p).lower().startswith(str(r).lower() + os.sep)), None)
        shown = (self.files._show(inside / str(p)[len(str(inside)) + 1:]) if inside else p.name)
        return {"file": shown, "line": line, "text": source[:200]}

    def _ask(self, method: str, params: dict) -> Any:
        try:
            return self.server().request(method, params)
        except (OSError, TimeoutError, RuntimeError) as e:
            return {"error": str(e)}

    def find_definition(self, path: str, line: int, name: str) -> dict:
        """Where `name`, written in `line` of a file, is declared, as the compiler sees it:
        file, line and that line. Also for names of the runtime (Ada.Strings.Unbounded...).
        Use it instead of guessing which package a name comes from."""
        p, params = self._position(path, line, name)
        if p is None:
            return params
        found = self._ask("textDocument/definition", params)
        if isinstance(found, dict) and "error" in found:
            return found
        places = [self._place(l) for l in (found if isinstance(found, list) else [found] if found else [])]
        return {"name": name, "definitions": places} if places else {"error": f"the server knows no declaration of "
                                                                                f"{name} there"}

    def find_references(self, path: str, line: int, name: str) -> dict:
        """Where `name` (written in `line` of a file) is used in the project, with the
        declaration: each file, line and that line."""
        p, params = self._position(path, line, name)
        if p is None:
            return params
        found = self._ask("textDocument/references", {**params, "context": {"includeDeclaration": True}})
        if isinstance(found, dict) and "error" in found:
            return found
        places = [self._place(l) for l in (found or [])]
        return {"name": name, "count": len(places), "references": places[:MAX_REFERENCES],
                **({"truncated": True} if len(places) > MAX_REFERENCES else {})}

    def describe_symbol(self, path: str, line: int, name: str) -> dict:
        """What `name` (written in `line` of a file) is, as the compiler sees it: its
        declaration (the profile of a subprogram, the type of an object) and its comment."""
        p, params = self._position(path, line, name)
        if p is None:
            return params
        found = self._ask("textDocument/hover", params)
        if isinstance(found, dict) and "error" in found:
            return found
        contents = (found or {}).get("contents") if isinstance(found, dict) else None
        parts = contents if isinstance(contents, list) else [contents] if contents else []
        text = "\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in parts).strip()
        if not text:
            return {"error": f"the server says nothing of {name} there"}
        return {"name": name, "description": text if len(text) <= MAX_HOVER else text[:MAX_HOVER] + "…"}


register_methods(LanguageTools, LSP_TOOL_NAMES)
