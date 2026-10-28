from pathlib import Path

import pytest

from atlas.code import AdaAnalyzer, extract_requirements, requirement_pattern
from atlas.plugins import CodeUnit, call_name

pytest.importorskip("tree_sitter_ada")

SOURCE = b"""with Ada.Text_IO;
with Store;
package body Radar is
   -------------------------------------------
   -- Smooths the plots of a scan.
   -- REQ : RDR-0010, RDR_0011 also see the ICD
   -------------------------------------------
   procedure Smooth (Plots : in out Plot_Array; N : Integer) is
      Window : Float_Array (1 .. 5);
      function Weight (I : Integer) return Float is
      begin
         return Kernel (I);           -- a call of the nested function, not of Smooth
      end Weight;
   begin
      Window (1) := Weight (N);       -- Window (1) is indexing: a local array
      Plots (N) := Store.Last;        -- Plots (N) is indexing: a parameter
      Ada.Text_IO.Put_Line ("done");  -- Req: RDR-0012
   end Smooth;
end Radar;
"""


def test_requirement_tags():
    assert extract_requirements("-- Req: A-B, C-D deletes messages", requirement_pattern({})) == ["A-B", "C-D"]
    assert extract_requirements("-- @req SYS-1\n-- @req SYS-2", requirement_pattern({"req_tag": "@req"})) == \
        ["SYS-1", "SYS-2"]
    assert extract_requirements("-- [[TRACE SRS-9]]", requirement_pattern({"req_regex": r"\[\[TRACE ([^\]]+)\]\]"})) == \
        ["SRS-9"]
    assert extract_requirements("-- Req: none here", requirement_pattern({})) == []


def test_requirement_ids_of_the_convention():
    """The probe of the analysis of 2026-10: without the expression of an ID, a tag is read
    token by token and loses what follows a word or a slash."""
    import re
    from atlas.doors import Canonical, id_regex, id_shapes, normalize_id
    tag = requirement_pattern({})
    assert extract_requirements("-- Req: SRS-0004 and SRS-0005", tag) == ["SRS-0004"]       # as before
    ids = re.compile(id_regex({"id_pattern": "^(?:SRS|SSS)-\\d{4}$"}))   # id_pattern without anchors
    assert extract_requirements("-- Req: SRS-0004 and SRS-0005", tag, ids) == ["SRS-0004", "SRS-0005"]
    assert extract_requirements("-- Req: see SRS-0004", tag, ids) == ["SRS-0004"]
    assert extract_requirements("-- Req: SRS-0004 / SRS-0005", tag, ids) == ["SRS-0004", "SRS-0005"]
    assert extract_requirements("-- Req: srs-0004, SRS-00045, XSRS-0006", tag, ids) == ["srs-0004"]
    assert extract_requirements("-- SRS-0007 without a tag", tag, ids) == []
    assert id_regex({"id_regex": r"\bR\d+\b", "id_pattern": "^X$"}) == r"\bR\d+\b" and id_regex({}) == ""
    assert re.compile(id_regex({"id_pattern": "(?i)^srs-\\d+$"})).findall("Req: SRS-1, srs-2") == ["SRS-1", "srs-2"]

    # "SRS 0004" and "srs-4": a looser id_regex finds them and id_canonical makes them SRS-0004
    loose = re.compile(r"(?i)\b(?:SRS|SSS)[-_ ]?\d+\b")
    canonical = Canonical({"regex": r"^(SRS|SSS)[-_ ]?0*(\d+)$", "format": "{1}-{2:04d}"})
    found = extract_requirements("-- Req: SRS 0004, srs-4, SSS_12", tag, loose)
    assert {normalize_id(r) for r in found} == {"SRS0004", "SRS-4", "SSS_12"}       # three nodes before
    assert {normalize_id(r, "upper", canonical) for r in found} == {"SRS-0004", "SSS-0012"}
    assert normalize_id("SRS - 0004", "upper", canonical) == "SRS-0004"
    assert normalize_id("srs-4", "none", canonical) == "srs-4" and normalize_id("TBD", "upper", canonical) == "TBD"
    for bad in ({"regex": "^(SRS)$"}, {"regex": "^(SRS)$", "format": "{2}"}, {"regex": "(", "format": "x"},
                {"regex": "x", "format": "x", "case": "upper"}):
        with pytest.raises(ValueError, match="id_canonical"):
            Canonical(bad)
    assert id_shapes(["SRS-0004", "srs-4", "SRS 0004", "SRS-0005", "TBD"]) == [
        {"prefix": "SRS", "separator": "-", "digits": 4, "count": 2, "example": "SRS-0004"},
        {"prefix": "SRS", "separator": " ", "digits": 4, "count": 1, "example": "SRS 0004"},
        {"prefix": "other", "separator": "", "digits": 0, "count": 1, "example": "TBD"},
        {"prefix": "srs", "separator": "-", "digits": 1, "count": 1, "example": "srs-4"}]

    # the analyzers take it in their configuration (code.parse puts it there)
    src = b"package body P is\n   -- Req: SRS-0004 and SRS-0005\n   procedure Run is\n   begin\n      null;" \
          b"  -- Req: see SRS-0006 / SRS-0007\n   end Run;\nend P;\n"
    run = AdaAnalyzer({"id_regex": ids.pattern}).analyze_file(Path("p.adb"), src).subprograms[0]
    assert run["reqs"] == ["SRS-0004", "SRS-0005", "SRS-0006", "SRS-0007"]
    assert AdaAnalyzer({}).analyze_file(Path("p.adb"), src).subprograms[0]["reqs"] == ["SRS-0004"]
    from atlas.code import PythonAnalyzer
    py = b"def run():\n    # Req: SRS-0004 and SRS-0005\n    pass\n"
    assert PythonAnalyzer({"id_regex": ids.pattern}).analyze_file(Path("p.py"), py).subprograms[0]["reqs"] == \
        ["SRS-0004", "SRS-0005"]


