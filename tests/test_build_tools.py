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

    def make(check=None, test=None, tool_path=None, timeout=None) -> BuildTools:
        cfg = load_config(toml(f'''
[sources.code]
roots = ["{ada.as_posix()}"]
test_patterns = ["**/tests/**"]
check_command = {check if check is not None else [sys.executable, str(cc), "{include}", "{file}"]}
test_command = {test if test is not None else [sys.executable, str(cc), "{include}", "{file}", "-o", "{exe}"]}
tool_path = {tool_path or []}
command_timeout = {timeout or 30}
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
    assert bad["errors"] == [{"file": "tracks.adb", "line": 3, "column": 7, "message": '"Trk" is undefined'}]
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


def test_offered_by_the_mcp_server(project, toml):
    from atlas.mcp import default_tools
    ada, make = project
    tools = make()
    cfg = load_config(toml(f'[sources.code]\nroots = ["{ada.as_posix()}"]\ncheck_command = ["cc", "{{file}}"]\n'))
    assert "check_code" in default_tools(cfg) and "run_test" not in default_tools(cfg)     # run_test runs code
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
