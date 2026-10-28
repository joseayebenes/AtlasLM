"""hephesto — writes and runs the code of a project that atlas knows.

atlas (atlaslm) knows the project and reads its code; hephesto changes it: the file tools that
write, the compiler and the tests, the judge of a change and `hephesto develop`, the plans that
an agent carries out task by task with the tests as judge.

    edit.py        CodeTools: an atlas CodeReader that also edits files (edit_file, write_file,
                   replace_subprogram, add_subprogram)
    build.py       BuildTools: an atlas Toolchain that runs check_code, run_test and run_program
    judge.py       JudgeTools: run_tests and judge_changes, the judge of develop for an editor
    develop.py     the plans of hephesto develop
    hints.py       drafts of hints for the errors of the compiler that came without one
    procedures.py  the test procedures of DOORS as tests, step by step, without a model
    evaluate.py    the develop cases of an evaluation set (hephesto eval)

Importing hephesto registers its tools as "tool" plugins of atlas, for the agents of develop and
for `hephesto serve`; atlas never imports it, so `atlas serve` never offers them.

    from hephesto import develop
    develop("plan.toml", atlas.load("atlas.toml"))
"""
from hephesto.edit import TOOL_NAMES, WRITE_TOOLS, CodeTools          # registers the file tools that write
from hephesto.build import BUILD_TOOL_NAMES, BuildTools               # registers check_code, run_test, run_program
from hephesto.judge import JUDGE_TOOL_NAMES, JudgeTools               # registers run_tests and judge_changes

__all__ = ["BUILD_TOOL_NAMES", "BuildTools", "CodeTools", "JUDGE_TOOL_NAMES", "JudgeTools", "TOOL_NAMES",
           "WRITE_TOOLS", "__version__", "develop"]

__version__ = "0.3.0"


def develop(*args, **kwargs):
    """hephesto.develop.develop, imported on first use (it needs the [agents] extra)."""
    from hephesto.develop import develop as run
    return run(*args, **kwargs)
