import json
from atlas.cli import run, status
from atlas.config import load_config


def test_incremental_run_with_doors_and_code(toml, samples, capsys):
    cfg = load_config(toml(f'''
[pipeline]
steps = ["doors", "code"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
languages = ["ada"]
'''))
    import pytest
    pytest.importorskip("tree_sitter_ada")
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"]["doors.requirements"] == 3 and rep["processed"]["code.parse"] >= 4
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {} and rep["skipped"]["code.parse"] >= 4
    assert rep["profile"]["code.parse"]["ran"] == 0 and rep["profile"]["code.parse"]["units"] >= 4
    assert status(cfg) == 0
    out = capsys.readouterr().out
    assert "last run:" in out and "previous runs:" in out and "code.parse" in out


def test_docs_step_is_incremental_retries_and_prunes(toml, docs_samples, tmp_path, fake, fake_chats, capsys):
    import shutil
    import pytest
    from atlas.cli import sections
    from atlas.config import ConfigError
    from atlas.store import Store
    pytest.importorskip("markitdown")
    url, control = fake
    docs = tmp_path / "docs"
    docs.mkdir()
    for f in ("spec_with_bookmarks.pdf", "hmi_manual.md"):
        shutil.copy(docs_samples / f, docs / f)
    sources = f'''
[llm]
base_url = "{url}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["docs"]
[[sources.docs]]
name = "standards"
paths = ["{docs.as_posix()}"]
patterns = ["*.pdf"]
analysis = "regex"
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
patterns = ["*.md"]
lang = "Spanish"
'''
    cfg = load_config(toml(sources))
    ws = cfg.workspace

    def go(expected_rc=0):
        assert run(cfg) == expected_rc
        return json.loads(capsys.readouterr().out)

    rep = go()
    assert rep["processed"] == {"docs.process": 2}
    assert (ws / "docs" / "standards" / "spec_with_bookmarks_pdf" / "requirements.md").exists()
    hmi = ws / "docs" / "manuals" / "hmi_manual_md"
    assert "SRS-0102" in (hmi / "requirements.md").read_text()

    # nothing changed: nothing processed, no request to the model
    chats = fake_chats()
    rep = go()
    assert rep["processed"] == {} and rep["skipped"] == {"docs.process": 2} and fake_chats() == chats

    # another extractor gives another text: the PDF is processed again, the Markdown is not
    assert run(load_config(toml(sources + '[plugins]\ndisabled = ["extractor:pymupdf"]\n'))) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {"docs.process": 1} and rep["skipped"] == {"docs.process": 1}
    rep = go()
    assert rep["processed"] == {"docs.process": 1} and fake_chats() == chats

    # a changed document whose analysis fails: output written, unit in error, retried next time
    (docs / "hmi_manual.md").write_text((docs / "hmi_manual.md").read_text().replace("30 days", "60 days"))
    control.write_text(json.dumps({"prose": True}))
    rep = go(expected_rc=1)
    assert rep["errors"] == {"docs.process": 1} and "analysis pending" in (hmi / "index.md").read_text()
    control.write_text("{}")
    rep = go()
    assert rep["processed"] == {"docs.process": 1} and "60 days" in (hmi / "requirements.md").read_text()

    # a deleted document is pruned with its output
    (docs / "spec_with_bookmarks.pdf").unlink()
    rep = go()
    assert rep["pruned"] == {"docs.process": 1}
    assert not (ws / "docs" / "standards" / "spec_with_bookmarks_pdf").exists()
    assert Store(ws).counts()["documents"] == 1

    assert sections(cfg) == 0
    assert "[manuals]" in capsys.readouterr().out
    with pytest.raises(ConfigError, match="analysis"):
        run(load_config(toml(sources.replace('analysis = "regex"', 'analysis = "gpt"'))))


