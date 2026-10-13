"""Code analyzers. Each language is a `CodeAnalyzer` plugin registered under its name;
[sources.code] languages = ["ada"] picks the ones in use.

    ada.py      AdaAnalyzer (tree-sitter-ada), the analysis of legacy/ada_graph.py
    python.py   PythonAnalyzer (ast, no dependencies): functions, classes and their tags
    common.py   requirement tags in comments, shared by every language
    tools.py    CodeTools: list, search, read and edit files, for coding agents
    build.py    BuildTools: check_code and run_test, the compiler and the tests of the project

The traceability tag is `req_tag` of [sources.code] ("Req:" by default), or `req_regex`
for full control (group 1 = the list of IDs). To add another language: a CodeAnalyzer
subclass + `register("code_analyzer", "cpp", MyClass)`, in its own module declared in
[plugins] modules or by entry point.
"""
from __future__ import annotations

from atlas.code.ada import AdaAnalyzer
from atlas.code.python import PythonAnalyzer
from atlas.code.common import extract_requirements, requirement_pattern
from atlas.code.tools import TOOL_NAMES, CodeTools          # registers the file tools
from atlas.code.build import BUILD_TOOL_NAMES, BuildTools   # registers check_code and run_test
from atlas.plugins import register

__all__ = ["AdaAnalyzer", "BUILD_TOOL_NAMES", "BuildTools", "CodeTools", "PythonAnalyzer", "TOOL_NAMES",
           "extract_requirements", "requirement_pattern"]

register("code_analyzer", "ada", AdaAnalyzer)
register("code_analyzer", "python", PythonAnalyzer)