def test_ada_bodies_calls_comments_and_requirements(tmp_path):
    f = tmp_path / "radar-filters.adb"
    unit = AdaAnalyzer({}).analyze_file(f, SOURCE)
    assert unit.unit == "radar.filters" and unit.imports == ["ada.text_io", "store"]
    subs = {s["name"]: s for s in unit.subprograms}
    smooth = subs["smooth"]
    assert smooth["calls"] == [{"name": "weight", "line": 15, "column": 21},           # not window, plots or kernel
                               {"name": "ada.text_io.put_line", "line": 17, "column": 7}]
    assert smooth["doc"] == "Smooths the plots of a scan.\nREQ : RDR-0010, RDR_0011 also see the ICD"
    assert smooth["reqs"] == ["RDR-0010", "RDR-0012", "RDR_0011"]
    assert subs["weight"]["calls"] == [{"name": "kernel", "line": 12, "column": 17}]
    assert (smooth["start_line"], smooth["end_line"]) == (8, 18)
    assert smooth["kind"] == "procedure" and subs["weight"]["kind"] == "function"
    assert smooth["signature"] == "procedure Smooth (Plots : in out Plot_Array; N : Integer)"
    assert subs["weight"]["signature"] == "function Weight (I : Integer) return Float"
    assert AdaAnalyzer.predefined >= {"integer", "float", "string"}


def test_the_column_of_a_call_counts_characters_and_old_units_still_load(tmp_path):
    src = "package body P is\n   procedure Go is\n   begin\n      Ñu (1); Q.Run;\n   end Go;\nend P;\n"
    unit = AdaAnalyzer({}).analyze_file(tmp_path / "p.adb", src.encode("utf-8"))
    assert unit.subprograms[0]["calls"] == [{"name": "ñu", "line": 4, "column": 7},
                                            {"name": "q.run", "line": 4, "column": 15}]   # Ñ is two bytes
    stored = {"file": "p.adb", "unit": "p", "language": "ada", "subprograms": [{"name": "go", "calls": ["q.run"]}],
              "tests": []}                                   # as 0.2 stored it: calls without place, and tests
    old = CodeUnit.from_dict(stored)
    assert [call_name(c) for c in old.subprograms[0]["calls"]] == ["q.run"] and not hasattr(old, "tests")


