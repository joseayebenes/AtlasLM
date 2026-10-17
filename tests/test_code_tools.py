import inspect
import os
import stat
from types import SimpleNamespace

import pytest

from atlas import plugins
from atlas.code import TOOL_NAMES, CodeTools

TRACKS = """package body Tracks is
   procedure Update (I : Integer) is
   begin
      Logger.Log ("update");
   end Update;

   procedure Delete_Track (I : Integer) is
   begin
      Logger.Log ("delete");
   end Delete_Track;
end Tracks;
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with its code under src/, a build folder that is excluded and a secret outside."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "util").mkdir(parents=True)
    (tmp_path / "src" / "tracks.adb").write_text(TRACKS)
    (tmp_path / "src" / "util" / "logger.ads").write_text("package Logger is\n   procedure Log (S : String);\nend Logger;\n")
    (tmp_path / "src" / "build").mkdir()
    (tmp_path / "src" / "build" / "tracks.ali").write_text("generated\n")
    (tmp_path / "secret.txt").write_text("password\n")
    return CodeTools(["src"], exclude=["**/build/**"])


def test_list_grep_and_read(project):
    assert project.list_files("**/*.ad?")["files"] == ["src/tracks.adb", "src/util/logger.ads"]
    assert project.list_files()["files"] == ["src/tracks.adb", "src/util/logger.ads"]     # build/ is excluded
    found = project.grep(r"Logger\.Log", glob="*.adb", context=1)
    assert [(m["file"], m["line"]) for m in found["matches"]] == [("src/tracks.adb", 4), ("src/tracks.adb", 9)]
    assert found["matches"][0]["context"].splitlines()[0] == "    3 |    begin"
    assert project.grep("procedure log", ignore_case=True, path="src/util")["count"] == 1
    assert project.grep("(unclosed")["error"].startswith("invalid regular expression")
    part = project.read_file("src/tracks.adb", 7, 10)
    assert part["source"].splitlines() == ["    7 |    procedure Delete_Track (I : Integer) is", "    8 |    begin",
                                           '    9 |       Logger.Log ("delete");', "   10 |    end Delete_Track;"]
    assert part["total_lines"] == 11 and not part["truncated"]


@pytest.mark.parametrize("path", ["secret.txt", "src/../secret.txt", "/etc/hosts", "src/build/tracks.ali",
                                  "src/link.txt"])
def test_nothing_outside_the_roots(project, tmp_path, path):
    try:
        (tmp_path / "src" / "link.txt").symlink_to(tmp_path / "secret.txt")      # a link that leaves the root
    except OSError:                                 # Windows: a symbolic link needs a privilege
        if path == "src/link.txt":
            pytest.skip("cannot create a symbolic link here")
    assert "error" in project.read_file(path)
    assert "error" in project.write_file(path, "x")
    assert "secret.txt" not in str(project.grep("password"))


def test_edit_rules(project, tmp_path):
    f = "src/tracks.adb"
    assert "read" in project.edit_file(f, "update", "refresh")["error"]          # not read yet
    project.read_file(f)
    many = project.edit_file(f, "Logger.Log", "Log")
    assert "appears 2 times (lines [4, 9])" in many["error"]
    missing = project.edit_file(f, 'Logger.Log ("deleted");', "x")
    assert missing["similar_lines"][0]["line"] == 9                              # helps the model copy it right
    done = project.edit_file(f, 'Logger.Log ("delete");', 'Logger.Log ("track deleted");')
    assert done["replacements"] == 1 and '+      Logger.Log ("track deleted");' in done["diff"]
    assert project.edit_file(f, "Logger.Log", "Trace.Log", replace_all=True)["replacements"] == 2   # no new read needed
    (tmp_path / f).write_text(TRACKS)                                         # someone else changes it
    assert "changed since you read it" in project.edit_file(f, "Update", "Refresh")["error"]


def test_an_edit_already_made_and_the_closest_block(project):
    f = "src/tracks.adb"
    project.read_file(f)
    old, new = '   begin\n      Logger.Log ("update");', '   begin\n      Logger.Log ("updated");\n      Count := Count + 1;'
    assert project.edit_file(f, old, new)["replacements"] == 1
    again = project.edit_file(f, old, new)                    # a model that did not notice it was made
    assert "already made" in again["error"] and again["line"] == 3     # where new_text starts, "   begin"
    wrong = project.edit_file(f, 'procedure Delete_Track (I : Integer) is\nbegin\n   Logger.Log ("deleting");',
                              "x")                             # wrong indentation and text: the block as it is
    assert wrong["closest"]["start_line"] == 8 and wrong["closest"]["end_line"] == 10
    assert wrong["closest"]["text"].startswith("   procedure Delete_Track (I : Integer) is")
    assert "closest" not in project.edit_file(f, "Something else entirely", "x")


def test_an_old_text_too_long_is_refused(project, tmp_path):
    from atlas.code.tools import MAX_OLD_LINES
    f = tmp_path / "src" / "long.adb"
    f.write_text("".join(f"   X{i} : Integer := {i};\n" for i in range(MAX_OLD_LINES + 5)))
    project.read_file("src/long.adb")
    whole = f.read_text()
    refused = project.edit_file("src/long.adb", whole, whole.replace("X0", "Y0"))
    assert f"has {MAX_OLD_LINES + 5} lines" in refused["error"] and "several edits" in refused["error"]
    assert project.edit_file("src/long.adb", "   X0 : Integer := 0;", "   Y0 : Integer := 0;")["replacements"] == 1


def test_backslashes_at_the_end_of_every_new_line_are_removed(project, tmp_path):
    f = "src/tracks.adb"
    project.read_file(f)
    done = project.edit_file(f, "   procedure Update (I : Integer) is\n   begin",
                             "   procedure Update (\\\n      I : Integer) is\\\n   begin")   # gemma-4-26b
    assert done["replacements"] == 1 and done["notes"]
    assert "   procedure Update (\n      I : Integer) is\n   begin\n" in (tmp_path / f).read_text()
    new = project.write_file("src/new.ads", "package New_Unit is\\\n   X : Integer;\\\nend New_Unit;\n")
    assert new["notes"] and (tmp_path / "src" / "new.ads").read_text() == "package New_Unit is\n   X : Integer;\nend New_Unit;\n"
    shell = "a \\\n  b\nc\nd\ne\n"                      # one continuation line: left as it is
    project.write_file("src/run.sh", shell)
    assert (tmp_path / "src" / "run.sh").read_text() == shell


def test_encoding_line_endings_and_permissions(project, tmp_path):
    crlf = tmp_path / "src" / "legacy.adb"
    crlf.write_bytes("-- Módulo de pistas\r\nprocedure Run is\r\nbegin\r\n   null;\r\nend Run;\r\n".encode("latin-1"))
    crlf.chmod(0o750)                               # on Windows, chmod only sets or clears read-only
    project.read_file("src/legacy.adb")
    assert "replacements" in project.edit_file("src/legacy.adb", "begin\n   null;", "begin\n   Go;")
    assert crlf.read_bytes() == "-- Módulo de pistas\r\nprocedure Run is\r\nbegin\r\n   Go;\r\nend Run;\r\n".encode("latin-1")
    assert os.name == "nt" or stat.S_IMODE(crlf.stat().st_mode) == 0o750
    assert "cannot hold" in project.edit_file("src/legacy.adb", "Go;", "Go;  -- 漢字")["error"]
    assert not list((tmp_path / "src").glob(".*atlas-tmp"))


def test_write_and_read_only(project, tmp_path):
    new = project.write_file("src/new/radar.ads", "package Radar is\nend Radar;\n")
    assert new["created"] and (tmp_path / "src" / "new" / "radar.ads").exists()
    assert "read" in project.write_file("src/tracks.adb", "x")["error"]           # overwrite: read it first
    project.read_file("src/tracks.adb")
    assert "-   procedure Update (I : Integer) is" in project.write_file("src/tracks.adb", "null;\n")["diff"]
    same = project.write_file("src/tracks.adb", "null;\n")                   # a model that loops: tell it
    assert same["unchanged"] and "nothing changed" in same["note"] and "diff" not in same
    ro = CodeTools(["src"], read_only=True)
    ro.read_file("src/tracks.adb")
    assert ro.edit_file("src/tracks.adb", "null;", "x") == {"error": "these tools are read-only"}
    assert "error" in CodeTools([]).read_file("src/tracks.adb")                   # no roots, no files


def test_protected_files_can_be_read_not_changed(project, tmp_path):
    """The tests judge the code: an agent asked to fix one changed what it expected instead."""
    (tmp_path / "src" / "tests").mkdir()
    (tmp_path / "src" / "tests" / "test_tracks.py").write_text("assert True\n")
    tools = CodeTools(["src"], exclude=["**/build/**"], protected=["src/tests/**"])
    assert "assert True" in tools.read_file("src/tests/test_tracks.py")["source"]
    for res in (tools.edit_file("src/tests/test_tracks.py", "True", "False"),
                tools.write_file("src/tests/test_tracks.py", "x"), tools.write_file("src/tests/test_new.py", "x")):
        assert "is protected" in res["error"] and "change the code it checks" in res["error"]
    assert (tmp_path / "src" / "tests" / "test_tracks.py").read_text() == "assert True\n"
    assert not (tmp_path / "src" / "tests" / "test_new.py").exists()
    tools.read_file("src/tracks.adb")                                       # the rest, as always
    assert tools.edit_file("src/tracks.adb", '"update"', '"updated"')["replacements"] == 1


def test_registered_as_tool_plugins(project, toml, tmp_path):
    from atlas.config import load_config
    plugins.load_plugins()
    assert set(TOOL_NAMES) <= set(plugins.available("tool"))
    context = SimpleNamespace(cfg=load_config(toml(f'[sources.code]\nroots = ["{(tmp_path / "src").as_posix()}"]\n')))
    read, edit = (plugins.get("tool", n)(context) for n in ("read_file", "edit_file"))
    path = (tmp_path / "src" / "tracks.adb").as_posix()
    read.run(path=path)                                       # the same CodeTools behind every tool
    assert edit.run(path=path, old_text="Update", new_text="Refresh", replace_all=True)["replacements"] == 2
    params = inspect.signature(edit.function()).parameters    # what ADK and MCP turn into a schema
    assert list(params) == ["path", "old_text", "new_text", "replace_all"] and params["replace_all"].annotation
    assert edit.description.startswith("Replaces `old_text` by `new_text`")


def test_tolerates_the_usual_mistakes_of_small_models(project, tmp_path):
    # what gemma4:12b did: quotes escaped as inside a JSON string, and one space too many
    f = "src/tracks.adb"
    assert project.list_files("*.adb")["files"] == ["src/tracks.adb"]          # in every subfolder
    assert project.list_files("*.ads", path=".")["files"] == ["src/util/logger.ads"]   # "." holds the root
    project.read_file(f)
    done = project.edit_file(f, '       Logger.Log (\\"delete\\");', '       Logger.Log (\\"track deleted\\");')
    assert len(done["notes"]) == 2 and done["replacements"] == 1
    assert (tmp_path / f).read_text() == TRACKS.replace('"delete"', '"track deleted"')
    # a block with other indentation is re-indented like the file
    block = project.edit_file(f, "procedure Update (I : Integer) is\nbegin\n   Logger.Log (\"update\");",
                              "procedure Update (I : Integer) is\nbegin\n   Check (I);\n   Logger.Log (\"update\");")
    assert block["notes"] == ["old_text was found ignoring indentation, and new_text was re-indented like the file"]
    assert "   procedure Update (I : Integer) is\n   begin\n      Check (I);\n      Logger.Log" in (tmp_path / f).read_text()
    # ignoring indentation must still be unique
    assert "appears 2 times" in project.edit_file(f, "begin", "start")["error"]


def test_tolerates_the_listing_of_read_file_copied_into_an_edit(project, tmp_path):
    """What gemma4:12b sent in edits of several lines: the line numbers of read_file, a
    backslash before each line break and some line breaks as \\n, in old_text and in
    new_text. Without this tolerance it repeated the same failed edit until max_steps."""
    f = "src/tracks.adb"
    project.read_file(f)
    old = ("   procedure Update (I : Integer) is\\\n    3 |    begin\\\n"
           "    4 |       Logger.Log (\\\"update\\\");\\\n    5 |    end Update;")
    new = ("    2 |    procedure Update (I : Integer) is\\n    3 |    begin\\n"
           "    4 |       if I > 0 then\\n    5 |          Logger.Log (\\\"update\\\");\\n    6 |       end if;\\n"
           "    7 |    end Update;")
    done = project.edit_file(f, old, new)
    assert done["replacements"] == 1 and any("line numbers of read_file" in n for n in done["notes"])
    assert (tmp_path / f).read_text() == TRACKS.replace(
        '      Logger.Log ("update");\n', '      if I > 0 then\n         Logger.Log ("update");\n      end if;\n')
    # a real line that starts like a listing is left alone when old_text has no listing
    assert "is not in" in project.edit_file(f, "    9 | no such line", "x")["error"]


# --- whole subprograms ------------------------------------------------------------------------

def test_replace_a_whole_subprogram_by_its_name(project, tmp_path):
    pytest.importorskip("tree_sitter_ada")
    f = "src/tracks.adb"
    assert "read src/tracks.adb" in project.replace_subprogram(f, "Update", "procedure Update is begin null; end;")["error"]
    project.read_file(f)
    new = """procedure Update (I : Integer) is
