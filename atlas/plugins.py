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

import functools
import importlib
import inspect
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = ["KINDS", "CodeAnalyzer", "CodeUnit", "DocumentExtractor", "Link", "Requirement", "RequirementsAdapter",
           "Tool", "available", "describe", "get", "load_plugins", "plugin", "register", "register_methods"]

log = logging.getLogger(__name__)

KINDS = ("code_analyzer", "requirements", "extractor", "tool")


# ----------------------------------------------------------------------------
# Interfaces
# ----------------------------------------------------------------------------

@dataclass
class CodeUnit:
    """Result of analysing a code file: the minimum the graph needs.

    A subprogram is {name, start_line, end_line, calls, doc, reqs} and, if the analyzer
    knows them, `kind` (procedure, function, method, task, entry, operator) and `signature`
    (its profile as written, on one line). A call is the name as written ("pkg.op") or
    {"name", "line", "column"}: where that name starts, both counted from 1, so that a
    language server can be asked what it is."""
    file: str
    unit: str                                   # compilation unit / module name
    language: str
    subprograms: list[dict] = field(default_factory=list)   # {name, kind, start_line, end_line, calls, doc, reqs, signature?}
    types: list[dict] = field(default_factory=list)
    variables: list[dict] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)        # withs / includes
    declarations: list[dict] = field(default_factory=list)  # specs / headers: {name, kind, line, doc, reqs}
    uses: list[str] = field(default_factory=list)           # Ada `use`: units looked into after the own one
    aliases: dict[str, str] = field(default_factory=dict)   # renamings and generic instances: name -> target

    @classmethod
    def from_dict(cls, data: dict) -> "CodeUnit":
        """A CodeUnit from its stored form, without the fields this version no longer has
        (`tests`, before 0.3)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def call_name(call: str | dict) -> str:
    """The name of a call of a CodeUnit, with or without its position."""
    return call if isinstance(call, str) else call["name"]


class CodeAnalyzer(ABC):
    """One analyzer per language. It must be deterministic and work file by file (the
    unit of the incremental engine)."""
    name: str = ""                # "ada", "cpp"...
    extensions: tuple[str, ...] = ()
    predefined: frozenset[str] = frozenset()   # names of the language that are never calls (Ada: Integer...)

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
    knows whether to read it again. `exported_at()` says when the module was exported from
    its source, if the source says it: atlas shows it next to everything that comes from it."""
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

    def exported_at(self, module: str) -> str:
        """When the module was exported from its source (ISO 8601); "" if it does not say."""
        return ""

    def module_info(self, module: str) -> dict:
        """{"exported_at", "source"}: when and from where the module was exported ("" if
        the source does not say)."""
        return {"exported_at": self.exported_at(module), "source": ""}


class DocumentExtractor(ABC):
    """Turns a document into text (with \\f between pages if it has them). When several
    enabled extractors handle an extension, the one with the highest `priority` is used.
    `license` is that of the third-party library that does the work, as `atlas plugins`
    shows it ("" if there is none)."""
    name: str = ""
    extensions: tuple[str, ...] = ()
    priority: int = 0
    license: str = ""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def extract(self, path: Path) -> str: ...

    def read(self, path: Path) -> tuple[str, dict]:
        """The text and what the extractor knows about the file: `pages`,
        `pages_without_text` (pages with images and no text: a scan) and `metadata` (of
        the PDF). An extractor that knows nothing of that only implements `extract`."""
        return self.extract(path), {}


class Tool(ABC):
    """Tool for ADK agents and for the MCP server. `run` takes keyword arguments and
    returns something compact and JSON-serialisable (use `truncated` if it cuts)."""
    name: str = ""
    description: str = ""
    writes: bool = False            # changes files or data (MCP clients may ask before calling it)

    def __init__(self, context: Any):
        self.context = context      # access to graph, index, requirements, code

    @abstractmethod
    def run(self, **kwargs) -> Any: ...

    def function(self) -> Callable[..., Any]:
        """The typed function behind the tool, from which ADK (FunctionTool) and the MCP
        server take the parameters and the description; `run` by default."""
        return self.run


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


def register_methods(owner: type, methods: Iterable[str], writes: Iterable[str] = ()) -> None:
    """Registers methods of `owner` as "tool" plugins with their own names. Every tool of a
    context shares the object `owner.for_context(context)` returns; the function of a tool
    is its bound method, whose typed signature and docstring ADK and the MCP server turn
    into the tool's schema and description."""
    writes = set(writes)
    for method in methods:
        class MethodTool(Tool):
            name = method
            description = inspect.cleandoc(getattr(owner, method).__doc__ or "")

            def __init__(self, context: Any):
                super().__init__(context)
                self.owner = owner.for_context(context)

            def function(self) -> Callable[..., Any]:
                return getattr(self.owner, self.name)

            def run(self, **kwargs) -> Any:
                return self.function()(**kwargs)

        MethodTool.writes = method in writes
        MethodTool.__name__ = MethodTool.__qualname__ = "".join(w.title() for w in method.split("_")) + "Tool"
        register("tool", method, MethodTool)


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
    built_in = ["atlas.code", "atlas.doors", "atlas.docs", "atlas.index", "atlas.graph", "atlas.agents.tools"]
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


PLAIN = (str, int, float, bool, list, dict, type(None))


def instrumented(function: Callable[..., Any], sink: Callable[[dict], None], name: str | None = None,
                 **fields: Any) -> Callable[..., Any]:
    """`function` wrapped so that each call goes to `sink` as an event: {"name", "args" (its
    arguments by name, only plain values: never a configuration or a context), "result",
    "error" (when it raised; the exception goes on), "seconds", "usage" (what
    atlas.llm.usage() grew during the call: requests, tokens, cost) and `fields`}. With
    functools.wraps, so that ADK and the MCP server still read its signature and its
    docstring. A sink that fails does not fail the call. The tools of the MCP server and of
    the agents, and the commands of the CLI, go through it (see atlas.usage)."""
    tool = name or function.__name__
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None

    def arguments(args: tuple, kwargs: dict) -> dict:
        try:
            bound = signature.bind_partial(*args, **kwargs).arguments if signature else kwargs
        except TypeError:
            bound = kwargs
        return {k: v for k, v in bound.items() if isinstance(v, PLAIN)}

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        from atlas.llm.connection import usage, usage_since
        before, t0 = usage(), time.perf_counter()
        result, error = None, None
        try:
            result = function(*args, **kwargs)
            return result
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            event = {"name": tool, "args": arguments(args, kwargs), "result": result, "error": error,
                     "seconds": round(time.perf_counter() - t0, 3), "usage": usage_since(before), **fields}
            try:
                sink(event)
            except Exception as e:                 # the log must never break the tool
                log.warning("the call to %s was not logged: %s", tool, e)
    return wrapper


def describe() -> dict[str, list[str]]:
    return {k: available(k) for k in KINDS}