# The probe of the analysis of 2026-10 (section 6.2): what ada-1 lost or mixed up
PROBE = b"""package body Probe is
   type Count is range 0 .. 9;
   -- The main loop.
   -- Req: SRS-0010
   task body Main_Loop is
      Ticks : Count := 0;
   begin
      loop
         Run (1);                       -- Req: SRS-0011
         Put_Line (Count'Image (Ticks));
      end loop;
   end Main_Loop;
   protected body Lock is
      -- Req: SRS-0012
      entry Seize when not Busy is
      begin
         Busy := True;
         Notify;
      end Seize;
      procedure Release is
      begin
         Busy := False;
      end Release;
   end Lock;
   function "+" (A, B : Vector) return Vector is
   begin
      return Add (A, B);                -- Req: SRS-0013
   end "+";
   -- Req: SRS-0014
   procedure Run (N : Integer) is
      T : Count := 3;
   begin
      Put_Line (T'Image);
      Put_Line (Count'Image (T));
      T'Write (Stream, T);
      Log (N);
   end Run;
   procedure Run is
   begin
      Reset;                            -- Req: SRS-0015
   end Run;
end Probe;
"""


def test_ada_tasks_entries_protected_bodies_operators_and_attributes():
    unit = AdaAnalyzer({}).analyze_file(Path("probe.adb"), PROBE)
    subs = [(s["name"], s["kind"], s["start_line"], s["end_line"], sorted({call_name(c) for c in s["calls"]}), s["reqs"])
            for s in unit.subprograms]
    assert subs == [
        ("main_loop", "task", 5, 12, ["put_line", "run"], ["SRS-0010", "SRS-0011"]),     # not count: an attribute
        ("lock", "protected", 13, 24, [], []),                   # the tags of its entries are theirs
        ("seize", "entry", 15, 19, ["notify"], ["SRS-0012"]),
        ("release", "procedure", 20, 23, [], []),
        ('"+"', "operator", 25, 28, ["add"], ["SRS-0013"]),
        ("run", "procedure", 30, 37, ["log", "put_line"], ["SRS-0014"]),   # not t (T'Image, T'Write) nor count
        ("run", "procedure", 38, 41, ["reset"], ["SRS-0015"])]
    assert unit.variables == []                                   # Ticks is local to the task
    spec = AdaAnalyzer({}).analyze_file(Path("probe.ads"), b'package Probe is\n   -- Adds.\n'
                                        b'   function "+" (A, B : Vector) return Vector;\n   procedure Run;\nend Probe;\n')
    assert [(d["name"], d["kind"], d["doc"]) for d in spec.declarations] == [('"+"', "operator", "Adds."),
                                                                            ("run", "procedure", "")]
    assert AdaAnalyzer({}).version() == "ada-4"


def test_ada_use_clauses_and_aliases():
    src = b"""with Ada.Text_IO; use Ada.Text_IO;
package body P is
   package TIO renames Ada.Text_IO;
   use type Store.Count;
   use all type Store.Kind;
   use Store, Other.Pkg;
   procedure Q (X : Integer) renames Store.Put;
   function "-" (A, B : T) return T renames Sub;
   package Int_Stack is new Stacks (Integer);
   procedure P2 is new Gen_P;
   procedure Run is
      use Inner;
   begin
      Q (1);
   end Run;
end P;
"""
    unit = AdaAnalyzer({}).analyze_file(Path("p.adb"), src)
    assert unit.uses == ["ada.text_io", "store", "other.pkg", "inner"]        # not the `use type` ones
    assert unit.aliases == {"tio": "ada.text_io", "q": "store.put", '"-"': "sub", "int_stack": "stacks",
                            "p2": "gen_p"}
    assert [call_name(c) for c in unit.subprograms[0]["calls"]] == ["q"]   # the graph resolves the alias