def test_index_step_search_and_ask(toml, docs_samples, tmp_path, fake, fake_stats, capsys):
    import shutil
    import pytest
    from atlas.cli import ask, main, search, status
    from atlas.config import ConfigError
    pytest.importorskip("markitdown")
    url, control = fake
    docs = tmp_path / "docs"
    docs.mkdir()
    for f in ("spec_with_bookmarks.pdf", "hmi_manual.md"):
        shutil.copy(docs_samples / f, docs / f)
    extra = f'''
[llm]
base_url = "{url}"
model = "fake"
[embeddings]
model = "embeddinggemma"
batch = 8
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["docs", "index"]
[[sources.docs]]
name = "standards"
paths = ["{docs.as_posix()}"]
patterns = ["*.pdf"]
analysis = "regex"
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
patterns = ["*.md"]
'''
    path = toml(extra)
    cfg = load_config(path)

    def go(expected_rc=0):
        assert run(cfg) == expected_rc
        return json.loads(capsys.readouterr().out)

    rep = go()
    assert rep["processed"] == {"docs.process": 2, "index.documents": 2}
    stats = fake_stats()
    rep = go()
    assert rep["processed"] == {} and rep["skipped"]["index.documents"] == 2
    assert fake_stats()["emb_texts"] == stats["emb_texts"]

    assert search(cfg, "SRS-0102 alarm") == 0
    out = capsys.readouterr().out
    assert "[1] hmi_manual.md › 2 Alarms, p. 1 (requirement)" in out
    assert ask(cfg, "When does the HMI raise an alarm?") == 0
    out = capsys.readouterr().out
    assert "[1]" in out and "Sources:" in out and "003-2-alarms.md" in out
    with pytest.raises(SystemExit) as e:              # the TEXT [atlas.toml] form of the command line
        main(["search", "keep logs", str(path)])
    assert e.value.code == 0 and "hmi_manual.md" in capsys.readouterr().out

    # a changed document: reprocessed and reindexed; a failed embeddings batch is retried
    (docs / "hmi_manual.md").write_text((docs / "hmi_manual.md").read_text().replace("30 days", "60 days"))
    control.write_text(json.dumps({"emb_fail_if": ["60 days"]}))
    rep = go(expected_rc=1)
    assert rep["processed"]["docs.process"] == 1 and rep["errors"] == {"index.documents": 1}
    control.write_text("{}")
    rep = go()
    assert rep["processed"] == {"index.documents": 1}
    search(cfg, "keep logs for 60 days")
    assert "60 days" in capsys.readouterr().out

    # a deleted document leaves the index too
    (docs / "spec_with_bookmarks.pdf").unlink()
    rep = go()
    assert rep["pruned"] == {"docs.process": 1, "index.documents": 1}
    assert status(cfg) == 0
    assert "'documents': 1, 'records'" in capsys.readouterr().out
    with pytest.raises(ConfigError, match="kinds"):
        run(load_config(toml(extra + '[index]\nkinds = ["chunk", "pages"]\n')))


def test_graph_step_and_report(toml, samples, tmp_path, capsys):
    import shutil
    import pytest
    from atlas.cli import graph, status
    from atlas.config import ConfigError
    from atlas.graph import KnowledgeGraph
    pytest.importorskip("tree_sitter_ada")
    ada = tmp_path / "ada"
    shutil.copytree(samples / "ada", ada)
    extra = f'''
[pipeline]
steps = ["code", "graph"]
[sources.code]
roots = ["{ada.as_posix()}"]
languages = ["ada"]
'''
    cfg = load_config(toml(extra))

    def go():
        assert run(cfg) == 0
        return json.loads(capsys.readouterr().out)

    assert go()["processed"] == {"code.parse": 5, "graph.build": 1}
    exported = cfg.workspace / "graph"
    assert (exported / "atlas.graphml").exists() and (exported / "atlas.json").exists()
    assert go()["skipped"] == {"code.parse": 5, "graph.build": 1}

    # a new call in one file: that file is analysed again and the graph rebuilt
    body = ada / "src" / "logger.adb"
    body.write_text(body.read_text().replace("Counter := Counter + 1;", "Counter := Counter + 1;\n      Log (\"id\");"))
    assert go()["processed"] == {"code.parse": 1, "graph.build": 1}
    g = KnowledgeGraph.from_config(cfg)
    assert [i["id"] for i in g.callers("logger.log")["items"]] == ["logger.next_id", "tracks.delete_track",
                                                                   "tracks.update"]
    assert graph(cfg) == 0
    out = capsys.readouterr().out
    assert "6 subprograms" not in out and "5 subprograms" in out and "ada.text_io.put_line" in out
    assert status(cfg) == 0 and "graph: {'nodes': 21" in capsys.readouterr().out

    # without code, the graph goes too
    shutil.rmtree(ada / "src")
    shutil.rmtree(ada / "tests")
    assert go()["pruned"] == {"code.parse": 5, "graph.build": 1}
    assert not list(exported.glob("atlas.*")) and graph(cfg) == 1
    with pytest.raises(ConfigError, match="exports"):
        run(load_config(toml(extra + '[graph]\nexports = ["svg"]\n')))


