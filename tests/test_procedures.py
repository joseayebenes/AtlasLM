"""atlas tests: the steps of the test procedures as tests in each language, without a model."""
from types import SimpleNamespace

import networkx as nx
import pytest

from atlas.code.procedures import generate_tests, literal, slugs, split_steps
from atlas.config import ConfigError, load_config

NOMINAL = """Objective: a client identifies itself and closes the session, VERSION 1.0 included.
1. Connect.
2. Send `HELLO tester-01`; expect `OK HELLO tester-01 VERSION 1.0`.
3. Send `SAY "hi"`; expect `OK SAY "hi"`.
4. Send a line of 200 characters `A`; expect `ERR 413 LINE_TOO_LONG`.
5. Check that the server closed the connection.

Notes after a blank line are not a step."""

STEPS = [
    {"match": r"Connect( again)?", "ada": "Connect;", "python": "client.connect()"},
    {"match": r"Send `(?P<command>[^`]*)`; expect `(?P<expected>[^`]*)`",
     "ada": "Expect ({n}, {command:str}, {expected:str});", "python": "client.expect({n}, {command:str}, {expected:str})"},
    {"match": r"Send a line of (?P<count>\d+) characters `(?P<char>.)`; expect `(?P<expected>[^`]*)`",
     "ada": "Expect ({n}, (1 .. {count} => '{char}'), {expected:str});",
     "python": "client.expect({n}, {char:str} * {count}, {expected:str})"},
    {"match": r"Check that the server closed the connection",
     "ada": "--  {n}. {text}\nExpect_Closed ({n});", "python": "client.expect_closed({n})"},
]
ADA = """--  Generated from {id} ({title}). Verifies {verifies}.
with Test_Client; use Test_Client;

--  Req: {id}
procedure Test_{Slug} is
begin
   {steps}
end Test_{Slug};
"""
PYTHON = """from test_client import Client


# Req: {id}
def test_{slug}():
    client = Client()
    {steps}
    print({"ok": True})
"""


def graph(procedures: dict) -> SimpleNamespace:
    """What generate_tests uses of a KnowledgeGraph: nodes, list_requirements and trace."""
    G = nx.DiGraph()
    for req_id, (heading, text, verifies) in procedures.items():
        G.add_node(req_id, kind="requirement", module="STP_X", heading=heading, text=text, verifies=verifies)

    def list_requirements(prefix, module, limit):
        if module != "STP_X":
            return {"error": f"module '{module}' is not in the graph", "modules": ["STP_X"]}
        return {"items": [{"id": n} for n in G.nodes], "count": len(G)}

    def trace(req_id):
        return {"links_out": [{"id": v, "type": "verifies"} for v in G.nodes[req_id]["verifies"]]}
    return SimpleNamespace(G=G, list_requirements=list_requirements, trace=trace)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "test.adb").write_text(ADA)
    (tmp_path / "templates" / "test.py").write_text(PYTHON)
    settings = {"languages": {"ada": {"path": "tests/ada/test_{slug}.adb", "template": "templates/test.adb"},
                              "python": {"path": "tests/python/test_{slug}.py", "template": "templates/test.py"}},
                "steps": STEPS}
    return tmp_path, settings


def test_the_numbered_steps_with_or_without_line_breaks():
    preamble, steps = split_steps(NOMINAL)
    assert preamble.startswith("Objective:") and "VERSION 1.0" in preamble
    assert [n for n, _ in steps] == [1, 2, 3, 4, 5]
    assert steps[1] == (2, "Send `HELLO tester-01`; expect `OK HELLO tester-01 VERSION 1.0`.")
    assert steps[4] == (5, "Check that the server closed the connection.")      # not the notes after it
    joined = " ".join(NOMINAL.split("\n\n")[0].split("\n"))                         # as the store keeps it
    assert split_steps(joined)[1] == steps
    assert split_steps("Send `PING`.") == ("Send `PING`.", [])
    assert slugs("STP-0101") == {"slug": "stp_0101", "Slug": "Stp_0101"}
    assert literal('SAY "hi"', "ada") == '"SAY ""hi"""' and literal('SAY "hi"', "python") == '"SAY \\"hi\\""'