def test_spec_declarations_types_and_variables(samples):
    f = samples / "ada" / "src" / "tracks.ads"
    unit = AdaAnalyzer({"req_tag": "Req:"}).analyze_file(f, f.read_bytes())
    assert [(d["name"], d["reqs"]) for d in unit.declarations] == \
        [("update", ["SRS-0002", "SRS-0003"]), ("delete_track", ["SRS-0004"])]
    track = next(t for t in unit.types if t["name"] == "track")
    assert track["record"] and track["reqs"] == ["SRS-0001"] and "Az   : Float;" in track["source"]
    assert {(v["name"], v["constant"]) for v in unit.variables} == {("trk_db", False), ("max_tracks", True)}


# --- the GPR project ([sources.code] project) ------------------------------------------------

COMMON_GPR = '''-- shared settings, no sources
abstract project Common is
   type Mode_Type is ("debug", "release");
   Mode : Mode_Type := external ("MODE", "debug");
   Version := external ("VERSION");
   for Source_Dirs use ();
end Common;
'''
LIB_GPR = '''with "../common/common.gpr";
library project Lib is
   for Source_Dirs use ("src/**");               -- src and every folder below it
   for Object_Dir use "obj/" & Common.Mode;
   for Library_Dir use "lib";
   for Library_Name use "lib";
end Lib;
'''
APP_GPR = '''with "../common/common";
limited with "../lib/lib.gpr";
with "gnatcoll";                                  -- installed: not next to the project
project App is
   Extra := ("generated");
   case Common.Mode is
      when "release" | "profile" =>
         for Source_Dirs use ("src") & Extra;
      when others =>
         for Source_Dirs use ("src", "debug");
   end case;
   for Object_Dir use Project'Project_Dir & "obj";
   for Exec_Dir use "bin";
   for Main use ("main.adb");
   for Source_List_File use "sources.lst";
   package Compiler is
      for Switches ("Ada") use ("-g");
   end Compiler;
   package Builder renames Common.Builder;
   Unknown := Nowhere;
   pragma Whatever;
end App;
'''


def _gpr_tree(root: Path) -> Path:
    for folder in ("common", "lib/src/a/b", "app/src", "app/debug", "app/generated"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "common" / "common.gpr").write_text(COMMON_GPR)
    (root / "lib" / "lib.gpr").write_text(LIB_GPR)
    (root / "app" / "app.gpr").write_text(APP_GPR)
    return root / "app" / "app.gpr"


def test_gpr_folders_scenario_variables_and_imports(tmp_path):
    from atlas.code.gpr import read_project
    app = _gpr_tree(tmp_path)
    t = tmp_path.resolve()
    found = read_project(app, env={})
    assert found["name"] == "App" and found["main"] == ["main.adb"]
    assert found["source_dirs"] == [str(t / "app/src"), str(t / "app/debug"), str(t / "lib/src"),
                                    str(t / "lib/src/a"), str(t / "lib/src/a/b")]    # the ** expanded; Common has none
    assert found["object_dirs"] == [str(t / "app/obj"), str(t / "app/bin"), str(t / "lib/obj/debug"), str(t / "lib/lib")]
    assert found["withs"] == [str(t / "common/common.gpr"), str(t / "lib/lib.gpr")]
    assert found["externals"] == {"MODE": "debug", "VERSION": ""}
    warnings = "\n".join(found["warnings"])
    assert 'common.gpr:5: external ("VERSION") has no default' in warnings
    assert 'app.gpr:3: with "gnatcoll": not next to app.gpr' in warnings
    assert "app.gpr:15: Source_List_File is not used" in warnings
    assert "app.gpr:20: Nowhere is not a variable" in warnings
    assert "app.gpr:21: not understood ('pragma'...)" in warnings
    assert len(found["warnings"]) == 5                       # the packages are skipped quietly

    release = read_project(app, {"MODE": "release", "VERSION": "2"}, env={"MODE": "debug"})
    assert release["source_dirs"][:2] == [str(t / "app/src"), str(t / "app/generated")]
    assert str(t / "lib/obj/release") in release["object_dirs"] and release["externals"]["MODE"] == "release"
    assert read_project(app, env={"MODE": "release"})["externals"]["MODE"] == "release"     # as gprbuild: the environment
    assert "is not one of Mode_Type" in "\n".join(read_project(app, {"MODE": "fast"}, env={})["warnings"])


