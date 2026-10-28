"""The MCP server of hephesto: what an editor needs that atlas does not do, compile the code, run
its tests and judge a change. The editor edits; with `edit`, hephesto edits too.

    hephesto serve atlas.toml            check_code, run_test, run_tests and judge_changes (and
                                         run_program with [sources.code] program_command)
    hephesto serve atlas.toml --edit     also the file tools: list_files, grep, read_file, outline,
                                         read_subprogram and the ones that write

It is atlas's server (atlas.mcp.create_server) with these tools and their paragraphs of
instructions; `atlas serve` is the one that knows the project. In VS Code both go in
.vscode/mcp.json, as two servers.
"""
from __future__ import annotations

from typing import Any

from atlas.code.files import READ_TOOL_NAMES
from atlas.config import Config
from hephesto.build import BUILD_TOOL_NAMES
from hephesto.edit import WRITE_TOOLS
from hephesto.judge import JUDGE_TOOL_NAMES

__all__ = ["INSTRUCTIONS", "create_server", "serve", "tool_names"]

INSTRUCTIONS = [
    (WRITE_TOOLS, "edit_file, write_file, replace_subprogram and add_subprogram only change a file after a read_file "
                  "of it; check the diff they return. replace_subprogram takes a whole subprogram and needs nothing "
                  "copied."),
    (BUILD_TOOL_NAMES, "`check_code` compiles a file with the compiler of the project and returns its errors: use it "
                       "after every change; `run_test` builds a test procedure and runs it; `run_program` runs the "
                       "program with an input and returns its output."),
    (JUDGE_TOOL_NAMES, "`run_tests` runs the tests of a plan of hephesto develop and says how far each one gets, and "
                       "`judge_changes` says after a change what changed, whether it compiles and which tests got "
                       "further or went back: call it after each change, and undo what makes a test go back."),
]


def tool_names(cfg: Config, edit: bool = False) -> list[str]:
    """The tools of `hephesto serve`: the compiler, the tests and the judge (run_program only
    with a program to run), and with `edit` the file tools; those of [plugins] disabled, out."""
    from atlas.code.gpr import code_config
    names = ["check_code", "run_test", *(["run_program"] if code_config(cfg).get("program_command") else []),
             *JUDGE_TOOL_NAMES]
    if edit:
        names = [*READ_TOOL_NAMES, *WRITE_TOOLS, *names]
    return [n for n in names if f"tool:{n}" not in cfg["plugins"]["disabled"]]


def create_server(cfg: Config, context: Any = None, edit: bool = False) -> Any:
    from atlas.mcp import create_server as atlas_server
    return atlas_server(cfg, context, names=tool_names(cfg, edit), title="hephesto", extra=INSTRUCTIONS)


def serve(cfg: Config, context: Any = None, edit: bool = False) -> None:
    from atlas.mcp import serve as atlas_serve
    atlas_serve(cfg, context, names=tool_names(cfg, edit), title="hephesto", extra=INSTRUCTIONS)
