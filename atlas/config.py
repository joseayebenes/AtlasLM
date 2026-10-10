"""Loading and validation of atlas.toml.

- A single TOML file; values accept ${VAR} and ${VAR:-default}.
- Each known section has a schema (key -> type). Unknown keys or wrong types are errors
  that name the section and the key.
- `fingerprint(section)` returns a stable hash of a section: the incremental engine adds
  it to the fingerprint of the units that depend on that section.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


# Schema: section -> {key: type | "any"}. "list" = list, "dict" = free nested table.
SCHEMA: dict[str, dict[str, Any]] = {
    "project": {"name": str, "workspace": str, "log_level": str},
    "llm": {"base_url": str, "model": str, "api_key": str, "temperature": float,
            "max_tokens": int, "reasoning_effort": str, "network": dict},
    "embeddings": {"base_url": str, "model": str, "api_key": str, "batch": int, "network": dict},
    "network": {"proxy": str, "ca_cert": str, "client_cert": str, "client_key": str,
                "no_proxy": str, "insecure": bool, "timeout": float, "requests_per_minute": int,
                "tokens_per_minute": int, "adaptive": bool, "max_concurrent": int,
                "max_retries": int, "max_backoff": float, "rate_limit_max_wait": float,
                "retry_statuses": list},
    "pipeline": {"workers": int, "on_error": str, "max_attempts": int, "retry_quarantined": bool,
                 "prune_missing": bool, "steps": list},
    "plugins": {"modules": list, "disabled": list},
    "sources": {"docs": list, "doors": dict, "code": dict},
    "graph": {"exports": list, "communities": bool},
    "index": {"chunk_chars": int, "chunk_overlap": int, "kinds": list},
    "agents": {"model": str, "dirs": list, "eval": dict},       # and [agents.<name>]: AGENT_SCHEMA
    "mcp": {"enabled": bool, "tools": list},
}

DOCS_SCHEMA = {"name": str, "paths": list, "patterns": list, "analysis": str, "req_regex": str,
               "structure": str, "heading_regex": str, "lang": str, "max_chars": int,
               "min_chars": int, "enabled": bool}
DOORS_SCHEMA = {"adapter": str, "enabled": bool, "database": dict, "exchange": dict, "convention": dict,
                "modules": list, "attributes": list}
AGENT_SCHEMA = {"description": str, "tools": list, "model": str, "max_steps": int, "reasoning_effort": str,
                "temperature": float}                             # what [agents.<name>] can retouch
CODE_SCHEMA = {"roots": list, "languages": list, "exclude": list, "req_tag": str, "req_regex": str,
               "test_patterns": list, "summaries": str, "enabled": bool}

DEFAULTS: dict[str, dict[str, Any]] = {
    "project": {"name": "atlas", "workspace": "./workspace", "log_level": "INFO"},
    "pipeline": {"workers": 1, "on_error": "continue", "max_attempts": 3,
                 "retry_quarantined": False, "prune_missing": True,
                 "steps": ["docs", "doors", "code", "graph", "index"]},
    "plugins": {"modules": [], "disabled": []},
    "graph": {"exports": ["graphml", "json"], "communities": False},
    "index": {"chunk_chars": 1200, "chunk_overlap": 200,
              "kinds": ["overview", "section_summary", "chunk", "requirement", "definition", "open_issue",
                        "code_doc"]},
    "agents": {"model": "", "dirs": ["agents"]},
    "mcp": {"enabled": True, "tools": []},
}


def _validate(data: dict, schema: dict, where: str) -> None:
    for k, v in data.items():
        if k not in schema:
            raise ConfigError(f"[{where}] unknown key: '{k}' (valid: {', '.join(schema)})")
        kind = schema[k]
        if kind is dict:
            if not isinstance(v, dict):
                raise ConfigError(f"[{where}] '{k}' must be a table")
        elif kind is list:
            if not isinstance(v, list):
                raise ConfigError(f"[{where}] '{k}' must be a list")
        elif kind is float:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ConfigError(f"[{where}] '{k}' must be a number")
        elif kind is int:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ConfigError(f"[{where}] '{k}' must be an integer")
        elif not isinstance(v, kind):
            raise ConfigError(f"[{where}] '{k}' must be of type {kind.__name__}")


@dataclass
class Config:
    """Validated configuration. Sections are read as dictionaries (`cfg["llm"]`) and each
    module builds its own object from its section."""

    path: Path
    data: dict = field(default_factory=dict)

    def __getitem__(self, section: str) -> Any:
        return self.data[section]

    def get(self, section: str, default: Any = None) -> Any:
        return self.data.get(section, default)

    @property
    def workspace(self) -> Path:
        ws = Path(self.data["project"]["workspace"])
        return ws if ws.is_absolute() else (self.path.parent / ws).resolve()

    def fingerprint(self, *sections: str) -> str:
        """Stable hash of one or more sections (e.g. 'sources.docs', 'embeddings')."""
        parts = []
        for s in sections:
            d: Any = self.data
            for piece in s.split("."):
                d = d.get(piece, None) if isinstance(d, dict) else None
            parts.append(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"configuration file {path} does not exist")
    try:
        with open(path, "rb") as f:
            data = _expand(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML syntax in {path}: {e}") from e

    for section in data:
        if section not in SCHEMA:
            raise ConfigError(f"unknown section [{section}] (valid: {', '.join(SCHEMA)})")
    agents = data.get("agents", {})
    data["agents"] = {k: v for k, v in agents.items() if k in SCHEMA["agents"] or not isinstance(v, dict)}
    for section, schema in SCHEMA.items():
        if section in data:
            _validate(data[section], schema, section)
    for name, table in agents.items():              # [agents.<name>]: retouches an agent
        if name not in SCHEMA["agents"] and isinstance(table, dict):
            _validate(table, AGENT_SCHEMA, f"agents.{name}")
            data["agents"][name] = table
    sources = data.get("sources", {})
    for i, d in enumerate(sources.get("docs", [])):
        _validate(d, DOCS_SCHEMA, f"sources.docs[{i}]")
        if "name" not in d or "paths" not in d:
            raise ConfigError(f"[sources.docs[{i}]] 'name' or 'paths' is missing")
    if "doors" in sources:
        _validate(sources["doors"], DOORS_SCHEMA, "sources.doors")
    if "code" in sources:
        _validate(sources["code"], CODE_SCHEMA, "sources.code")

    for section, defaults in DEFAULTS.items():
        data[section] = {**defaults, **data.get(section, {})}
    data.setdefault("sources", {})
    return Config(path=path, data=data)