def test_gpr_that_cannot_be_read_says_where(tmp_path):
    from atlas.code.gpr import read_project
    (tmp_path / "bad.gpr").write_text('project Bad is\n   for Source_Dirs use ("src";\nend Bad;\n')
    found = read_project(tmp_path / "bad.gpr", env={})
    assert "bad.gpr:2: expected ')', found ';'; the rest of the file is not read" in found["warnings"][0]
    assert found["source_dirs"] == [str(tmp_path.resolve())]           # what was read: no Source_Dirs, the default
    assert "does not exist" in read_project(tmp_path / "none.gpr", env={})["warnings"][0]
    (tmp_path / "plain.gpr").write_text("project Plain is\nend Plain;\n")        # the defaults: its own folder
    plain = read_project(tmp_path / "plain.gpr", env={})
    assert plain["source_dirs"] == plain["object_dirs"] == [str(tmp_path.resolve())]


def _gpr_config(tmp_path, toml, samples, extra=""):
    """The Ada samples in a temporary folder with their samples.gpr, and atlas.toml over it."""
    import shutil
    from atlas.config import load_config
    ada = tmp_path / "ada"
    shutil.copytree(samples / "ada", ada, ignore=shutil.ignore_patterns("obj"))
    stray = ada / "obj" / "release" / "b__main.adb"                  # what gprbuild leaves: not code
    stray.parent.mkdir(parents=True)
    stray.write_text("package body B is end B;\n")
    return ada, load_config(toml(f'''
[pipeline]
steps = ["code"]
[sources.code]
project = "{(ada / 'samples.gpr').as_posix()}"
test_patterns = ["**/tests/**"]
{extra}
[sources.code.project_vars]
BUILD = "release"
'''))


def test_the_project_gives_roots_exclude_and_commands(tmp_path, toml, samples, capsys):
    import json
    from atlas.cli import list_plugins, run
    from atlas.code import LanguageTools
    from hephesto import BuildTools, CodeTools
    from atlas.code.gpr import code_config
    from atlas.store import Store
    ada, cfg = _gpr_config(tmp_path, toml, samples)
    sc = code_config(cfg)
    assert [cfg.resolve(r) for r in sc["roots"]] == [(ada / "src").resolve(), (ada / "tests").resolve()]   # from atlas.toml
    assert f"{(ada / 'obj' / 'release').resolve().as_posix()}/*" in sc["exclude"]
    gprbuild = ["gprbuild", "-P", "{project}", "-XBUILD=release", "--autoconf={build}/gprbuild.cgpr"]
    checking = ["--relocate-build-tree={build}/check", f"--root-dir={ada.resolve()}"]   # not the .ali of the builds
    assert sc["check_command"] == [*gprbuild, *checking, "-c", "-u", "-f", "{file}"]
    assert sc["check_commands"]["ads"][-2:] == ["-cargs", "-gnatc"]
    assert sc["test_command"] == [*gprbuild, "{file}", "-o", "{exe}"]
    build = BuildTools.from_config(cfg)
    tracks = (ada / "src" / "tracks.adb").resolve()
    assert build._command(build.check_command, tracks)[:4] == ["gprbuild", "-P", str((ada / "samples.gpr").resolve()),
                                                              "-XBUILD=release"]
    lsp = LanguageTools.from_config(cfg, build)
    assert lsp._gpr() == str((ada / "samples.gpr").resolve()) and lsp.scenario == {"BUILD": "release"}
    assert CodeTools.from_config(cfg).read_file(str(ada / "obj" / "release" / "b__main.adb"))["error"]

    assert run(cfg) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"code.parse": 5}
    assert not any("b__main" in u["file"] for u in Store(cfg.workspace).code_units())
    assert list_plugins(cfg) == 0
    out = capsys.readouterr().out
    assert "(BUILD=release)" in out and "(from the project)" in out and "5 .ads/.adb (2 .ads)" in out

    _, given = _gpr_config(tmp_path / "given", toml, samples, f'roots = ["{(ada / "src").as_posix()}"]\n'
                                                             'check_command = ["gcc", "-c", "{file}"]')
    sc = code_config(given)                                  # what is given stays; the object folders still go out
    assert sc["roots"] == [(ada / "src").as_posix()] and sc["check_command"] == ["gcc", "-c", "{file}"]
    assert "check_commands" not in sc and sc["test_command"][0] == "gprbuild" and len(sc["exclude"]) >= 1


