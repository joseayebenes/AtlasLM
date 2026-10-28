"""The toolchain of the project as atlas knows it, without running it: where its tools are, its
build folder, its library and its manuals.

    library_spec      the public declarations of a library unit: of the runtime of the compiler
                      ([sources.code] library: GNAT's adainclude) or of the project (library.py)
    search_reference  the manuals of the language and of the compiler ([sources.code]
                      reference) and the declarations of the library, by words (reference.py)

`tool_path` ([sources.code]) goes before PATH for the tools of the project (globs allowed; a
relative folder is relative to atlas.toml, like the code roots): the language server of lsp.py
here, the compiler and the tests in hephesto (hephesto.build.BuildTools, a Toolchain that also
runs them).

    toolchain = Toolchain.from_config(cfg)
    toolchain.library_spec("Ada.Command_Line")
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from atlas.code.files import CodeReader, rebased
from atlas.code.library import Library
from atlas.code.reference import Reference
from atlas.plugins import register_methods

__all__ = ["TOOLCHAIN_TOOL_NAMES", "Toolchain", "on_path", "toolchain_settings"]

TOOLCHAIN_TOOL_NAMES = ("library_spec", "search_reference")


def toolchain_settings(cfg, base: str | Path | None = None) -> dict:
    """The arguments of a Toolchain over [sources.code], but the files. With `base`, a copy of the
    working folder (a parallel attempt of hephesto develop): its own build folder, and the folders
    of tool_path in the copy (the scripts of the project build what is in it); the manuals and
    the library are shared."""
    from atlas.code.gpr import code_config
    sc = code_config(cfg)
    project = sc.get("project", "")
    if base is not None and project:
        project = rebased(project, base, cfg.home)
    return {"build_dir": cfg.workspace / "build" if base is None else Path(base) / ".atlas" / "build",
            "tool_path": [str(cfg.resolve(p)) if base is None else rebased(p, base, cfg.home)
                          for p in sc.get("tool_path", [])],
            "library": [str(cfg.resolve(p)) for p in sc.get("library", [])],
            "reference": [str(cfg.resolve(p)) for p in sc.get("reference", [])],
            "reference_db": cfg.workspace / "reference.db", "project": project}


class Toolchain:
    def __init__(self, files: CodeReader, build_dir: str | Path, tool_path: Iterable[str] = (),
                 library: Iterable[str] = (), reference: Iterable[str] = (), reference_db: str | Path | None = None,
                 project: str = ""):
        self.files = files                    # the confinement of the file tools: same roots and exclusions
        self.library = Library(library, files.roots)
        self.reference = Reference(reference_db or Path(build_dir).parent / "reference.db", reference, self.library)
        self.build_dir = Path(build_dir)
        self.tool_path = [p for pattern in tool_path            # the commands run in build_dir: absolute paths
                          for p in sorted(glob.glob(os.path.abspath(os.path.expanduser(pattern))), reverse=True)]
        self.project = project                # {project}: the GPR of [sources.code] project

    @classmethod
    def from_config(cls, cfg, files: CodeReader | None = None, base: str | Path | None = None) -> "Toolchain":
        """Over [sources.code] (see toolchain_settings)."""
        return cls(files or CodeReader.from_config(cfg, base=base), **toolchain_settings(cfg, base))

    @classmethod
    def for_context(cls, context: Any) -> "Toolchain":
        """One instance per context, over its CodeReader; the BuildTools of hephesto, if the
        context has one, is that toolchain."""
        tools = getattr(context, "_toolchain", None) or getattr(context, "_build_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg, CodeReader.for_context(context))
            context._toolchain = tools
        return tools

    def library_spec(self, unit: str, name: str = "") -> dict:
        """The public declarations of a library unit: a package of the runtime of the
        compiler (Ada.Command_Line, Ada.Strings.Unbounded, GNAT.Sockets...) or a spec of the
        project: its subprograms with their parameters, its types, constants and exceptions,
        with their comment. `name` keeps those whose name contains it. Look here before using
        a subprogram you have not used in this project: do not guess names or parameters."""
        try:
            return self.library.spec(unit, name, shown=self._shown)
        except ImportError as e:
            return {"error": f"library_spec needs tree-sitter-ada: {e}"}

    def search_reference(self, query: str, k: int = 5) -> dict:
        """Searches by words the manuals of the language and of the compiler (the reference
        manual and the user's guide of GNAT) and the declarations of the library: which
        package has a subprogram for something ("split a string by a separator"), what a
        pragma, an attribute or a switch of the compiler does. Returns `k` passages, each with
        where it comes from (a manual and its section, or Unit.Name), the best first."""
        if not (self.reference.patterns or self.library.folders):
            return {"error": "no manuals and no library to search: [sources.code] reference and library"}
        try:
            return {"passages": self.reference.search(query, k)}
        except ImportError as e:
            return {"error": f"search_reference needs tree-sitter-ada for the library: {e}"}

    def _shown(self, p: Path) -> str:
        """A spec as an answer shows it: as read_file takes it if it is in the code roots, else
        its name (the runtime of the compiler cannot be read with read_file)."""
        return self.files._show(p) if self.files._resolve(str(p))[1] is None else p.name


def on_path(args: list[str], path: str) -> list[str]:
    """`args` with its program looked up in `path` (tool_path, then PATH). Windows looks for it in
    the PATH of atlas, not in the one given to the process, and would not see tool_path; there
    `which` also finds the .exe, .cmd or .bat of a name."""
    if os.name != "nt" or not args or os.path.dirname(args[0]):
        return args
    return [shutil.which(args[0], path=path) or args[0], *args[1:]]


register_methods(Toolchain, TOOLCHAIN_TOOL_NAMES)
