"""The MCP servers of a project in VS Code: .vscode/mcp.json, next to atlas.toml.

    atlas init --vscode [atlas.toml]       adds (or updates) the server "atlas"
    hephesto init --vscode [atlas.toml]    the same with "hephesto", beside it

Each server runs with the Python that runs this command (`python -m atlas serve atlas.toml`), so
it works the same from a virtual environment as from a wheelhouse installation, without the
editor knowing where the `atlas` command is; `cwd` is the folder of the project
(${workspaceFolder}), where VS Code opens .vscode. The other servers of the file and the rest of
it are kept as they are. A file that is not plain JSON (VS Code also takes comments) is not
touched: the answer has the entry to add by hand.

    from atlas.vscode import add_server
    add_server(Path("atlas.toml"), "atlas")
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["add_server", "server_entry"]


def server_entry(config: Path, module: str, args: tuple[str, ...] = ()) -> dict:
    """The entry of `python -m <module> serve <atlas.toml> [args]` for the folder of the TOML."""
    return {"type": "stdio", "command": sys.executable, "args": ["-m", module, "serve", config.name, *args],
            "cwd": "${workspaceFolder}"}


def add_server(config: Path, name: str, module: str = "", args: tuple[str, ...] = ()) -> tuple[Path, str]:
    """Adds the server `name` (`module`, by default the same) to the .vscode/mcp.json of the
    folder of `config`. (the file, what was done: "created", "added", "updated", "unchanged");
    a ValueError, with the entry to add by hand, when the file cannot be read as JSON."""
    config = config.resolve()
    path = config.parent / ".vscode" / "mcp.json"
    entry = server_entry(config, module or name, args)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            snippet = json.dumps({name: entry}, indent=2)
            raise ValueError(f"{path} is not plain JSON ({e}); add this to its \"servers\" by hand:\n{snippet}") from None
        if not isinstance(data, dict) or not isinstance(data.get("servers", {}), dict):
            raise ValueError(f"{path}: \"servers\" is not an object; add the server {name} by hand")
    servers = data.setdefault("servers", {})
    done = "created" if not path.exists() else "unchanged" if servers.get(name) == entry else \
        "updated" if name in servers else "added"
    if done != "unchanged":
        servers[name] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, done