begin
   if I > 0 then
      Logger.Log ("update");
   end if;
end Update;"""
    done = project.replace_subprogram(f, "tracks.update", new)          # qualified, and Ada has no case
    assert done["replaced"] == "body of Tracks.Update, lines 2-5" and "+      if I > 0 then" in done["diff"]
    text = (tmp_path / f).read_text()
    assert text == TRACKS.replace('      Logger.Log ("update");\n',
                                  '      if I > 0 then\n         Logger.Log ("update");\n      end if;\n')
    # with its comment: the comment above is replaced too; nothing was copied from the file
    project.replace_subprogram(f, "Delete_Track", "--  Req: SRS-0004\nprocedure Delete_Track (I : Integer) is\n"
                                                  "begin\n   null;\nend Delete_Track;")
    again = project.replace_subprogram(f, "Delete_Track", "--  Req: SRS-0005\nprocedure Delete_Track (I : Integer) is\n"
                                                          "begin\n   null;\nend Delete_Track;")
    text = (tmp_path / f).read_text()
    assert "SRS-0004" not in text and "   --  Req: SRS-0005\n   procedure Delete_Track" in text and "+" in again["diff"]
    # what it refuses, and why
    assert "no complete subprogram named Update" in project.replace_subprogram(f, "Update", "   null;")["error"]
    missing = project.replace_subprogram(f, "Updat", "procedure Updat is begin null; end Updat;")["error"]
    assert "no subprogram named Updat (similar: update" in missing and "add_subprogram" in missing
    (tmp_path / "src" / "notes.txt").write_text("procedure X\n")
    assert "know Ada and Python files; use edit_file" in project.replace_subprogram("src/notes.txt", "X", "x")["error"]


def test_overloads_and_declarations(project, tmp_path):
    pytest.importorskip("tree_sitter_ada")
    spec = "src/util/logger.ads"
    (tmp_path / spec).write_text("package Logger is\n   procedure Log (S : String);\n   procedure Log (I : Integer);\n"
                                 "private\n   Count : Natural := 0;\nend Logger;\n")
    project.read_file(spec)
    both = project.replace_subprogram(spec, "Log", "procedure Log (S : String; Level : Natural := 0);")
    assert "2 subprograms are named Log" in both["error"] and "(lines 2-2)" in both["error"]
    assert project.replace_subprogram(spec, "Log", "procedure Log (S : String; Level : Natural := 0);", line=2)[
        "replaced"] == "declaration of Logger.Log, lines 2-2"
    body = project.replace_subprogram(spec, "Log", "procedure Log (S : String) is begin null; end Log;", line=2)
    assert "has no body of Log, but it has a declaration" in body["error"]
    # a new declaration goes before the private part
    added = project.add_subprogram(spec, "procedure Flush;")
    assert added["added"] == ["Flush"]
    assert (tmp_path / spec).read_text() == ("package Logger is\n   procedure Log (S : String; Level : Natural := 0);\n"
                                             "   procedure Log (I : Integer);\n\n   procedure Flush;\n\nprivate\n"
                                             "   Count : Natural := 0;\nend Logger;\n")
    assert "already has a declaration of Logger.Flush" in project.add_subprogram(spec, "procedure Flush;")["error"]


def test_add_subprograms_to_a_package_body_and_after_another(project, tmp_path):
    pytest.importorskip("tree_sitter_ada")
    f = "src/tracks.adb"
    project.read_file(f)
    done = project.add_subprogram(f, "procedure Clear is\nbegin\n   null;\nend Clear;")
    assert done["added"] == ["Clear"]
    assert (tmp_path / f).read_text().endswith("   end Delete_Track;\n\n   procedure Clear is\n   begin\n      null;\n"
                                               "   end Clear;\n\nend Tracks;\n")
    project.add_subprogram(f, "function Count return Natural is (0);", after="Update")
    lines = (tmp_path / f).read_text().split("\n")
    assert lines[5:8] == ["", "   function Count return Natural is (0);", ""]
    # before another one, and before its comment: in Ada a subprogram must come before its caller
    (tmp_path / f).write_text((tmp_path / f).read_text().replace("   procedure Delete_Track",
                                                                 "   --  Req: SRS-0004\n   procedure Delete_Track"))
    project.read_file(f)
    project.add_subprogram(f, "procedure Do_Ping is\nbegin\n   null;\nend Do_Ping;", before="Delete_Track")
    text = (tmp_path / f).read_text()
    assert "   end Do_Ping;\n\n   --  Req: SRS-0004\n   procedure Delete_Track" in text
    assert "no complete subprogram" in project.add_subprogram(f, "X := 1;")["error"]
    assert "no subprogram named Nothing" in project.add_subprogram(f, "procedure Y is null;", after="Nothing")["error"]
    (tmp_path / "src" / "main.adb").write_text("procedure Main is\nbegin\n   null;\nend Main;\n")
    project.read_file("src/main.adb")
    assert "not a package" in project.add_subprogram("src/main.adb", "procedure Y is null;")["error"]


def test_python_functions_and_methods(project, tmp_path):
    f = "src/calc.py"
    (tmp_path / f).write_text("import os\n\n\n@staticmethod\ndef add(a, b):\n    return 0\n\n\nclass Calc:\n"
                              "    def mul(self, a, b):\n        return 0\n")
    project.read_file(f)
    project.replace_subprogram(f, "add", "def add(a, b):\n    return a + b\n")
    project.replace_subprogram(f, "Calc.mul", "def mul(self, a, b):\n    return a * b    # \"\\n\" stays\n")
    project.add_subprogram(f, "def sub(a, b):\n    return a - b\n")
    assert (tmp_path / f).read_text() == ("import os\n\n\ndef add(a, b):\n    return a + b\n\n\nclass Calc:\n"
                                          "    def mul(self, a, b):\n        return a * b    # \"\\n\" stays\n\n\n"
                                          "def sub(a, b):\n    return a - b\n")
    # the listing of read_file copied into the new text is removed
    listed = project.replace_subprogram(f, "sub", "   14 | def sub(a, b):\n   15 |     return b - a\n")
    assert listed["notes"] and (tmp_path / f).read_text().endswith("def sub(a, b):\n    return b - a\n")
