"""check_code and run_test: the compiler and the tests of the project for coding agents. With
a fake compiler everywhere, and with GNAT when it is installed (Alire's toolchain or PATH)."""
import glob
import os
import shutil
import sys
from pathlib import Path

import pytest

from atlas.code import BuildTools
from atlas.config import load_config

GNAT_PATH = ["~/.local/share/alire/toolchains/gnat_native_*/bin"]
FAKE_CC = '''import sys
from pathlib import Path
args = sys.argv[1:]
Path("args.txt").write_text("\\n".join(args))
src = Path(args[-1]) if not args[-1].startswith("/") or Path(args[-1]).exists() else None
if "-o" in args:                            # a build: write an executable that runs the "test"
    exe = Path(args[args.index("-o") + 1])
    body = Path(args[args.index("-o") - 1]).read_text()
    if "BROKEN" in body:
        print(f"{Path(args[args.index('-o') - 1]).name}:2:4: error: missing \\";\\"")
        sys.exit(1)
    code = 1 if "FAIL" in body else 0
    exe.write_text(f"#!{sys.executable}\\nimport sys\\nprint('ran')\\nsys.exit({code})\\n")
    exe.chmod(0o755)
    sys.exit(0)
if src and "BROKEN" in src.read_text():
    print(f"{src.name}:3:7: error: \\"Trk\\" is undefined")
    print(f"{src.name}:5:1: warning: variable \\"H\\" is not referenced")
    sys.exit(1)
'''


@pytest.fixture
def project(tmp_path, toml, samples):
    """The Ada samples in a temporary folder, and atlas.toml over them with a fake compiler."""
    ada = tmp_path / "ada"
    shutil.copytree(samples / "ada", ada)
    cc = tmp_path / "bin1" / "fakecc.py"
    cc.parent.mkdir()
    cc.write_text(FAKE_CC)

    def make(check=None, test=None, tool_path=None, timeout=None, extra="") -> BuildTools:
        cfg = load_config(toml(f'''
[sources.code]
roots = ["{ada.as_posix()}"]
test_patterns = ["**/tests/**"]
check_command = {check if check is not None else [sys.executable, str(cc), "{include}", "{file}"]}
test_command = {test if test is not None else [sys.executable, str(cc), "{include}", "{file}", "-o", "{exe}"]}
tool_path = {tool_path or []}
command_timeout = {timeout or 30}
{extra}
'''.replace("'", '"')))
        return BuildTools.from_config(cfg)
    return ada, make


def test_check_code_returns_the_messages_of_the_compiler(project):
    ada, make = project
    tools = make()
    ok = tools.check_code(str(ada / "src" / "tracks.adb"))
    assert ok["ok"] is True and ok["errors"] == [] and ok["file"].endswith("src/tracks.adb")
    args = (tools.build_dir / "args.txt").read_text().splitlines()       # it ran in <workspace>/build
    assert f"-I{ada / 'src'}" in args and f"-I{ada / 'tests'}" in args and args[-1] == str(ada / "src" / "tracks.adb")
    (ada / "src" / "tracks.adb").write_text("BROKEN")
    bad = tools.check_code(str(ada / "src" / "tracks.adb"))
    assert bad["ok"] is False
    hint = bad["errors"][0].pop("hint")                                   # see test_hints_come_with_the_errors...
    assert bad["errors"] == [{"file": "tracks.adb", "line": 3, "column": 7, "message": '"Trk" is undefined'}]
    assert hint.startswith("Trk is not declared or not visible here")
    assert bad["warnings"][0]["message"] == 'variable "H" is not referenced'
    # only inside the code roots; nothing without a command
    assert "error" in tools.check_code("/etc/hosts")
    assert "check_command" in make(check=[]).check_code(str(ada / "src" / "tracks.adb"))["error"]


def test_run_test_builds_runs_and_tells_why_it_failed(project):
    ada, make = project
    tools = make()
    test = ada / "tests" / "test_tracks.adb"
    res = tools.run_test(str(test))
    assert (res["built"], res["passed"], res["exit_code"], res["output"].strip()) == (True, True, 0, "ran")
    test.write_text(test.read_text() + "-- FAIL")
    assert tools.run_test(str(test))["passed"] is False
    test.write_text("BROKEN")
    res = tools.run_test(str(test))
    assert res["built"] is False and res["errors"][0]["message"] == 'missing ";"'