def test_graph_of_doors_code_and_documents(toml, samples, docs_samples, tmp_path, capsys):
    """The three parts of the graph, each rebuilt only when what it is built from changes,
    and the traceability commands."""
    import shutil
    import pytest
    from atlas.cli import graph, matrix, trace
    from atlas.graph import KnowledgeGraph
    pytest.importorskip("tree_sitter_ada")
    pytest.importorskip("markitdown")
    ada, docs = tmp_path / "ada", tmp_path / "docs"
    shutil.copytree(samples / "ada", ada)
    docs.mkdir()
    shutil.copy(docs_samples / "hmi_manual.md", docs / "hmi_manual.md")
    extra = f'''
[pipeline]
steps = ["doors", "code", "docs", "graph"]
[sources.code]
roots = ["{ada.as_posix()}"]
test_patterns = ["**/tests/**"]
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
patterns = ["*.md"]
analysis = "regex"
[graph]
communities = true
'''
    cfg = load_config(toml(extra))

    def go(config=cfg):
        assert run(config) == 0
        return json.loads(capsys.readouterr().out)

    assert go()["processed"] == {"doors.requirements": 3, "code.parse": 5, "docs.process": 1, "graph.build": 3}
    assert go()["skipped"]["graph.build"] == 3
    g = KnowledgeGraph.from_config(cfg)
    assert g.trace("SRS-0004")["verified_by"] == [{"id": "test_tracks.test_tracks", "how": "tag"}]
    assert g.trace("SRS-0102")["documents"][0]["title"] == "2 Alarms"        # SRS_TMS_OLD and the manual
    assert g.G.nodes["logger.log"]["community"] >= 0 and g.build_stats["code"]["communities"] >= 1

    manual = docs / "hmi_manual.md"                     # a document changes: only its part of the graph
    manual.write_text(manual.read_text().replace("30 days", "60 days"))
    assert go()["processed"] == {"docs.process": 1, "graph.build": 1}
    assert go(load_config(toml(extra.replace('["**/tests/**"]', "[]"))))["processed"] == \
        {"code.parse": 5, "graph.build": 3}            # what a test is changes the whole graph
    assert KnowledgeGraph.from_config(cfg).G.nodes["test_tracks.test_tracks"]["kind"] == "subprogram"
    go()

    assert graph(cfg) == 0
    out = capsys.readouterr().out
    assert "1 tests" in out and "DOORS of" in out and "3 requirements stated" in out
    assert "requirements without code" in out and "    1  cited in code not in doors" in out   # SRS-0103
    assert trace(cfg, "srs-0004") == 0 and '"implemented_by"' in capsys.readouterr().out
    assert trace(cfg, "SRS-9999") == 1
    capsys.readouterr()
    assert matrix(cfg, "SRS_TMS") == 0 and "40 requirements: 4 with code, 1 with tests" in capsys.readouterr().out
    written = cfg.workspace / "graph" / "matrix-SRS_TMS.csv"
    assert written.read_text().splitlines()[0] == "id,text,status,verification,derived,code,tests,covering_tests," \
                                                  "documents"
    assert "| SRS-0004 |" in written.with_suffix(".md").read_text()
    assert matrix(cfg, "NOPE") == 1 and "SRS_TMS, SRS_TMS_OLD, SSS_MISSION" in capsys.readouterr().out
    assert matrix(cfg, "*") == 0 and (cfg.workspace / "graph" / "matrix-all.md").exists()
    capsys.readouterr()

    manual.unlink()                                     # no documents: their part of the graph goes
    assert go()["pruned"] == {"docs.process": 1, "graph.build": 1}
    assert not [n for n, d in KnowledgeGraph.from_config(cfg).G.nodes(data=True) if d["kind"] == "section"]
    assert (cfg.workspace / "graph" / "atlas.graphml").exists()


