"""The Python analyzer: functions, methods, classes, imports and requirement tags, and Python
tests in the same graph as Ada ones."""
from atlas.code import AdaAnalyzer, PythonAnalyzer
from atlas.graph.build import build_code_graph

SOURCE = b'''"""Tests of the session commands. Req: SRS-0900 (module level: cites nothing)"""
import os
import socket
from . import helpers

PORT = int(os.environ.get("SERVER_PORT", "5555"))
timeout = 2.0


class Client:
    """A line client. Req: SRS-0001"""

    def send(self, line):
        self.sock.sendall(line.encode() + b"\\n")
        return self.read()

    def read(self):
        return helpers.read_line(self.sock)


# Req: SRS-0002, SRS-0003
@helpers.slow
def test_session():
    """Opens a session and closes it."""
    client = Client()
    assert client.send("HELLO tester") == "OK HELLO"
    # Req: SRS-0004 (inside the body)

    def step(command):
        # Req: SRS-0099 belongs to the nested function only
        return client.send(command)
    print(step("BYE"))


if __name__ == "__main__":
    test_session()
'''


def test_functions_methods_classes_and_their_tags(tmp_path):
    f = tmp_path / "tests" / "python" / "test_session.py"
    f.parent.mkdir(parents=True)
    unit = PythonAnalyzer({"roots": [str(tmp_path / "tests")]}).analyze_file(f, SOURCE)
    assert unit.unit == "python.test_session" and unit.language == "python"
    assert unit.imports == [".", "os", "socket"]
    subs = {s["name"]: s for s in unit.subprograms}
    assert list(subs) == ["Client.send", "Client.read", "test_session", "test_session.step"]
    session = subs["test_session"]
    assert session["reqs"] == ["SRS-0002", "SRS-0003", "SRS-0004"]        # above, and in the body; not step's
    assert session["doc"] == "Req: SRS-0002, SRS-0003\nOpens a session and closes it."
    assert session["calls"] == ["Client", "client.send", "step"]           # not print, not the calls of step
    assert (session["start_line"], session["end_line"]) == (22, 32)        # from the decorator
    assert subs["test_session.step"]["reqs"] == ["SRS-0099"]
    assert subs["Client.read"]["calls"] == ["helpers.read_line"]
    assert [(t["name"], t["reqs"]) for t in unit.types] == [("Client", ["SRS-0001"])]
    assert [(v["name"], v["constant"]) for v in unit.variables] == [("PORT", True), ("timeout", False)]


def test_a_file_that_does_not_parse_is_an_empty_unit(tmp_path, caplog):
    f = tmp_path / "half_written.py"
    unit = PythonAnalyzer({}).analyze_file(f, b"def broken(:\n    pass\n")
    assert unit.unit == "half_written" and unit.subprograms == []
    assert "does not parse as Python" in caplog.text


def test_python_and_ada_tests_verify_the_same_requirement(tmp_path):
    ada = tmp_path / "tests" / "ada" / "test_session.adb"
    py = tmp_path / "tests" / "python" / "test_session.py"
    units = [AdaAnalyzer({}).analyze_file(ada, b"procedure Test_Session is\n   -- Req: SRS-0002\nbegin\n   null;\n"
                                                b"end Test_Session;\n"),
             PythonAnalyzer({"roots": [str(tmp_path / "tests")]}).analyze_file(py, SOURCE)]
    G, stats = build_code_graph(units, test_patterns=["**/tests/**"])
    assert {n for n, _ in G.in_edges("SRS-0002")} == {"test_session.test_session", "python.test_session.test_session"}
    assert all(G.nodes[n]["kind"] == "test" for n, _ in G.in_edges("SRS-0002"))
    assert stats["test_files"] == 2
