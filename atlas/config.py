"""Loading and validation of atlas.toml.

- A single TOML file; values accept ${VAR} and ${VAR:-default}.
- Each known section has a schema (key -> type). Unknown keys or wrong types are errors
  that name the section and the key.
- `fingerprint(section)` returns a stable hash of a section: the incremental engine adds
  it to the fingerprint of the units that depend on that section. Secrets do not count:
  changing a password reprocesses nothing, and no hash of it is stored.
- Secrets come from the environment: an API key for a server that is not local, or a
  password in a proxy URL or in an ODBC DSN, written as is in the file is an error.
- `Config.unset` names the ${VAR} that expanded to nothing, because this process does not
  have them: a program that starts atlas (an MCP client) may not pass the terminal's.
- Every path of the file is relative to the folder of the file (`Config.resolve`), wherever
  atlas runs from; the engine names the files of the sources from there (`Config.relative`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_URL_PASSWORD = re.compile(r"(://[^/@:]*:)([^/@]*)(@)")             # http://user:password@proxy
_DSN_PASSWORD = re.compile(r"(?i)(\b(?:pwd|password)\s*=\s*)([^;]*)")  # ODBC: PWD=...; Password=...
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


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


def unset_variables(v: Any) -> list[str]:
    """The ${VAR} of `v` (the TOML before expanding) that expand to nothing: not set, or
    empty, in the environment, and without a default value."""
    if isinstance(v, str):
        return [m.group(1) for m in _VAR.finditer(v) if not os.environ.get(m.group(1)) and not m.group(2)]
    if isinstance(v, (dict, list)):
        names = [n for x in (v.values() if isinstance(v, dict) else v) for n in unset_variables(x)]
        return list(dict.fromkeys(names))
    return []


def unset_hint(cfg: "Config") -> str:
    """Which ${VAR} of the configuration this process lacks, and where to give them; "" if
    none."""
    if not getattr(cfg, "unset", None):
        return ""
    return (f"{', '.join(cfg.unset)} of {cfg.path.name} not set in the environment of atlas. A program "
            "that starts atlas, such as an MCP client, gives it its own environment, not that of the "
            "terminal: pass them in its configuration (env; environment in OpenCode)")


# Schema: section -> {key: type | "any"}. "list" = list, "dict" = free nested table.
SCHEMA: dict[str, dict[str, Any]] = {
    "project": {"name": str, "workspace": str, "log_level": str},
    "llm": {"base_url": str, "model": str, "api_key": str, "temperature": float,
            "max_tokens": int, "reasoning_effort": str, "extra_body": dict, "reasoning_field": str,
            "network": dict},
    "embeddings": {"base_url": str, "model": str, "api_key": str, "batch": int, "network": dict},
    "network": {"proxy": str, "ca_cert": str, "client_cert": str, "client_key": str,
                "no_proxy": str, "insecure": bool, "timeout": float, "requests_per_minute": int,
                "tokens_per_minute": int, "adaptive": bool, "max_concurrent": int,
                "max_retries": int, "max_backoff": float, "rate_limit_max_wait": float,
                "retry_statuses": list},
    "pipeline": {"workers": int, "step_workers": dict, "on_error": str, "max_attempts": int,
                 "retry_quarantined": bool, "prune_missing": bool, "steps": list},
    "plugins": {"modules": list, "disabled": list},
    "sources": {"docs": list, "doors": dict, "code": dict},
    "graph": {"exports": list, "communities": bool},
    "index": {"chunk_chars": int, "chunk_overlap": int, "kinds": list},
    "search": {"results": int, "per_document": int, "context_chars": int, "scope": str, "scope_chars": int},
    "agents": {"model": str, "dirs": list, "eval": dict},       # and [agents.<name>]: AGENT_SCHEMA
    "mcp": {"enabled": bool, "tools": list, "profile": str},
    "tests": {"languages": dict, "steps": list},                # atlas tests (atlas/code/procedures.py)
}

SEARCH_SCOPES = ("auto", "passages", "whole")     # [search] scope: what ask reads (atlas.index.search)
MCP_PROFILES = ("editor", "trace", "all")         # [mcp] profile: the tools of atlas serve (atlas.mcp)

DOCS_SCHEMA = {"name": str, "paths": list, "patterns": list, "analysis": str, "req_regex": str,
               "structure": str, "heading_regex": str, "lang": str, "max_chars": int,
               "min_chars": int, "enabled": bool}
DOORS_SCHEMA = {"adapter": str, "enabled": bool, "database": dict, "exchange": dict, "convention": dict,
                "modules": list, "attributes": list}
AGENT_SCHEMA = {"description": str, "tools": list, "model": str, "max_steps": int, "reasoning_effort": str,
                "temperature": float, "top_p": float, "top_k": int,
                "keep_tool_results": int, "stall_steps": int}     # what [agents.<name>] can retouch
CODE_SCHEMA = {"roots": list, "languages": list, "exclude": list, "req_tag": str, "req_regex": str,
               "test_patterns": list, "summaries": str, "enabled": bool, "check_command": list,
               "test_command": list, "tool_path": list, "command_timeout": int, "check_timeout": int,
               "test_timeout": int, "program_timeout": int, "run_command": list,
               "test_commands": dict, "check_commands": dict, "program_command": list, "protected": list,
               "syntax_commands": dict, "check_on_edit": bool, "hints": list, "library": list,
               "reference": list, "language_server": list, "language_server_project": str}

DEFAULTS: dict[str, dict[str, Any]] = {
    "project": {"name": "atlas", "workspace": "./workspace", "log_level": "INFO"},
    "pipeline": {"workers": 1, "step_workers": {}, "on_error": "continue", "max_attempts": 3,
                 "retry_quarantined": False, "prune_missing": True,
                 "steps": ["docs", "doors", "code", "graph", "index"]},
    "plugins": {"modules": [], "disabled": []},
    "graph": {"exports": ["graphml", "json"], "communities": False},
    "index": {"chunk_chars": 1200, "chunk_overlap": 200,
              "kinds": ["overview", "section_summary", "chunk", "requirement", "definition", "open_issue",
                        "code_doc"]},
    # what search and ask take from the index; changing it reprocesses nothing
    "search": {"results": 8, "per_document": 3, "context_chars": 14000, "scope": "auto", "scope_chars": 100000},
    "agents": {"model": "", "dirs": ["agents"]},
    "mcp": {"enabled": True, "tools": [], "profile": ""},     # "": editor with [sources.code], else all
    "tests": {"languages": {}, "steps": []},
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


def _validate_tests(tests: dict) -> None:
    """[tests]: each language with its path and template; each step with its match, a valid
    regular expression, and the code of some of those languages."""
    languages = tests.get("languages", {})
    for name, spec in languages.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"[tests.languages.{name}] must be a table with path and template")
        _validate(spec, {"path": str, "template": str}, f"tests.languages.{name}")
        if "path" not in spec or "template" not in spec:
            raise ConfigError(f"[tests.languages.{name}] needs path and template")
    for i, step in enumerate(tests.get("steps", [])):
        where = f"tests.steps[{i}]"
        if not isinstance(step, dict) or not isinstance(step.get("match"), str):
            raise ConfigError(f"[{where}] needs match, a regular expression")
        try:
            re.compile(step["match"])
        except re.error as e:
            raise ConfigError(f"[{where}] match is not a valid regular expression: {e}") from None
        unknown = sorted(k for k in step if k != "match" and k not in languages)
        if unknown:
            raise ConfigError(f"[{where}] {unknown} are not languages of [tests.languages] "
                              f"({', '.join(languages) or 'none'})")
        if not all(isinstance(v, str) for v in step.values()):
            raise ConfigError(f"[{where}] the code of each language must be a string")


def _validate_search(search: dict) -> None:
    if search.get("scope", "auto") not in SEARCH_SCOPES:
        raise ConfigError(f"[search] scope must be one of {', '.join(SEARCH_SCOPES)}")
    for key, least in (("results", 1), ("per_document", 0), ("context_chars", 1000), ("scope_chars", 1000)):
        if key in search and search[key] < least:
            raise ConfigError(f"[search] '{key}' must be at least {least}")


def _validate_mcp(mcp: dict) -> None:
    if mcp.get("profile", "") not in ("", *MCP_PROFILES):
        raise ConfigError(f"[mcp] profile must be one of {', '.join(MCP_PROFILES)}")


@dataclass
class Config:
    """Validated configuration. Sections are read as dictionaries (`cfg["llm"]`) and each
    module builds its own object from its section."""

    path: Path
    data: dict = field(default_factory=dict)
    unset: list[str] = field(default_factory=list)      # ${VAR} of the file this process lacks

    def __getitem__(self, section: str) -> Any:
        return self.data[section]

    def get(self, section: str, default: Any = None) -> Any:
        return self.data.get(section, default)

    @property
    def workspace(self) -> Path:
        ws = self.resolve(self.data["project"]["workspace"])
        return ws if Path(self.data["project"]["workspace"]).expanduser().is_absolute() else ws.resolve()

    @property
    def home(self) -> Path:
        """The folder of atlas.toml: the relative paths of the configuration start here."""
        return self.path.parent

    def resolve(self, path: str | Path) -> Path:
        """A path of the configuration (a file, a folder or a glob pattern): an absolute one
        as it is, a relative one from the folder of atlas.toml, whatever folder atlas runs in."""
        p = Path(path).expanduser()
        return p if p.is_absolute() else Path(os.path.normpath(self.home / p))

    def relative(self, path: str | Path) -> str:
        """How the engine names a file of the sources (units, code files of the graph):
        relative to the folder of atlas.toml if it is inside it, absolute otherwise. The
        same name from any folder, and the one of the versions that ran from the folder of
        atlas.toml, so a workspace does not reprocess everything."""
        p = Path(os.path.abspath(self.resolve(path)))
        return os.path.relpath(p, self.home) if p.is_relative_to(self.home) else str(p)

    def fingerprint(self, *sections: str) -> str:
        """Stable hash of one or more sections (e.g. 'sources.docs', 'embeddings'), without
        their secrets (see redact)."""
        parts = []
        for s in sections:
            d: Any = self.data
            for piece in s.split("."):
                d = d.get(piece, None) if isinstance(d, dict) else None
            parts.append(json.dumps(redact(d), sort_keys=True, ensure_ascii=False, default=str))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def redact(value: Any) -> Any:
    """A copy without secrets: api_key, client_key and the passwords of URLs and DSNs."""
    if isinstance(value, dict):
        return {k: "***" if k in ("api_key", "client_key") and v else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _DSN_PASSWORD.sub(r"\1***", _URL_PASSWORD.sub(r"\1***\3", value))
    return value


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def literal_secrets(raw: dict) -> list[str]:
    """Where the file (`raw`: the TOML before expanding ${VAR}) writes a secret as is: an
    api_key of [llm] or [embeddings] for a server that is not local, a password in a proxy
    URL, or one in the DSN of [sources.doors.database]."""
    def literal(v: Any) -> bool:
        return isinstance(v, str) and bool(v.strip()) and "${" not in v

    found = []
    llm, emb = raw.get("llm") or {}, raw.get("embeddings") or {}
    llm_url = _expand(llm.get("base_url", "")) or "http://localhost"
    for name, section, url in (("llm", llm, llm_url), ("embeddings", emb, _expand(emb.get("base_url", "")) or llm_url)):
        if literal(section.get("api_key")) and not _is_local(url):
            found.append(f"[{name}] api_key")
    for name, net in (("network", raw.get("network")), ("llm.network", llm.get("network")),
                      ("embeddings.network", emb.get("network"))):
        m = _URL_PASSWORD.search((net or {}).get("proxy", "") if isinstance(net, dict) else "")
        if m and literal(m.group(2)):
            found.append(f"[{name}] proxy (its password)")
    dsn = (((raw.get("sources") or {}).get("doors") or {}).get("database") or {}).get("dsn", "")
    m = _DSN_PASSWORD.search(dsn if isinstance(dsn, str) else "")
    if m and literal(m.group(2)):
        found.append("[sources.doors.database] dsn (its password)")
    return found


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"configuration file {path} does not exist")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML syntax in {path}: {e}") from e
    data = _expand(raw)

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
    _validate_tests(data.get("tests", {}))
    _validate_search(data.get("search", {}))
    _validate_mcp(data.get("mcp", {}))
    secrets = literal_secrets(raw)
    if secrets:
        raise ConfigError(f"secrets are read from the environment, not written in {path.name}: "
                          f"{', '.join(secrets)}. Write them as ${{VARIABLE}} (e.g. api_key = \"${{ATLAS_API_KEY}}\") "
                          f"and set the variable; a server without a key needs no api_key")

    for section, defaults in DEFAULTS.items():
        data[section] = {**defaults, **data.get(section, {})}
    data.setdefault("sources", {})
    return Config(path=path, data=data, unset=unset_variables(raw))