def test_doors_requirements_are_indexed(toml, fake, fake_stats, capsys):
    from atlas.cli import search
    extra = f'''
[llm]
base_url = "{fake[0]}"
model = "fake"
[embeddings]
model = "embeddinggemma"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "index"]
'''
    cfg = load_config(toml(extra))

    def go(config=cfg):
        assert run(config) == 0
        return json.loads(capsys.readouterr().out)

    assert go()["processed"] == {"doors.requirements": 3, "index.requirements": 3}
    embedded = fake_stats()["emb_texts"]
    assert go()["skipped"] == {"doors.requirements": 3, "index.requirements": 3}
    assert fake_stats()["emb_texts"] == embedded                    # nothing embedded again
    assert search(cfg, "srs-0004") == 0 and "SRS_TMS (requirement)" in capsys.readouterr().out
    assert go(load_config(toml(extra + '[index]\nkinds = ["overview"]\n')))["pruned"] == {"index.requirements": 3}


def test_step_workers_and_documents_with_the_same_name_in_parallel(toml, docs_samples, tmp_path, capsys):
    import shutil
    import pytest
    from atlas.config import ConfigError
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        shutil.copy(docs_samples / "hmi_manual.md", tmp_path / d / "manual.md")
    (tmp_path / "b" / "manual.md").write_text((tmp_path / "a" / "manual.md").read_text().replace("30 days", "60 days"))
    sources = f'''
[pipeline]
steps = ["docs"]
workers = 1
step_workers = {{ docs = 2 }}
[[sources.docs]]
name = "manuals"
paths = ["{(tmp_path / 'a').as_posix()}", "{(tmp_path / 'b').as_posix()}"]
analysis = "regex"
'''
    cfg = load_config(toml(sources))
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {"docs.process": 2} and rep["profile"]["docs.process"]["workers"] == 2
    outs = sorted(p.name for p in (cfg.workspace / "docs" / "manuals").iterdir())
    assert len(outs) == 2 and outs[0] == "manual_md" and outs[1].startswith("manual_md-")    # never the same folder
    texts = {(cfg.workspace / "docs" / "manuals" / o / "requirements.md").read_text().count("60 days") for o in outs}
    assert texts == {0, 1}
    for bad, message in (("graph = 2", "one by one"), ("indexes = 2", "unknown step"), ("docs = 0", "positive")):
        with pytest.raises(ConfigError, match=message):
            run(load_config(toml(sources.replace("docs = 2", bad))))


def test_init_writes_a_template_that_loads_and_runs(tmp_path, capsys, monkeypatch):
    import pytest
    from atlas import __version__
    from atlas.cli import main
    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    for argv, code in ((["init"], 0), (["init"], 1), (["--version"], 0)):     # init never overwrites
        with pytest.raises(SystemExit) as exit:
            main(argv)
        assert exit.value.code == code
    assert capsys.readouterr().out.strip().endswith(f"atlas {__version__}")
    cfg = load_config(tmp_path / "atlas.toml")
    assert cfg["llm"]["api_key"] == "local" and cfg.workspace == tmp_path / "workspace"
    assert run(cfg) == 0                     # no sources yet (no docs/ nor src/): nothing to do
