"""Plugin registry: everything that can be added or removed goes through here.

Plugin kinds (base class -> registry name):
    CodeAnalyzer          "code_analyzer"      a programming language
    RequirementsAdapter   "requirements"       a way to read requirements (DB, ReqIF, CSV, MCP)
    DocumentExtractor     "extractor"          a way to get text out of a document (PDF, DOCX...)
    Tool                  "tool"               a tool for ADK agents and for the MCP server

How they are registered:
    1. Built-in ones: each atlas module calls `register(...)` when imported.
    2. External ones via entry point: in the pyproject of the external package,
         [project.entry-points."atlas.plugins"]  my_plugin = "my_package.my_module"
       and that module registers what it offers when imported.
    3. Via configuration: [plugins] modules = ["my_package.my_module"] (same idea, no install).
    4. In Python: atlas.plugins.register("code_analyzer", "cpp", MyAnalyzer).

How they are removed: [plugins] disabled = ["code_analyzer:cpp"], or by not enabling them
in the source configuration (e.g. languages = ["ada"] only uses the "ada" analyzer).
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

KINDS = ("code_analyzer", "requirements", "extractor", "tool")


# ----------------------------------------------------------------------------
# Interfaces
# ----------------------------------------------------------------------------

@dataclass
class CodeUnit:
    """Result of analysing a code file: the minimum the graph needs."""
    file: str
    unit: str                                   # compilation unit / module name
    language: str
    subprograms: list[dict] = field(default_factory=list)   # {name, start_line, end_line, calls, doc, reqs}
    types: list[dict] = field(default_factory=list)
    variables: list[dict] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)        # withs / includes
    tests: list[dict] = field(default_factory=list)         # {name, covers:[...], reqs:[...]}
    declarations: list[dict] = field(default_factory=list)  # specs / headers: {name, line, doc, reqs}


class CodeAnalyzer(ABC):
    """One analyzer per language. It must be deterministic and work file by file (the
    unit of the incremental engine)."""
    name: str = ""                # "ada", "cpp"...
    extensions: tuple[str, ...] = ()

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def analyze_file(self, path: Path, source: bytes) -> CodeUnit: ...

    def version(self) -> str:
        """Changing it invalidates the cached results of this analyzer."""
        return "1"


@dataclass
class Requirement:
    id: str                       # already normalised according to the configured convention
    text: str
    module: str
    attributes: dict = field(default_factory=dict)
    parent: str | None = None     # hierarchy inside the module (parent object), if any
    raw_id: str = ""              # as it came from the source


@dataclass
class Link:
    source: str
    target: str
    type: str                     # satisfies, derives_from, refines, verifies...


class RequirementsAdapter(ABC):
    """Reads requirements and links from a source. `snapshot()` returns something whose
    hash changes when the source changes (e.g. export date or table hash), so the engine
    knows whether to read it again."""
    name: str = ""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def modules(self) -> list[str]: ...

    @abstractmethod
    def snapshot(self, module: str) -> str: ...

    @abstractmethod
    def requirements(self, module: str) -> Iterable[Requirement]: ...

    @abstractmethod
    def links(self, module: str) -> Iterable[Link]: ...


class DocumentExtractor(ABC):
    """Turns a document into text (with \\f between pages if it has them)."""
    name: str = ""
    extensions: tuple[str, ...] = ()

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def extract(self, path: Path) -> str: ...


class Tool(ABC):
    """Tool for ADK agents and for the MCP server. `run` takes keyword arguments and
    returns something compact and JSON-serialisable (use `truncated` if it cuts)."""
    name: str = ""
    description: str = ""

    def __init__(self, context: Any):
        self.context = context      # access to graph, index, requirements, code

    @abstractmethod
    def run(self, **kwargs) -> Any: ...


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------

_REGISTRY: dict[str, dict[str, type]] = {k: {} for k in KINDS}
_LOADED = False


def register(kind: str, name: str, cls: type) -> type:
    if kind not in KINDS:
        raise ValueError(f"unknown plugin kind '{kind}' (valid: {', '.join(KINDS)})")
    if name in _REGISTRY[kind] and _REGISTRY[kind][name] is not cls:
        log.warning("plugin %s:%s redefined by %s", kind, name, cls)
    _REGISTRY[kind][name] = cls
    return cls


def plugin(kind: str, name: str) -> Callable[[type], type]:
    """Decorator: @plugin("code_analyzer", "ada")"""
    return lambda cls: register(kind, name, cls)


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY[kind])


def get(kind: str, name: str, disabled: Iterable[str] = ()) -> type:
    if f"{kind}:{name}" in set(disabled):
        raise LookupError(f"plugin {kind}:{name} is disabled in [plugins] disabled")
    try:
        return _REGISTRY[kind][name]
    except KeyError:
        raise LookupError(f"no {kind} plugin named '{name}'; available: "
                          f"{', '.join(available(kind)) or 'none'}") from None


def load_plugins(modules: Iterable[str] = (), use_entry_points: bool = True) -> list[str]:
    """Imports the built-in modules, those declared by entry point and those in the
    configuration. Returns the names loaded."""
    global _LOADED
    loaded: list[str] = []
    built_in = ["atlas.code", "atlas.doors", "atlas.docs"]
    for m in built_in:
        try:
            importlib.import_module(m)
            loaded.append(m)
        except ImportError as e:               # optional dependency missing: carry on
            log.info("built-in plugin %s not available: %s", m, e)
    if use_entry_points:
        for ep in entry_points(group="atlas.plugins"):
            try:
                ep.load()
                loaded.append(f"entry-point:{ep.name}")
            except Exception as e:
                log.warning("could not load plugin %s: %s", ep.name, e)
    for m in modules:
        importlib.import_module(m)              # a failure here is an error: the user asked for it
        loaded.append(m)
    _LOADED = True
    return loaded


def describe() -> dict[str, list[str]]:
    return {k: available(k) for k in KINDS}
