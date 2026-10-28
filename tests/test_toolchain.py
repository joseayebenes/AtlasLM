"""The toolchain of the project as atlas knows it, without running it: library_spec,
search_reference and the language server, over a Toolchain (atlas/code/toolchain.py). With
GNAT and the Ada Language Server when they are installed."""
import glob
import os
import shutil
import sys
from pathlib import Path

import pytest

from atlas.code import Toolchain
from atlas.config import load_config

GNAT_PATH = ["~/.local/share/alire/toolchains/gnat_native_*/bin"]


def _gnat() -> bool:
    found = [p for pattern in GNAT_PATH for p in glob.glob(os.path.expanduser(pattern))]
    return bool(shutil.which("gnatmake", path=os.pathsep.join(found + [os.environ.get("PATH", "")])))


gnat = pytest.mark.skipif(not _gnat(), reason="GNAT is not installed (alr toolchain --select gnat_native gprbuild)")


@pytest.fixture
def project(tmp_path, toml, samples):
    """The Ada samples in a temporary folder, and the Toolchain of an atlas.toml over them."""
    ada = tmp_path / "ada"
    shutil.copytree(samples / "ada", ada)

    def make(tool_path=None, extra="") -> Toolchain:
        cfg = load_config(toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\ntool_path = {tool_path or []}\n{extra}\n'
                               .replace("'", '"')))
        return Toolchain.from_config(cfg)
    return ada, make


@gnat
def test_library_spec_reads_the_runtime_of_gnat(project):
    ada, make = project
    tools = make(extra='library = ["~/.local/share/alire/toolchains/gnat_native_*/lib/gcc/*/*/adainclude"]')
    sockets = tools.library_spec("GNAT.Sockets", name="Receive_Socket")["declarations"]
    assert sockets and all(d["name"] == "Receive_Socket" for d in sockets)
    split = tools.library_spec("GNAT.String_Split")                       # an instance: its generic's declarations
    assert split["instance_of"] == "GNAT.Array_Split" and any(d["name"] == "Create" for d in split["declarations"])
    assert "Length (Source : Unbounded_String) return Natural" in str(tools.library_spec("Ada.Strings.Unbounded", "Length"))


INFO = """This is gnat_rm.info, produced by makeinfo.

\x1f
File: gnat_rm.info,  Node: Pragma Assertion_Policy,  Next: Pragma Assume,  Up: Implementation Defined Pragmas

2.17 Pragma Assertion_Policy
============================

Syntax: pragma Assertion_Policy (CHECK | DISABLE | IGNORE);
This pragma controls the assertions that are checked.

* Pragma Assume::

\x1f
File: gnat_rm.info,  Node: Pragma Assume,  Next: Pragma Elaborate_All,  Up: Implementation Defined Pragmas

2.18 Pragma Assume
==================

Syntax: pragma Assume (Boolean_EXPRESSION);
\x1f
Tag Table:
Node: Top 100
"""


def test_search_reference_over_the_manuals_and_the_library(project, tmp_path, toml, capsys):
    pytest.importorskip("tree_sitter_ada")
    from atlas.cli import main
    ada, make = project
    lib = tmp_path / "adainclude"
    lib.mkdir()
    (lib / "a-strfix.ads").write_text("package Ada.Strings.Fixed is\n   function Trim (Source : String) return String;\n"
                                      "   --  Removes the leading and trailing blanks of Source.\nend Ada.Strings.Fixed;\n")
    (lib / "s-imgint.ads").write_text("package System.Img_Int is\n   procedure Image_Integer (V : Integer);\n"
                                      "   --  Removes nothing, internal.\nend System.Img_Int;\n")
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "gnat_rm.info").write_text(INFO)
    (docs / "style.md").write_text("# Naming\n\nPackages are nouns.\n\n# Blanks\n\nNo leading blanks in an image.\n")
    tools = make(extra=f'library = ["{lib.as_posix()}"]\nreference = ["{docs.as_posix()}"]')
    found = tools.search_reference("pragma Assume")["passages"]
    assert found[0]["source"] == "gnat_rm.info: Pragma Assume" and "Boolean_EXPRESSION" in found[0]["text"]
    blanks = [p["source"] for p in tools.search_reference("remove leading blanks")["passages"]]
    assert "Ada.Strings.Fixed.Trim" in blanks and "style.md: Blanks" in blanks
    assert not any(s.startswith("System.") for s in blanks)              # the internal units of GNAT are left out
    # again when a manual changes; a manual taken out of the configuration goes
    (docs / "style.md").write_text("# Blanks\n\nUse Ada.Strings.Fixed.Trim for an image without blanks.\n")
    os.utime(docs / "style.md", (1, 1))
    assert "Ada.Strings.Fixed.Trim for an image" in str(tools.search_reference("image without blanks"))
    assert set(make(extra=f'library = ["{lib.as_posix()}"]').reference.ensure()) == {"library"}
    assert "no manuals and no library" in make().search_reference("x")["error"]
    # atlas reference builds it from the command line
    cfg = toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\nreference = ["{docs.as_posix()}"]\n')
    with pytest.raises(SystemExit) as end:
        main(["reference", str(cfg)])
    assert end.value.code == 0 and "gnat_rm.info" in capsys.readouterr().out


# --- the language server ---------------------------------------------------------------------

def _line_with(path: Path, text: str) -> int:
    return next(i for i, l in enumerate(path.read_text().split("\n"), 1) if text in l)


def test_the_tools_of_a_language_server(project, tmp_path):
    from atlas.code import LanguageTools
    ada, make = project
    fake = Path(__file__).parent / "fakes" / "fake_lsp.py"
    tools = LanguageTools(make(), [sys.executable, str(fake)])
    tracks = ada / "src" / "tracks.adb"
    line = _line_with(tracks, "Logger.Log")
    found = tools.find_definition(str(tracks), line, "Logger.Log")            # qualified: the last part
    assert found["definitions"][0]["file"].endswith(("logger.ads", "logger.adb"))   # the fake takes the first
    assert "procedure Log" in found["definitions"][0]["text"]
    refs = tools.find_references(str(tracks), line, "Log")
    assert refs["count"] >= 2 and any(r["file"].endswith("tracks.adb") for r in refs["references"])
    assert tools.describe_symbol(str(tracks), line, "Log")["description"].startswith("procedure Log")
    assert "is not in line 1" in tools.find_definition(str(tracks), 1, "Log")["error"]
    assert "outside the code roots" in tools.find_definition("/etc/hosts", 1, "x")["error"]
    assert "no language server configured" in LanguageTools(make(), []).find_definition(str(tracks), line, "Log")["error"]


def _als() -> list[str]:
    return glob.glob(os.path.expanduser("~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"))


@pytest.mark.slow
@gnat
@pytest.mark.skipif(not _als(), reason="the Ada Language Server is not installed (the Ada extension of VS Code)")
def test_the_ada_language_server(project):
    from atlas.code import LanguageTools
    ada, make = project
    tools = LanguageTools(make(tool_path=GNAT_PATH), ["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"])
    tracks = ada / "src" / "tracks.adb"
    line = _line_with(tracks, "Logger.Log")
    found = tools.find_definition(str(tracks), line, "Log")
    assert found["definitions"][0]["file"].endswith("logger.ads"), found
    assert "Log" in tools.describe_symbol(str(tracks), line, "Log")["description"]
