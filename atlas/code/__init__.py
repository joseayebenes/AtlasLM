"""Code analyzers. Each language is a `CodeAnalyzer` plugin registered under its name;
[sources.code] languages = ["ada"] picks the ones in use.

The Ada analyzer wraps the existing `ada_graph.py` (tree-sitter-ada), whose API is still
in Spanish (`analizar`, `subprogramas`, `tipos`) until it is migrated. To add another
language: a CodeAnalyzer subclass + `register("code_analyzer", "cpp", MyClass)`, in its
own module declared in [plugins] modules or by entry point.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from atlas.plugins import CodeAnalyzer, CodeUnit, register


class AdaAnalyzer(CodeAnalyzer):
    name = "ada"
    extensions = (".ads", ".adb", ".ada")

    def _legacy(self):
        try:
            import ada_graph  # the current script, installed separately or in legacy/
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "legacy"))
            import ada_graph
        return ada_graph

    def analyze_file(self, path: Path, source: bytes) -> CodeUnit:
        ag = self._legacy()
        req_pattern = re.compile(self.config.get("req_regex", r"(?i)\bReq\s*:\s*([^\n]+)"))
        info = ag.analizar(Path(path), req_pattern)
        return CodeUnit(
            file=str(path), unit=info["unit"], language="ada", imports=info["withs"],
            subprograms=[{"name": s["name"], "start_line": s["start_line"], "end_line": s["end_line"],
                          "calls": s["calls"], "doc": s["doc"], "reqs": s["reqs"]}
                         for s in info["subprogramas"]],
            types=[{"name": t["name"], "line": t["line"], "record": t["record"], "doc": t["doc"],
                    "reqs": t.get("reqs", [])} for t in info["tipos"] if not t["local"]],
            variables=[{"name": v["name"], "line": v["line"], "constant": v["constant"]}
                       for v in info["variables"]],
        )

    def version(self) -> str:
        return "ada_graph-1"


register("code_analyzer", "ada", AdaAnalyzer)
