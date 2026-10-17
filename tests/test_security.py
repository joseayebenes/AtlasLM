"""Nothing leaves the workstation except to the configured servers: every connection that a
whole run, a search, a cited answer, a trace and an agent open goes to the API of [llm] and
[embeddings] (here, the fake one). Python's audit hooks see every socket of the process,
in every thread and whatever library opens it.

And nothing of a local-only source (model = "none") goes even there: the fake API keeps the
body of every request, and none has their text."""
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


ANNEX = """# Annex Q

1 Quokka beacon

Req: SRS-0977 The quokka beacon shall blink every 7 seconds while the wombat relay is armed.

2 Wombat relay

The wombat relay forwards the quokka telemetry to the ground station.
"""
LOCAL_TEXTS = ("quokka", "wombat", "Annex Q",                     # the local-only document
               "mission system shall provide",                  # the local-only DOORS module
               "Removes track I from the database")             # a comment of the local-only code


def test_local_only_sources_send_nothing(tmp_path, samples, docs_samples, fake, fake_stats, capsys):
    """With a document, a DOORS module and the code in model = "none", a whole run, searches,
    answers, a verification and the reports send no request whose body has their text: the
    fake API keeps every body it receives."""
    for module in ("markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    import json
    from atlas.cli import comply, coverage, impact, verify
    from atlas.graph import KnowledgeGraph
    from atlas.outbound import LocalOnly, sent_summary
    url, control = fake
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    bodies = tmp_path / "bodies.jsonl"
    control.write_text(json.dumps({"bodies": str(bodies)}))
    annex = tmp_path / "client"
    annex.mkdir()
    (annex / "annex_q.md").write_text(ANNEX, encoding="utf-8")
    path = tmp_path / "atlas.toml"
    path.write_text(f'''
[project]
workspace = "{(tmp_path / 'ws').as_posix()}"
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
name = "manuals"
paths = ["{(docs_samples / 'hmi_manual.md').as_posix()}"]
[[sources.docs]]
name = "client"
paths = ["{annex.as_posix()}"]
model = "none"
[sources.doors]
adapter = "exchange"
local_only = ["SSS_MISSION"]
[sources.doors.exchange]
path = "{(samples / 'requirements.sqlite').as_posix()}"
[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-\\\\d{{4}}$"
inline_reference_regex = "\\\\b(?:SRS|SSS)-\\\\d{{4}}\\\\b"
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
model = "none"
[agents]
model = "fake"
''', encoding="utf-8")
    cfg = load_config(path)

    _AUDIT["seen"].clear()
    _AUDIT["on"] = True
    try:
        assert run(cfg) == 0
        assert search(cfg, "SRS-0977") == 0
        assert ask(cfg, "What does SRS-0977 ask for?") == 0
        assert ask(cfg, "Summarise the annex", document="annex_q.md", whole=True) == 0
        assert ask(cfg, "When does the HMI raise an alarm?") == 0
        assert verify(cfg, "SRS-0102") == 0 and verify(cfg, "SSS-0001") == 0
        assert comply(cfg, "SRS-0004") == 0
        assert impact(cfg, "Logger.Log") == 0 and coverage(cfg, "SSS_MISSION") == 0
        if importlib.util.find_spec("google.adk"):
            agent(cfg, "assistant", "What does SRS-0977 ask for?")
    finally:
        _AUDIT["on"] = False
    out = capsys.readouterr().out
    assert "quokka beacon shall blink" in out            # the person sees it: search and the cited sources
    assert "[local] annex_q.md" in out and "not sent to the model" in out
    connects = {a for e, a in _AUDIT["seen"] if e == "socket.connect"}
    assert connects == {("127.0.0.1", port)}, connects

    sent = bodies.read_text(encoding="utf-8")
    assert sent.count("\n") > 10 and "raise an alarm" in sent and '"input"' in sent    # the rest did go
    leaked = [t for t in LOCAL_TEXTS if t in sent]
    assert not leaked, leaked
    view = KnowledgeGraph.from_config(cfg).for_model(LocalOnly.from_config(cfg))     # what any agent tool sees
    seen = json.dumps([[n, d] for n, d in view.G.nodes(data=True)], default=str)
    assert not [t for t in LOCAL_TEXTS if t in seen] and "SRS-0977" in seen

    # and the record says what went, and that nothing of the local-only sources did
    rows = {(r["model"], r["source"], r["kind"]) for r in sent_summary(cfg.workspace / "atlas.db")}
    assert {("fake", "docs:manuals", "sections"), ("embeddinggemma", "docs:manuals", "texts"),
            ("embeddinggemma", "doors:SRS_TMS", "texts"), ("fake", "docs:manuals", "answers"),
            ("fake", "docs:manuals", "judgements"), ("fake", "doors:SRS_TMS", "reports")} <= rows, rows
    assert not [r for r in rows if r[1] in ("docs:client", "doors:SSS_MISSION", "code")], rows
