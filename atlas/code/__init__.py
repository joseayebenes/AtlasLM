"""Code analyzers. Each language is a `CodeAnalyzer` plugin registered under its name;
[sources.code] languages = ["ada"] picks the ones in use.

    ada.py      AdaAnalyzer (tree-sitter-ada), the analysis of legacy/ada_graph.py
    common.py   requirement tags in comments, shared by every language

The traceability tag is `req_tag` of [sources.code] ("Req:" by default), or `req_regex`
for full control (group 1 = the list of IDs). To add another language: a CodeAnalyzer
subclass + `register("code_analyzer", "cpp", MyClass)`, in its own module declared in
[plugins] modules or by entry point.
"""
from __future__ import annotations

from atlas.code.ada import AdaAnalyzer
from atlas.code.common import extract_requirements, requirement_pattern
from atlas.plugins import register

__all__ = ["AdaAnalyzer", "extract_requirements", "requirement_pattern"]

register("code_analyzer", "ada", AdaAnalyzer)