def test_tool_path_timeout_and_missing_compiler(project, tmp_path):
    ada, make = project
    fake = tmp_path / "bin2" / "fakecc"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n")
    fake.chmod(0o755)
    # tool_path (with a glob) goes before PATH: "fakecc" is found there
    tools = make(check=["fakecc", "{file}"], tool_path=[str(tmp_path / "bin*")])
    assert tools.check_code(str(ada / "src" / "tracks.adb"))["ok"] is True
    assert "not found" in make(check=["no-such-compiler", "{file}"]).check_code(str(ada / "src" / "tracks.adb"))["error"]
    slow = make(check=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert "more than 1 s" in slow.check_code(str(ada / "src" / "tracks.adb"))["error"]


WRAPPER = '''import os, subprocess, sys
print("service started", flush=True)        # what a test needs first, e.g. the server it talks to
sys.exit(subprocess.run(sys.argv[1:], env={**os.environ, "SERVER_PORT": "5999"}).returncode)
'''


def test_a_test_runs_through_a_wrapper_and_python_tests_just_run(project, tmp_path, monkeypatch):
    ada, make = project
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "with_server").write_text(f"#!{sys.executable}\n" + WRAPPER)
    (scripts / "with_server").chmod(0o755)
    monkeypatch.chdir(tmp_path)                 # a relative tool_path is relative to where atlas runs
    tools = make(tool_path=["scripts"], extra='''
run_command = ["with_server", "{exe}"]
[sources.code.test_commands]
py = ["with_server", "PYTHON", "{file}"]
'''.replace("PYTHON", sys.executable))
    res = tools.run_test(str(ada / "tests" / "test_tracks.adb"))
    assert res["built"] and res["passed"] and res["output"].split() == ["service", "started", "ran"]
    test = ada / "tests" / "test_session.py"
    test.write_text("import os\nassert os.environ['SERVER_PORT'] == '5999'\nprint('step 1 ok')\n")
    res = tools.run_test(str(test))
    assert (res["passed"], res["exit_code"]) == (True, 0) and "built" not in res and "step 1 ok" in res["output"]
    test.write_text("assert 1 == 2, 'step 2: expected OK PONG'\n")
    res = tools.run_test(str(test))
    assert res["passed"] is False and "step 2: expected OK PONG" in res["output"]
    # without a command for .py, a Python test goes through test_command like the others
    assert "built" in make().run_test(str(test))


def test_check_commands_by_extension(project):
    """GNAT checks a spec alone with -gnatc, and a body generating code (more errors)."""
    ada, make = project
    cc = ada.parent / "bin1" / "fakecc.py"
    tools = make(extra=f'[sources.code.check_commands]\nads = ["{sys.executable}", "{cc}", "-gnatc", "{{file}}"]\n')
    assert tools.check_code(str(ada / "src" / "tracks.ads"))["ok"]
    assert "-gnatc" in (tools.build_dir / "args.txt").read_text().splitlines()
    assert tools.check_code(str(ada / "src" / "tracks.adb"))["ok"]
    assert "-gnatc" not in (tools.build_dir / "args.txt").read_text().splitlines()


PROGRAM = '''import sys
for line in sys.stdin:
    print("< " + line.strip().upper())
if "crash" in sys.argv[1:]:
    print("connected to 127.0.0.1:5555")
    print("raised CONSTRAINT_ERROR : tracks.adb:6 length check failed", file=sys.stderr)
    print('  File "TEST", line 3, in test_tracks', file=sys.stderr)
    print("tracks.adb:6 again, and logger.adb:999 (no such line)", file=sys.stderr)
    sys.exit(1)
'''


