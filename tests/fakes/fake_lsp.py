"""A fake language server over stdio, for the tests of atlas.code.lsp: definition, references
and hover found by searching the words of the Ada files under the root, answered as an LSP
server answers them. Like the Ada Language Server, it asks the client for its configuration."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

root = Path(".")


def read() -> dict:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if not line.strip():
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


def send(message: dict) -> None:
    data = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()


def word_at(params: dict) -> str:
    path = Path(unquote(urlparse(params["textDocument"]["uri"]).path))
    pos = params["position"]
    line = path.read_text().split("\n")[pos["line"]]
    found = [m.group(0) for m in re.finditer(r"\w+", line) if m.start() <= pos["character"] <= m.end()]
    return found[0] if found else ""


def lines():
    for p in sorted(root.rglob("*")):
        if p.suffix in (".ads", ".adb"):
            for i, text in enumerate(p.read_text().split("\n")):
                yield p, i, text


def location(p: Path, i: int) -> dict:
    return {"uri": "file://" + quote(str(p)), "range": {"start": {"line": i, "character": 0},
                                                        "end": {"line": i, "character": 1}}}


while True:
    message = read()
    method, params = message.get("method"), message.get("params") or {}
    if method == "exit":
        sys.exit(0)
    if not method or "id" not in message:          # notifications, and the client's answers
        continue
    result = None
    if method == "initialize":
        root = Path(unquote(urlparse(params["rootUri"]).path))
        result = {"capabilities": {"definitionProvider": True, "hoverProvider": True, "referencesProvider": True}}
        send({"jsonrpc": "2.0", "id": 9999, "method": "workspace/configuration",
              "params": {"items": [{"section": "ada"}]}})
    elif method in ("textDocument/definition", "textDocument/hover"):
        word = word_at(params)
        found = next(((p, i, t) for p, i, t in lines() if re.search(rf"\b(procedure|function)\s+{word}\b", t, re.I)),
                     None)
        if found and method == "textDocument/definition":
            result = location(found[0], found[1])
        elif found:
            result = {"contents": {"kind": "markdown", "value": found[2].strip()}}
    elif method == "textDocument/references":
        word = word_at(params)
        result = [location(p, i) for p, i, t in lines() if re.search(rf"\b{word}\b", t, re.I)]
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
