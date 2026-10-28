"""Code analyzers. Each language is a `CodeAnalyzer` plugin registered under its name;
[sources.code] languages = ["ada"] picks the ones in use.

    ada.py      AdaAnalyzer (tree-sitter-ada): units, subprograms, types, calls and their tags
    python.py   PythonAnalyzer (ast, no dependencies): functions, classes and their tags
    common.py   requirement tags in comments, shared by every language
    files.py    CodeReader: list, search and read the files of the code roots
    toolchain.py  Toolchain: tool_path, the build folder, library_spec and search_reference
    tools.py    CodeTools: a CodeReader that also edits files, for coding agents
    build.py    BuildTools: a Toolchain that runs check_code and run_test, the compiler and the tests
    judge.py    JudgeTools: run_tests and judge_changes, the judge of atlas develop for an editor
    lsp.py      LanguageTools: find_definition, find_references and describe_symbol, from a
                language server (the Ada Language Server)
    gpr.py      read_project: the source and object folders of a GPR project ([sources.code]
                project), for the roots, exclude and the gprbuild commands

The traceability tag is `req_tag` of [sources.code] ("Req:" by default), or `req_regex`
for full control (group 1 = the list of IDs). To add another language: a CodeAnalyzer
subclass + `register("code_analyzer", "cpp", MyClass)`, in its own module declared in
[plugins] modules or by entry point.
"""
from __future__ import annotations

from atlas.code.ada import AdaAnalyzer
from atlas.code.python import PythonAnalyzer
from atlas.code.common import extract_requirements, requirement_ids, requirement_pattern
from atlas.code.files import READ_TOOL_NAMES, CodeReader   # registers the reading tools
from atlas.code.toolchain import TOOLCHAIN_TOOL_NAMES, Toolchain   # registers library_spec and search_reference
from atlas.code.tools import TOOL_NAMES, CodeTools          # registers the file tools that write
from atlas.code.build import BUILD_TOOL_NAMES, BuildTools   # registers check_code and run_test
from atlas.code.judge import JUDGE_TOOL_NAMES, JudgeTools   # registers run_tests and judge_changes
from atlas.code.lsp import LSP_TOOL_NAMES, LanguageTools     # registers the tools of the language server
from atlas.plugins import register

__all__ = ["AdaAnalyzer", "BUILD_TOOL_NAMES", "BuildTools", "CodeReader", "CodeTools", "JUDGE_TOOL_NAMES", "JudgeTools",
           "LSP_TOOL_NAMES", "LanguageTools", "PythonAnalyzer", "READ_TOOL_NAMES", "TOOLCHAIN_TOOL_NAMES", "TOOL_NAMES",
           "Toolchain", "extract_requirements", "requirement_ids", "requirement_pattern"]

register("code_analyzer", "ada", AdaAnalyzer)
register("code_analyzer", "python", PythonAnalyzer)