def test_run_program_and_the_code_its_output_cites(project, tmp_path):
    ada, make = project
    program = tmp_path / "program.py"
    program.write_text(PROGRAM.replace("TEST", str(ada / "tests" / "test_tracks.adb")))
    tools = make(extra=f'program_command = ["{sys.executable}", "{program}"]\nprotected = ["**/tests/**"]\n')
    res = tools.run_program(["ping", "hello tester"])
    assert res == {"exit_code": 0, "output": "< PING\n< HELLO TESTER\n"}
    crash = make(extra=f'program_command = ["{sys.executable}", "{program}", "crash"]\nprotected = ["**/tests/**"]\n')
    res = crash.run_program([])
    assert res["exit_code"] == 1 and "length check failed" in res["output"]
    # the line of tracks.adb, once; not the protected test, nor "127.0.0.1:5555", nor a line past the end
    [at] = res["code_at"]
    assert at["file"].endswith("src/tracks.adb") and at["line"] == 6
    lines = at["source"].splitlines()
    assert len(lines) == 7 and lines[3].startswith(">   6 | ") and lines[0].startswith("    3 | ")
    # run_test shows it too
    test = ada / "tests" / "test_crash.py"
    test.write_text("import sys\nprint('Step 4: received nothing; server: raised at tracks.adb:2')\nsys.exit(1)\n")
    tools = make(extra=f'protected = ["**/tests/**"]\n[sources.code.test_commands]\npy = ["{sys.executable}", "{{file}}"]\n')
    res = tools.run_test(str(test))
    assert res["passed"] is False and res["code_at"][0]["line"] == 2
    assert "error" in make().run_program(["x"])                                 # nothing configured
    assert "list of strings" in crash.run_program("ping")["error"]