GNAT_PATH = ["~/.local/share/alire/toolchains/gnat_native_*/bin", "~/.local/share/alire/toolchains/gprbuild_*/bin"]


def _gprbuild() -> bool:
    import glob
    import os
    import shutil
    found = [p for pattern in GNAT_PATH for p in glob.glob(os.path.expanduser(pattern))]
    path = os.pathsep.join(found + [os.environ.get("PATH", "")])
    return bool(shutil.which("gprbuild", path=path) and shutil.which("gcc", path=path))


@pytest.mark.skipif(not _gprbuild(), reason="GNAT and gprbuild are not installed (alr toolchain --select gnat_native gprbuild)")
def test_gprbuild_checks_and_builds_over_the_project(tmp_path, toml, samples):
    from hephesto import BuildTools
    ada, cfg = _gpr_config(tmp_path, toml, samples, f"tool_path = {GNAT_PATH}".replace("'", '"'))
    build = BuildTools.from_config(cfg)
    assert build.check_code(str(ada / "src" / "tracks.adb"))["ok"]
    assert build.check_code(str(ada / "src" / "tracks.ads"))["ok"]             # a spec alone, with -gnatc
    (ada / "src" / "bad.adb").write_text("with Tracks;\nprocedure Bad is\nbegin\n   Tracks.Nope;\nend Bad;\n")
    bad = build.check_code(str(ada / "src" / "bad.adb"))
    assert not bad["ok"] and bad["errors"][0]["line"] == 4 and "Nope" in bad["errors"][0]["message"]
    ran = build.run_test(str(ada / "tests" / "test_tracks.adb"))
    assert ran["passed"] and ran["output"].strip() == "delete"
    assert (ada / "obj" / "release" / "tracks.ali").exists()                   # the Object_Dir of BUILD=release
    assert (build.build_dir / "gprbuild.cgpr").exists()                       # the compilers, found once


def _als() -> list[str]:
    import glob
    import os
    return glob.glob(os.path.expanduser("~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"))


@pytest.mark.skipif(not (_gprbuild() and _als()), reason="GNAT or the Ada Language Server is not installed")
def test_the_language_server_opens_the_project(tmp_path, toml, samples):
    from atlas.code import LanguageTools
    ada, cfg = _gpr_config(tmp_path, toml, samples, f"tool_path = {GNAT_PATH}\n".replace("'", '"') +
                           'language_server = ["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"]')
    tools = LanguageTools.from_config(cfg)
    tracks = ada / "src" / "tracks.adb"
    line = next(n for n, text in enumerate(tracks.read_text().splitlines(), 1) if "Logger.Log" in text)
    found = tools.find_definition(str(tracks), line, "Log")
    assert found["definitions"][0]["file"].endswith("logger.ads"), found
    assert not (cfg.workspace / "build" / "atlas_lsp.gpr").exists()            # not the minimal one