def test_a_test_per_procedure_and_language(project):
    base, settings = project
    g = graph({"STP-0101": ("Nominal session", NOMINAL, ["SRS-0101", "SRS-0110"])})
    res = generate_tests(g, "STP_X", settings, base)
    assert (res["written"], res["unchanged"], res["errors"]) == (2, 0, 0)
    ada = (base / "tests" / "ada" / "test_stp_0101.adb").read_text()
    assert ada == '''--  Generated from STP-0101 (Nominal session). Verifies SRS-0101, SRS-0110.
with Test_Client; use Test_Client;

--  Req: STP-0101
procedure Test_Stp_0101 is
begin
   Connect;
   Expect (2, "HELLO tester-01", "OK HELLO tester-01 VERSION 1.0");
   Expect (3, "SAY ""hi""", "OK SAY ""hi""");
   Expect (4, (1 .. 200 => 'A'), "ERR 413 LINE_TOO_LONG");
   --  5. Check that the server closed the connection
   Expect_Closed (5);
end Test_Stp_0101;
'''
    python = (base / "tests" / "python" / "test_stp_0101.py").read_text()
    assert '    client.expect(3, "SAY \\"hi\\"", "OK SAY \\"hi\\"")\n' in python
    assert '    client.expect(4, "A" * 200, "ERR 413 LINE_TOO_LONG")\n' in python
    assert 'print({"ok": True})' in python                       # braces of the code are left alone
    compile(python, "test_stp_0101.py", "exec")
    again = generate_tests(g, "STP-0101", settings, base)            # one procedure, nothing changed
    assert (again["written"], again["unchanged"]) == (0, 2)
    assert [f["status"] for f in again["procedures"][0]["files"]] == ["unchanged", "unchanged"]


def test_a_step_without_pattern_writes_nothing_of_that_procedure(project):
    base, settings = project
    g = graph({"STP-0101": ("Nominal", NOMINAL, []),
               "STP-0102": ("Wait", "1. Connect.\n2. Wait 2 seconds.\n", []),
               "STP-0103": ("Empty", "Only an objective.", [])})
    res = generate_tests(g, "STP_X", settings, base)
    assert res["errors"] == 2 and res["written"] == 2
    by_id = {p["id"]: p for p in res["procedures"]}
    assert by_id["STP-0102"]["errors"] == ["ada: no pattern of [[tests.steps]] matches step 2: 'Wait 2 seconds'",
                                           "python: no pattern of [[tests.steps]] matches step 2: 'Wait 2 seconds'"]
    assert by_id["STP-0102"]["files"] == [] and not (base / "tests" / "ada" / "test_stp_0102.adb").exists()
    assert "no numbered steps" in by_id["STP-0103"]["errors"][0]
    bad = {**settings, "steps": [{"match": "Connect", "ada": "Connect ({port});", "python": "client.connect()"}]}
    [p] = generate_tests(graph({"STP-0104": ("C", "1. Connect.", [])}), "STP-0104", bad, base)["procedures"]
    assert p["errors"] == ["ada: [[tests.steps]] 1, ada: {port} is not n, text or a named group of its match"]
    assert "not in the graph" in generate_tests(g, "STP_Y", settings, base)["error"]
    assert "template" in generate_tests(g, "STP_X", {**settings, "languages": {
        "ada": {"path": "x", "template": "missing.adb"}}}, base)["error"]


def test_the_configuration_of_tests(toml):
    cfg = load_config(toml('[tests.languages.ada]\npath = "t/{slug}.adb"\ntemplate = "t.adb"\n'
                           '[[tests.steps]]\nmatch = "Connect"\nada = "Connect;"\n'))
    assert cfg["tests"]["steps"][0]["ada"] == "Connect;"
    assert load_config(toml("")).get("tests") == {"languages": {}, "steps": []}
    with pytest.raises(ConfigError, match="not languages"):
        load_config(toml('[tests.languages.ada]\npath = "a"\ntemplate = "b"\n[[tests.steps]]\nmatch = "x"\njava = "y"\n'))
    with pytest.raises(ConfigError, match="regular expression"):
        load_config(toml('[[tests.steps]]\nmatch = "Send (["\n'))
    with pytest.raises(ConfigError, match="needs path and template"):
        load_config(toml('[tests.languages.ada]\npath = "a"\n'))
