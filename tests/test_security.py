"""Nothing leaves the workstation except to the configured servers: every connection that a
whole run, a search, a cited answer, a trace and an agent open goes to the API of [llm] and
[embeddings] (here, the fake one). Python's audit hooks see every socket of the process,
in every thread and whatever library opens it."""
import importlib.util
import sys

import pytest

from atlas.cli import agent, ask, run, search, trace
from atlas.config import load_config

_AUDIT = {"on": False, "seen": []}


def _hook(event: str, args: tuple) -> None:
    if _AUDIT["on"] and event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
        _AUDIT["seen"].append((event, args[1] if event == "socket.connect" else args[0]))


sys.addaudithook(_hook)          # it cannot be removed: it only records while _AUDIT["on"]


def test_nothing_leaves_but_to_the_configured_api(toml, samples, docs_samples, fake, fake_stats, capsys):
    for module in ("markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    url, _ = fake
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    cfg = load_config(toml(f'''
[llm]
base_url = "{url}"
model = "fake"
[embeddings]
model = "embeddinggemma"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "code", "docs", "graph", "index"]
workers = 2
[[sources.docs]]
name = "standards"
paths = ["{(docs_samples / 'spec_with_bookmarks.pdf').as_posix()}"]
analysis = "regex"
[[sources.docs]]
name = "manuals"
paths = ["{(docs_samples / 'hmi_manual.md').as_posix()}"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
[agents]
model = "fake"
'''))
    def chats() -> int:                    # the test asking the fake API is not recorded
        _AUDIT["on"] = False
        try:
            return fake_stats()["chat"]
        finally:
            _AUDIT["on"] = True

    _AUDIT["seen"].clear()
    _AUDIT["on"] = True
    try:
        assert run(cfg) == 0
        assert search(cfg, "track deletion") == 0
        assert ask(cfg, "When does the HMI raise an alarm?") == 0
        assert trace(cfg, "SRS-0004") == 0
        if importlib.util.find_spec("google.adk"):              # ADK, its sessions and AtlasLlm too
            before = chats()
            agent(cfg, "assistant", "Which test verifies SRS-0004?")
            assert chats() > before
    finally:
        _AUDIT["on"] = False
    capsys.readouterr()
    connects = [a for e, a in _AUDIT["seen"] if e == "socket.connect"]
    names = {a for e, a in _AUDIT["seen"] if e == "socket.getaddrinfo"}
    assert connects and set(connects) == {("127.0.0.1", port)}, set(connects)
    assert names <= {"127.0.0.1"}, names
    assert not [a for e, a in _AUDIT["seen"] if e == "urllib.Request"]
