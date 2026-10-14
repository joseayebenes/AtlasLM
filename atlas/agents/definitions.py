"""Agents defined in Markdown files, as the subagents of Claude Code.

Each file is an agent: a YAML header and, as its body, the instructions of the agent.

    ---
    name: assistant
    description: Answers questions about the project, citing its sources.
    tools: [search, get_requirement, trace, read_source]
    model: gemma4:12b            # optional: [agents] model, or else [llm] model
    max_steps: 8                 # optional: most calls to the model in one answer
    reasoning_effort: none       # optional: "none" turns reasoning off; else [llm] reasoning_effort
    temperature: 0.1             # optional: else [llm] temperature
    top_p: 0.95                  # optional: else the server default
    top_k: 64                    # optional: else the server default (sent in extra_body)
    keep_tool_results: 10        # optional: large tool results kept whole, the rest a note; 0 = all
    stall_steps: 15              # optional: tool calls without changing the code that end a run; 0 = none
    ---
    You answer questions about an engineering project...

The agents that come with atlas are in atlas/agents/builtin/. Those of the project are in
the folders of [agents] dirs (["agents"] by default, next to atlas.toml): a file with the
name of a built-in one replaces it. [agents.<name>] in atlas.toml retouches one field of an
agent (tools, model, max_steps, reasoning_effort, temperature, top_p, top_k, keep_tool_results,
stall_steps, description). The name is the `name` of the header, or the name of the file.

This module only reads the files: it does not need ADK.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from atlas.config import ConfigError

__all__ = ["AgentDef", "BUILTIN", "load_definitions", "parse_definition"]

log = logging.getLogger(__name__)

BUILTIN = Path(__file__).parent / "builtin"
HEADER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class AgentDef:
    name: str
    instruction: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str = ""                 # "" = [agents] model, or else [llm] model
    max_steps: int = 8
    reasoning_effort: str = ""      # "" = [llm] reasoning_effort
    temperature: float | None = None
    top_p: float | None = None      # None = the server default
    top_k: int | None = None        # None = the server default; not in the OpenAI API: goes in extra_body
    keep_tool_results: int = 0      # the large tool results kept whole, the older ones a note; 0 = all
    stall_steps: int = 0            # tool calls without changing the code (or passing a test) that end a run
    source: str = ""                # the file it comes from


def parse_definition(text: str, source: str = "") -> AgentDef:
    """An AgentDef from the text of a Markdown file with a YAML header."""
    import yaml
    m = HEADER.match(text.lstrip("﻿"))
    if not m:
        raise ConfigError(f"{source}: an agent file starts with a YAML header between --- lines")
    try:
        header = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{source}: the YAML header is not valid: {e}") from None
    if not isinstance(header, dict):
        raise ConfigError(f"{source}: the YAML header must be a mapping (key: value)")
    name = str(header.pop("name", "") or Path(source).stem)
    known = {f.name for f in fields(AgentDef)} - {"name", "instruction", "source"}
    unknown = sorted(set(header) - known)
    if unknown:
        raise ConfigError(f"{source}: unknown keys {unknown} (valid: name, {', '.join(sorted(known))})")
    if isinstance(header.get("tools"), str):                   # "tools: search, trace" is also a list
        header["tools"] = [t.strip() for t in header["tools"].split(",") if t.strip()]
    definition = AgentDef(name=name, instruction=m.group(2).strip(), source=source, **header)
    return _checked(definition, source)


def _checked(d: AgentDef, where: str) -> AgentDef:
    if not NAME.match(d.name):
        raise ConfigError(f"{where}: agent name '{d.name}' must be lower case letters, digits and _")
    if not isinstance(d.tools, list) or not all(isinstance(t, str) for t in d.tools):
        raise ConfigError(f"{where}: tools must be a list of tool names")
    if not isinstance(d.max_steps, int) or d.max_steps < 1:
        raise ConfigError(f"{where}: max_steps must be a positive integer")
    if not d.instruction:
        raise ConfigError(f"{where}: the agent has no instructions (the body of the file)")
    if d.top_p is not None and (isinstance(d.top_p, bool) or not isinstance(d.top_p, (int, float))
                                or not 0 < d.top_p <= 1):
        raise ConfigError(f"{where}: top_p must be a number above 0 and at most 1")
    if d.top_k is not None and (isinstance(d.top_k, bool) or not isinstance(d.top_k, int) or d.top_k < 1):
        raise ConfigError(f"{where}: top_k must be a positive integer")
    if isinstance(d.keep_tool_results, bool) or not isinstance(d.keep_tool_results, int) or d.keep_tool_results < 0:
        raise ConfigError(f"{where}: keep_tool_results must be an integer, 0 or more")
    if isinstance(d.stall_steps, bool) or not isinstance(d.stall_steps, int) or d.stall_steps < 0:
        raise ConfigError(f"{where}: stall_steps must be an integer, 0 or more")
    d.reasoning_effort = str(d.reasoning_effort or "")
    return d


def _files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def load_definitions(cfg=None) -> dict[str, AgentDef]:
    """The agents: the built-in ones, those of [agents] dirs over them, and the retouches of
    [agents.<name>]. Without `cfg`, only the built-in ones."""
    found: dict[str, AgentDef] = {}
    folders = [BUILTIN]
    if cfg is not None:
        for d in cfg["agents"].get("dirs", []):
            folder = Path(d).expanduser()
            folders.append(folder if folder.is_absolute() else cfg.path.parent / folder)
    for folder in folders:
        seen: dict[str, Path] = {}
        for f in _files(folder):
            d = parse_definition(f.read_text(encoding="utf-8"), str(f))
            if d.name in seen:
                raise ConfigError(f"two agents named '{d.name}' in {folder}: {seen[d.name].name} and {f.name}")
            seen[d.name] = f
            if d.name in found:
                log.debug("agent %s of %s replaces the one of %s", d.name, f, found[d.name].source)
            found[d.name] = d
    if cfg is not None:
        for name, table in cfg["agents"].items():
            if not isinstance(table, dict) or name in ("eval",):
                continue
            if name not in found:
                raise ConfigError(f"[agents.{name}] retouches an agent that does not exist "
                                  f"(agents: {', '.join(sorted(found))})")
            found[name] = _checked(replace(found[name], **table), f"[agents.{name}]")
    return found