def test_offered_by_the_mcp_server(project, toml):
    from atlas.mcp import default_tools
    ada, make = project
    tools = make()
    cfg = load_config(toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\ncheck_command = ["cc", "{{file}}"]\n'))
    assert "check_code" in default_tools(cfg) and "run_test" not in default_tools(cfg)     # run_test runs code
    assert "run_program" not in default_tools(cfg)
    assert "check_code" not in default_tools(load_config(toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\n')))


# --- with GNAT --------------------------------------------------------------------------------

def _gnat() -> bool:
    found = [p for pattern in GNAT_PATH for p in glob.glob(os.path.expanduser(pattern))]
    return bool(shutil.which("gnatmake", path=os.pathsep.join(found + [os.environ.get("PATH", "")])))


gnat = pytest.mark.skipif(not _gnat(), reason="GNAT is not installed (alr toolchain --select gnat_native gprbuild)")


@gnat
def test_gnat_finds_what_a_small_model_got_wrong(project):
    """What gemma4:12b wrote when asked for a counter of deleted tracks: a second
    Delete_Track, and a variable with the name of a function. Valid syntax, illegal Ada."""
    ada, make = project
    tools = make(check=["gcc", "-c", "-gnatc", "{include}", "{file}"], tool_path=GNAT_PATH)
    body = ada / "src" / "tracks.adb"
    assert tools.check_code(str(body))["ok"] is True
    body.write_text(body.read_text().replace("end Tracks;", """
   Deleted_Count : Integer := 0;
   function Deleted_Count return Integer is
   begin
      return Deleted_Count;
   end Deleted_Count;
   procedure Delete_Track (I : Integer) is
   begin
      Trk_DB (I).Id := 0;
   end Delete_Track;
end Tracks;"""))
    res = tools.check_code(str(body))
    assert res["ok"] is False and len(res["errors"]) >= 2, res
    assert any("conflict" in e["message"] or "duplicate" in e["message"] or "homograph" in e["message"]
               for e in res["errors"]), res


@gnat
def test_gnat_builds_and_runs_a_test(project):
    ada, make = project
    tools = make(check=["gcc", "-c", "-gnatc", "{include}", "{file}"], tool_path=GNAT_PATH,
                 test=["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"])
    res = tools.run_test(str(ada / "tests" / "test_tracks.adb"))
    assert (res["built"], res["passed"], res["output"].strip()) == (True, True, "delete"), res
    failing = ada / "tests" / "test_failing.adb"
    failing.write_text('procedure Test_Failing is\nbegin\n   raise Program_Error with "case 3: 300.0 should be Normal";\n'
                       'end Test_Failing;\n')
    res = tools.run_test(str(failing))
    assert res["built"] is True and res["passed"] is False and "300.0 should be Normal" in res["output"], res
    # a main procedure whose file has another name builds, with a warning that says so
    wrong = ada / "tests" / "test_speeds.adb"
    wrong.write_text("procedure Test_Speeds_Classify is\nbegin\n   null;\nend Test_Speeds_Classify;\n")
    res = tools.run_test(str(wrong))
    assert res["passed"] is True and "file name does not match unit name" in res["warnings"][0]["message"], res


def test_the_same_message_goes_once_with_its_lines_and_hints():
    """What GNAT said about the test gemma4:12b wrote: two errors in nine places each, and a
    hint in the same place. One entry per error, with its other lines and the hint."""
    from atlas.code.build import parse_messages
    out = "".join(f't.adb:{n}:29: error: operator for type "Speed_Class" defined at speeds.ads:2 is not directly '
                  f'visible\nt.adb:{n}:29: error: use clause would make operation legal\n'
                  f't.adb:{n + 1}:13: error: array type required in indexed component\n'
                  for n in (11, 14, 19, 22, 27, 30, 35, 40, 45))
    found = parse_messages(out)
    assert [(e["line"], e["also_at"]) for e in found["errors"]] == [(11, [14, 19, 22, 27, 30, 35, 40, 45]),
                                                                     (12, [15, 20, 23, 28, 31, 36, 41, 46])]
    assert found["errors"][0]["details"] == ["use clause would make operation legal"]
    assert "details" not in found["errors"][1] and "more_errors" not in found


SPEEDS_ADS = """package Speeds is
   type Speed_Class is (Invalid, Slow, Normal, Fast);
   -- Req: SRS-0160
   function Classify (Speed : Float) return Speed_Class;
end Speeds;
"""
SPEEDS_ADB = """package body Speeds is
   function Classify (Speed : Float) return Speed_Class is
   begin
      if Speed < 0.0 then
         return Invalid;
      elsif Speed < 50.0 then
         return Slow;
      elsif Speed <= 300.0 then
         return Normal;
      else
         return Fast;
      end if;
   end Classify;
end Speeds;
"""


@gnat
def test_gnat_on_the_test_a_small_model_wrote_and_on_the_model_of_tester_md(project):
    """The test of gemma4:12b for SRS-0160 does not build, for two reasons that the errors now
    give once each; the model of a test in examples/agents/tester.md builds and passes, and
    finds the wrong boundary."""
    import re
    ada, make = project
    (ada / "src" / "speeds.ads").write_text(SPEEDS_ADS)
    (ada / "src" / "speeds.adb").write_text(SPEEDS_ADB)
    tools = make(check=["gcc", "-c", "-gnatc", "{include}", "{file}"], tool_path=GNAT_PATH,
                 test=["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"])
    test = ada / "tests" / "test_speeds.adb"
    test.write_text("with Speeds;\nprocedure Test_Speeds is\n   -- Req: SRS-0160\nbegin\n" + "".join(
        f'   if Speeds.Classify({x}) /= Speeds.{c} then\n      raise Program_Error ("{x} should be {c}");\n   end if;\n'
        for x, c in (("-1.0", "Invalid"), ("49.9", "Slow"), ("300.0", "Normal"), ("300.1", "Fast"))) + "end Test_Speeds;\n")
    res = tools.run_test(str(test))
    assert res["built"] is False and len(res["errors"]) == 2, res
    visible, call = res["errors"]
    assert "not directly visible" in visible["message"] and len(visible["also_at"]) == 3
    assert any("use clause" in d for d in visible.get("details", [])), visible
    assert "array type required" in call["message"] and len(call["also_at"]) == 3

    model = re.search(r"A model of the test:\n\n((?:    .*\n?)+)", (Path(__file__).resolve().parents[1] / "examples" /
                                                                    "agents" / "tester.md").read_text())[1]
    test.write_text("\n".join(line[4:] for line in model.splitlines()) + "\n")
    assert tools.run_test(str(test))["passed"] is True
    (ada / "src" / "speeds.adb").write_text(SPEEDS_ADB.replace("Speed <= 300.0", "Speed < 300.0"))
    shutil.rmtree(tools.build_dir)             # gnatmake decides by time stamps: a change a moment after the
    res = tools.run_test(str(test))            # last build can go unseen, so build from scratch
    assert res["passed"] is False and "300.0 should be Normal" in res["output"], res


# --- the file tools of the same context: syntax first, check_code after ------------------------

FAKE_SYNTAX = '''import sys
from pathlib import Path
for i, line in enumerate(Path(sys.argv[-1]).read_text().splitlines(), 1):
    if "OPEN_BEGIN" in line:
        print(f"{Path(sys.argv[-1]).name}:{i}:4: error: \\"end;\\" expected for \\"begin\\" at line {i}")
        sys.exit(1)
'''


def test_a_change_that_breaks_the_syntax_is_not_made_and_each_change_is_checked(project, tmp_path, toml):
    from types import SimpleNamespace
    from atlas.code import CodeTools
    ada, make = project
    syntax = tmp_path / "bin1" / "syntax.py"
    syntax.write_text(FAKE_SYNTAX)
    build = make(extra=f'[sources.code.syntax_commands]\nadb = ["{sys.executable}", "{syntax}", "{{file}}"]\n')
    files = build.files
    build.hook()                                         # what CodeTools.for_context does in an agent
    f = str(ada / "src" / "tracks.adb")
    files.read_file(f)
    text = Path(f).read_text()
    broken = files.edit_file(f, "end Update;", "OPEN_BEGIN")      # the begin of Update is left open
    assert broken["error"].startswith("the change was not made: it breaks the syntax")
    assert broken["syntax_errors"][0]["message"].startswith('"end;" expected for "begin"')
    assert ">" in broken["around"] and Path(f).read_text() == text           # nothing was written
    done = files.edit_file(f, 'Logger.Log ("update");', 'Logger.Log ("updated");')
    assert done["replacements"] == 1 and done["check"] == {"ok": True}       # check_code, in the same answer
    Path(f).write_text(text.replace("procedure Update", "BROKEN procedure Update"))
    files.read_file(f)
    after = files.write_file(f, Path(f).read_text() + "\n")
    assert after["check"]["ok"] is False and after["check"]["errors"][0]["message"] == '"Trk" is undefined'
    # Python is checked with ast, with no command; a new file is written even if broken
    py = ada / "src" / "calc.py"
    py.write_text("def f():\n    return 1\n")
    files.read_file(str(py))
    assert "breaks the syntax" in files.edit_file(str(py), "return 1", "return (1")["error"]
    assert files.write_file(str(ada / "src" / "new.py"), "def g(:\n")["created"] is True
    # check_on_edit = false: no check in the answers
    quiet = make(extra="check_on_edit = false")
    quiet.hook()
    quiet.files.read_file(f)
    assert "check" not in quiet.files.write_file(f, Path(f).read_text() + "\n")
    # in an agent's context the hooks come with the file tools
    context = SimpleNamespace(cfg=load_config(toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\n')))
    tools = CodeTools.for_context(context)
    assert tools.syntax == context._build_tools.syntax_errors and tools.after_change is not None


@gnat
def test_gnat_rejects_a_begin_left_open(project):
    ada, make = project
    tools = make(check=["gcc", "-c", "-gnatc", "{include}", "{file}"], tool_path=GNAT_PATH,
                 extra='[sources.code.syntax_commands]\nadb = ["gcc", "-c", "-gnats", "{file}"]\n')
    tools.hook()
    f = str(ada / "src" / "tracks.adb")
    tools.files.read_file(f)
    text = Path(f).read_text()
    first_end = next(l.strip() for l in text.splitlines() if l.strip().startswith("end ") and l.strip() != "end;")
    broken = tools.files.edit_file(f, first_end, "null;")
    assert "breaks the syntax" in broken["error"] and Path(f).read_text() == text
    assert any("expected" in e["message"] or "missing" in e["message"] for e in broken["syntax_errors"])


def test_hints_come_with_the_errors_those_of_the_project_first(project, tmp_path):
    from atlas.code.build import HINTS, hint_for, load_hints
    from atlas.config import ConfigError
    ada, make = project
    (ada / "src" / "tracks.adb").write_text("BROKEN")
    error = make().check_code(str(ada / "src" / "tracks.adb"))["errors"][0]
    assert error["hint"].startswith("Trk is not declared or not visible here")         # hints.toml, of serie
    mine = tmp_path / "lessons.toml"
    mine.write_text('[[hint]]\nmatch = \'"(?P<name>Trk)" is undefined\'\nhint = "{name} is Track, in this project."\n')
    tools = make(extra=f'hints = ["{mine.as_posix()}"]')
    assert tools.check_code(str(ada / "src" / "tracks.adb"))["errors"][0]["hint"] == "Trk is Track, in this project."
    # a hint can match a detail of the message, and its groups go into the text
    hints = load_hints([HINTS])
    assert hint_for(hints, "no match here", ['possible misspelling of "Argument"']).startswith("The compiler knows Argument")
    assert hint_for(hints, "nothing to say") == ""
    mine.write_text('[[hint]]\nmatch = "(unclosed"\nhint = "x"\n')
    with pytest.raises(ConfigError, match="not a valid regular expression"):
        load_hints([mine])
    mine.write_text('[[hint]]\nmatch = "x"\n')
    with pytest.raises(ConfigError, match="needs match and hint"):
        load_hints([mine])


# --- the real declarations of the library ----------------------------------------------------

COMLIN = """package Ada.Command_Line is
   function Argument_Count return Natural;
   --  The number of arguments.

   function Argument (Number : Positive) return String;
   --  The argument at the position Number.

   type Exit_Status is new Integer;
   Success : constant Exit_Status;
private
   Hidden : Integer;
end Ada.Command_Line;
"""
LATIN = "package Ada.Characters.Latin_1 is\n   LF : constant Character := Character'Val (10);\nend Ada.Characters.Latin_1;\n"
OTHER = "package GNAT.Command_Line is\n   procedure Getopt (Switches : String);\nend GNAT.Command_Line;\n"


def test_library_spec_and_the_errors_that_name_what_does_not_exist(project, tmp_path):
    pytest.importorskip("tree_sitter_ada")
    ada, make = project
    lib = tmp_path / "adainclude"
    lib.mkdir()
    (lib / "a-comlin.ads").write_text(COMLIN)          # GNAT shortens the file names: found by the unit
    (lib / "a-chlat1.ads").write_text(LATIN)
    (lib / "g-comlin.ads").write_text(OTHER)
    tools = make(extra=f'library = ["{tmp_path.as_posix()}/adain*"]')
    spec = tools.library_spec("Ada.Command_Line")
    assert spec["unit"] == "Ada.Command_Line" and spec["file"] == "a-comlin.ads"
    assert [(d["kind"], d["name"]) for d in spec["declarations"]] == [
        ("function", "Argument_Count"), ("function", "Argument"), ("type", "Exit_Status"), ("constant", "Success")]
    assert spec["declarations"][1]["text"] == "function Argument (Number : Positive) return String"
    assert spec["declarations"][1]["doc"] == "The argument at the position Number."          # the private part is not
    assert [d["name"] for d in tools.library_spec("ada.command_line", name="count")["declarations"]] == ["Argument_Count"]
    assert "similar: ada.command_line" in tools.library_spec("Ada.Comand_Line")["error"]
    assert tools.library_spec("Tracks")["unit"] == "Tracks"                    # a spec of the project too
    # an error that names what does not exist brings what does
    errors = tools._hinted([{"message": '"Arguments" not declared in "Command_Line"'},
                            {"message": '"LF" is undefined'}, {"message": '"Nothing" is undefined'}],
                           text="with Ada.Command_Line;\nprocedure Main is begin null; end Main;\n")
    assert errors[0]["declared_in"]["unit"] == "Ada.Command_Line"            # not GNAT.Command_Line: the with says so
    assert errors[0]["declared_in"]["similar"][0] == "function Argument (Number : Positive) return String"
    assert errors[0]["hint"].startswith("Command_Line has no Arguments")
    assert errors[1]["found_in"] == ["Ada.Characters.Latin_1: LF : constant Character := Character'Val (10);"]
    assert "found_in" not in errors[2]
    with_gnat = tools._hinted([{"message": '"Getop" not declared in "Command_Line"'}], text="with GNAT.Command_Line;")
    assert with_gnat[0]["declared_in"]["unit"] == "GNAT.Command_Line"


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
