import json
import re
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


def test_docs_step_is_incremental_retries_and_prunes(toml, docs_samples, tmp_path, fake, fake_chats, capsys,
                                                     monkeypatch):
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

    # a model of its own for a source: its documents are analysed again, with it
    from atlas.llm import JsonLlm
    sent, create = [], JsonLlm._create
    monkeypatch.setattr(JsonLlm, "_create", lambda self, **kw: sent.append((kw["model"], kw.get("reasoning_effort")))
                        or create(self, **kw))
    own = load_config(toml(sources.replace('lang = "Spanish"', 'lang = "Spanish"\nanalysis_model = "analyst"\n'
                                                                'reasoning_effort = "low"')))
    assert run(own) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"docs.process": 1}
    assert sent and set(sent) == {("analyst", "low")}
    assert run(own) == 0 and json.loads(capsys.readouterr().out)["processed"] == {}
    sent.clear()
    same = sources.replace('lang = "Spanish"', 'lang = "Spanish"\nreasoning_effort = "low"')
    assert run(load_config(toml(same))) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"docs.process": 1}
    assert set(sent) <= {("fake", "low")}           # back to the model of [llm]: from the cache of its sections

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
    assert rep["profile"]["index.documents"]["api"]["embedding_tokens"] > 0       # what the server said
    assert rep["profile"]["docs.process"]["api"]["prompt_tokens"] > 0            # hmi_manual.md, with the model
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
    assert "\nModel: 2 chat, 1 embeddings, " in out and " completion tokens, " in out      # rewriting and answer
    from atlas.usage import summary
    logged = summary(cfg.workspace / "atlas.db")
    assert logged["answers"] == {"ask": {"answered": 1}}
    assert next(r for r in logged["by_name"] if r["name"] == "ask")["requests"] == 3
    with pytest.raises(SystemExit) as e:              # the TEXT [atlas.toml] form of the command line
        main(["search", "keep logs", str(path)])
    assert e.value.code == 0 and "hmi_manual.md" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:              # options anywhere: --name value or --name=value
        main(["search", "track event", str(path), "--k", "2", "--kind=requirement"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "[2]" in out and "[3]" not in out and out.count("(requirement)") == 2
    with pytest.raises(SystemExit) as e:
        main(["ask", "--whole", "When does the HMI raise an alarm?", "--document", "hmi_manual.md", str(path)])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "Read whole: hmi_manual.md (2 requests to the model)" in out
    chats = fake_stats()["chat"]
    with pytest.raises(SystemExit) as e:              # the plan, without asking the model
        main(["ask", "Summarise hmi_manual.md", str(path), "--section", "Alarms", "--dry-run"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and fake_stats()["chat"] == chats
    assert "read whole: hmi_manual.md › Alarms" in out and "requests to the model: at least 2" in out
    with pytest.raises(SystemExit) as e:
        main(["ask", "Summarise it", str(path), "--document", "hmi_manual.md", "--section", "9"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "no section '9'" in out and "hmi_manual.md › 2 Alarms" in out
    with pytest.raises(SystemExit) as e:              # a chapter: by its title, without its number
        main(["ask", "When does the HMI raise an alarm?", str(path), "--chapter", "alarms"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "hmi_manual.md › 2 Alarms" in out and "3 Logging" not in out
    control.write_text(json.dumps({"usage_chars_per_token": 4}))     # an answer longer than max_tokens
    assert ask(load_config(toml(extra.replace('model = "fake"', 'model = "fake"\nmax_tokens = 3'))),
               "When does the HMI raise an alarm?") == 0
    assert "(The answer is cut: the model reached max_tokens" in capsys.readouterr().out
    control.write_text("{}")
    for args, error in ((["search", "x", "--kind", "pages"], "unknown kind 'pages'"),
                        (["search", "x", "--whole"], "unknown option --whole"),
                        (["ask", "x", "--k", "many"], "--k must be an integer"),
                        (["status", "--k", "3"], "unknown option --k (valid: --json)"),
                        (["plugins", "--json"], "it takes none")):
        with pytest.raises(SystemExit) as e:
            main(args + [str(path)])
        assert error in str(e.value.code)

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
    assert "code_revision" not in KnowledgeGraph.from_config(cfg).build_stats["code"]

    # [graph] code_revision: the git revision of the code, in the build and in the matrix
    import subprocess
    if shutil.which("git"):
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=ada,
                                        check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q"), git("add", "-A"), git("commit", "-q", "-m", "code")
        revised = load_config(toml(extra + '[graph]\ncode_revision = true\n'))
        assert run(revised) == 0
        capsys.readouterr()
        g = KnowledgeGraph.from_config(revised)
        assert g.build_stats["code"]["code_revision"] == git("rev-parse", "HEAD")
        assert g.matrix()["meta"]["code_revision"] == git("rev-parse", "HEAD")

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
    from pathlib import Path
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
    assert "DOORS export: export date unknown" in out and "  SRS_TMS: ?" in out
    assert "requirements without code" in out and "    1  cited in code not in doors" in out   # SRS-0103
    assert "requirements without derivation" in out and "    0  linked to modules not read" in out
    assert "ID shapes as written" in out and "  code   6 SRS-NNNN" in out and "  doors  " in out
    # the expression of an ID goes to the analysis of the code and of the documents
    loose = 'id_regex = "(?i)\\\\b(?:SRS|SSS)[-_ ]?\\\\d+\\\\b"\n'
    assert go(load_config(toml(loose + extra)))["processed"] == \
        {"doors.requirements": 3, "code.parse": 5, "docs.process": 1, "graph.build": 3}
    canonical = 'id_canonical = { regex = "^(SRS|SSS)[-_ ]?0*(\\\\d+)$", format = "{1}-{2:04d}" }\n'
    assert go(load_config(toml(loose + canonical + extra)))["processed"] == \
        {"doors.requirements": 3, "graph.build": 3}           # the code and the documents are not read again
    go()
    assert trace(cfg, "srs-0004") == 0                  # Markdown, without a model
    out = capsys.readouterr().out
    assert out.startswith("# Context of SRS-0004 (requirement)") and "## Code that implements it" in out
    assert "`tracks.delete_track`" in out and "## Tests" in out and "test_tracks.test_tracks" in out
    assert trace(cfg, "SRS-9999") == 1 and "requirement 'SRS-9999' is not in the graph" in capsys.readouterr().out
    assert trace(cfg, "srs-0004", as_json=True) == 0     # --json: the dictionary of KnowledgeGraph.trace
    res = json.loads(capsys.readouterr().out)
    assert res["id"] == "SRS-0004" and [i["id"] for i in res["implemented_by"]["items"]] == ["tracks.delete_track"]
    assert trace(cfg, "SRS-9999", as_json=True) == 1 and "error" in json.loads(capsys.readouterr().out)
    assert matrix(cfg, "SRS_TMS") == 0 and "40 requirements: 4 with code, 1 with tests" in capsys.readouterr().out
    written = cfg.workspace / "graph" / "matrix-SRS_TMS.csv"
    raw = written.read_bytes()                          # for Excel in Spanish: a BOM and ";"
    assert raw.startswith(b"\xef\xbb\xbf") and raw[3:].decode().splitlines()[0] == \
        "id;text;status;verification;derived;code;tests;covering_tests;procedures;documents;exported_at"
    import csv
    rows = {r["id"]: r for r in csv.DictReader(written.open(encoding="utf-8-sig", newline=""), delimiter=";")}
    assert rows["SRS-0004"]["code"] == "tracks.delete_track" and rows["SRS-0004"]["tests"] == "test_tracks.test_tracks"
    assert "| SRS-0004 |" in written.with_suffix(".md").read_text()
    md = written.with_suffix(".md").read_text()
    assert "- DOORS SRS_TMS: export date unknown" in md                  # the database adapter
    assert "- atlas " in md and "graph built: code " in md
    saved = json.loads(written.with_suffix(".json").read_text())
    assert saved["meta"]["module"] == "SRS_TMS" and len(saved["rows"]) == 40 and saved["summary"]["requirements"] == 40
    shown = load_config(toml(extra))
    shown["sources"]["doors"]["attributes"] = ["Verification"]      # what [sources.doors] attributes shows
    assert matrix(shown, "SRS_TMS") == 0
    capsys.readouterr()
    assert written.read_text(encoding="utf-8-sig").splitlines()[0].startswith("id;text;Verification;derived;")
    assert go(load_config(toml(extra + 'csv_delimiter = ","\nmatrix_formats = ["csv"]\n')))["skipped"]["graph.build"] == 3
    assert matrix(load_config(toml(extra + 'csv_delimiter = ","\nmatrix_formats = ["csv"]\n')), "SRS_TMS") == 0
    assert "id,text," in written.read_text(encoding="utf-8-sig") and "matrix-SRS_TMS.md" not in capsys.readouterr().out
    assert matrix(cfg, "NOPE") == 1 and "SRS_TMS, SRS_TMS_OLD, SSS_MISSION" in capsys.readouterr().out
    assert matrix(cfg, "*") == 0 and (cfg.workspace / "graph" / "matrix-all.md").exists()
    capsys.readouterr()

    # --json: only the dictionary of the library function on stdout, for scripts
    from atlas.cli import main
    toml_path = str(cfg.path)

    def as_json(*args, code=0):
        with pytest.raises(SystemExit) as e:
            main([*args, toml_path, "--json"])
        assert e.value.code == code
        return json.loads(capsys.readouterr().out)

    res = as_json("matrix", "SRS_TMS")
    assert res["summary"]["requirements"] == 40 and len(res["rows"]) == 40 and "without_code" in res["gaps"]
    assert as_json("matrix", "NOPE", code=1)["modules"] == ["SRS_TMS", "SRS_TMS_OLD", "SSS_MISSION"]
    assert as_json("trace", "SRS-0004")["verified_by"] == [{"id": "test_tracks.test_tracks", "how": "tag"}]
    res = as_json("graph")
    assert set(res["builds"]) == {"code", "doors", "docs"} and res["gaps"]["requirements_without_code"]["count"] > 0
    res = as_json("status")
    assert res["data"]["requirements"] > 0 and res["graph"]["nodes"] > 0 and res["runs"][0]["report"]["processed"] == {}
    assert set(res) == {"workspace", "data", "graph", "index", "ledger", "pending", "runs", "doors_exports", "sent",
                        "schema_version"}
    res = as_json("sections")
    assert [(d["source"], Path(d["file"]).name) for d in res["documents"]] == [("manuals", "hmi_manual.md")]

    manual.unlink()                                     # no documents: their part of the graph goes
    assert go()["pruned"] == {"docs.process": 1, "graph.build": 1}
    assert not [n for n, d in KnowledgeGraph.from_config(cfg).G.nodes(data=True) if d["kind"] == "section"]
    assert (cfg.workspace / "graph" / "atlas.graphml").exists()


def _with_attributes(path, attributes: str):
    path.write_text(re.sub(r"attributes = \[.*\]", f"attributes = {attributes}", path.read_text()))
    return load_config(path)


def test_doors_requirements_are_indexed(toml, fake, fake_stats, capsys):
    import pytest
    from atlas.cli import check, main, search
    from atlas.config import ConfigError
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
    path = toml(extra)
    cfg = load_config(path)

    def go(config=cfg):
        assert run(config) == 0
        return json.loads(capsys.readouterr().out)

    assert go()["processed"] == {"doors.requirements": 3, "index.requirements": 3}
    embedded = fake_stats()["emb_texts"]
    assert go()["skipped"] == {"doors.requirements": 3, "index.requirements": 3}
    assert fake_stats()["emb_texts"] == embedded                    # nothing embedded again
    assert search(cfg, "srs-0004") == 0 and "SRS_TMS (requirement)" in capsys.readouterr().out
    outputs = []
    for query in ("SRS-0004", "SRS0004", "srs-4"):                # an ID written in another way
        assert search(cfg, query) == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0].startswith("\n[1] SRS_TMS (requirement)\n    SRS-0004:") and outputs[1:] == outputs[:1] * 2
    # the effective prefixes of the embeddings are in the fingerprints: another family reindexes
    none = load_config(toml(extra.replace('model = "embeddinggemma"', 'model = "embeddinggemma"\nfamily = "none"')))
    assert go(none)["processed"] == {"index.requirements": 3} and go(none)["processed"] == {}
    check(none)
    assert "embeddings prefixes: query='' document=''" in capsys.readouterr().out
    check(cfg)
    assert "embeddings prefixes: query='task: search result | query: ' document='title: {title} | text: '" in \
        capsys.readouterr().out
    check(load_config(toml(extra.replace('model = "embeddinggemma"', 'model = "corp-embed-300m"'))))
    assert "WARNING the embeddings model 'corp-embed-300m' is of no known family" in capsys.readouterr().out

    # the attributes of [sources.doors] attributes: in the text, and filters
    path = toml(extra)
    assert go()["processed"] == {"index.requirements": 3}           # back to the family of the model
    assert search(cfg, "track event", k=20, attribute="status=Approved", module="SRS_TMS") == 0
    out = capsys.readouterr().out
    assert out.count("status: Approved") == out.count("(requirement)") > 3 and "Draft" not in out
    assert "SSS_MISSION" not in out
    with pytest.raises(SystemExit) as e:              # repeated: both pairs
        main(["search", "track event", str(path), "--attribute", "status=Draft", "--attribute=verification=Test"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "nothing found with those filters" in out and "no passage has verification" in out
    assert re.search(r"status \([^)]*Draft", out)                    # what the filters can take
    with pytest.raises(SystemExit) as e:
        main(["search", "track event", str(path), "--attribute", "Approved"])
    assert "is not name=value" in str(e.value.code)
    # another list of attributes reindexes the modules, without reading DOORS again
    path.write_text(path.read_text().replace('adapter = "database"', 'adapter = "database"\nattributes = ["Status"]'))
    assert go(load_config(path))["processed"] == {"index.requirements": 3}
    assert search(load_config(path), "srs-0004") == 0 and "Status: " in capsys.readouterr().out
    with pytest.raises(ConfigError, match="not attribute names"):
        run(_with_attributes(path, '["a=b"]'))
    assert go(load_config(toml(extra + '[index]\nkinds = ["overview"]\n')))["pruned"] == {"index.requirements": 3}


def test_code_is_indexed_by_unit(toml, samples, tmp_path, fake, fake_stats, capsys):
    import shutil
    import pytest
    from atlas.cli import search
    pytest.importorskip("tree_sitter_ada")
    ada = tmp_path / "ada"
    shutil.copytree(samples / "ada", ada)
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
steps = ["code", "index"]
[sources.code]
roots = ["{ada.as_posix()}"]
test_patterns = ["**/tests/**"]
'''
    cfg = load_config(toml(extra))

    def go(config=cfg):
        assert run(config) == 0
        return json.loads(capsys.readouterr().out)

    # one unit per package (spec and body together), and one per test procedure
    assert go()["processed"] == {"code.parse": 5, "index.code": 3}
    assert search(cfg, "remove a track from the database") == 0
    out = capsys.readouterr().out
    assert "[1] tracks › tracks.delete_track · tracks.adb:11-15 (code_doc)" in out
    assert "Removes track I from the database." in out and 'Logger.Log ("delete");' in out

    embedded = fake_stats()["emb_texts"]
    assert go()["skipped"] == {"code.parse": 5, "index.code": 3} and fake_stats()["emb_texts"] == embedded
    # a changed body: that file is analysed again and its unit indexed again, embedding only
    # the record whose text changed
    body = ada / "src" / "tracks.adb"
    body.write_text(body.read_text().replace('"delete"', '"track deleted"'))
    assert go()["processed"] == {"code.parse": 1, "index.code": 1}
    assert fake_stats()["emb_texts"] == embedded + 1
    # a unit that disappears is pruned, and without code_doc in [index] kinds nothing is indexed
    for f in ("logger.ads", "logger.adb"):
        (ada / "src" / f).unlink()
    assert go()["pruned"] == {"code.parse": 2, "index.code": 1}
    rep = go(load_config(toml(extra + '[index]\nkinds = ["chunk"]\n')))
    assert "index.code" not in rep["processed"] and "index.code" not in rep["skipped"]


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
    for argv, code in ((["init"], 0), (["init"], 1), (["--version"], 0),     # init never overwrites
                       (["init", "gw.toml", "--profile", "gateway"], 0), (["init", "x.toml", "--profile=cloud"], 1)):
        with pytest.raises(SystemExit) as exit:
            main(argv)
        assert exit.value.code == code
    assert f"\natlas {__version__}\n" in capsys.readouterr().out
    cfg = load_config(tmp_path / "atlas.toml")
    assert cfg["llm"]["api_key"] == "local" and cfg.workspace == tmp_path / "workspace"
    assert run(cfg) == 0                     # no sources yet (no docs/ nor src/): nothing to do
    gateway = load_config(tmp_path / "gw.toml")
    assert "ATLAS_API_KEY" in gateway.unset and gateway["llm"]["reasoning_effort"] == "none"
    assert gateway["network"]["requests_per_minute"] == 28 and gateway["network"]["ca_cert"] == "system"
    assert gateway["search"]["scope_chars"] == 300000 and gateway["pipeline"]["workers"] == 6
    assert not (tmp_path / "x.toml").exists()


def test_a_configuration_finds_its_files_from_any_folder(tmp_path, samples, monkeypatch, capsys):
    """The relative paths of atlas.toml are relative to it: a project in a subfolder, run from
    another folder, finds its code, documents and DOORS database, and names them as when it
    runs from its own folder (nothing is processed again)."""
    import os
    import shutil
    import pytest
    from hephesto import CodeTools
    from atlas.graph import KnowledgeGraph
    from atlas.store import Store
    pytest.importorskip("tree_sitter_ada")
    project = tmp_path / "projects" / "tms"
    shutil.copytree(samples / "ada", project / "ada")
    (project / "docs").mkdir()
    (project / "docs" / "spec.md").write_text("# Spec\n\n## Tracks\n\nReq: SRS-0101 The system shall delete tracks.\n",
                                              encoding="utf-8")
    shutil.copy(samples / "requirements.sqlite", project / "req.sqlite")
    (project / "atlas.toml").write_text('''
[project]
workspace = "ws"
[pipeline]
steps = ["doors", "code", "docs", "graph"]
[[sources.docs]]
name = "specs"
paths = ["docs"]
analysis = "regex"
req_regex = "(?i)\\\\bReq\\\\s*:\\\\s*(\\\\S+)"
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "req.sqlite"
[sources.code]
roots = ["ada"]
test_patterns = ["**/tests/**"]
''', encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    cfg = load_config(project / "atlas.toml")
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"]["code.parse"] == 5 and rep["processed"]["docs.process"] == 1
    assert rep["processed"]["doors.requirements"] >= 2 and not rep["errors"]
    store = Store(cfg.workspace)
    assert os.path.join("ada", "src", "tracks.adb") in [u["file"] for u in store.code_units()]
    assert [d["unit"] for d in store.documents()] == [f"specs:{os.path.join('docs', 'spec.md')}"]
    # the graph and the file tools read the files from the folder of atlas.toml too
    graph = KnowledgeGraph.from_config(cfg)
    assert "Delete_Track" in graph.read_file(os.path.join("ada", "src", "tracks.adb"), 1, 40)["source"]
    assert "error" not in CodeTools.from_config(cfg, read_only=True).read_file("ada/src/tracks.adb")
    # from its own folder: the same names, nothing to process
    monkeypatch.chdir(project)
    assert run(load_config("atlas.toml")) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {} and rep["pruned"] == {} and rep["skipped"]["code.parse"] == 5


def test_a_missing_code_root_is_warned(toml, tmp_path, caplog):
    import pytest
    from atlas.cli import _context, code_step
    pytest.importorskip("tree_sitter_ada")
    cfg = load_config(toml(f'''
[sources.code]
roots = ["{(tmp_path / 'no-such-src').as_posix()}"]
languages = ["ada"]
'''))
    ctx = _context(cfg)
    step = code_step(ctx)
    with caplog.at_level("WARNING", logger="atlas"):
        assert step.units(ctx) == []
    assert "no-such-src does not exist" in caplog.text


def test_doctor_finds_a_missing_root_and_a_missing_command(toml, samples, tmp_path, fake, capsys):
    from atlas.cli import doctor
    from atlas.doctor import diagnose
    cfg = load_config(toml(f'''
[llm]
base_url = "{fake[0]}"
model = "fake"
api_key = "local"
[network]
no_proxy = "*"
[pipeline]
steps = ["doors", "code"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}", "{(tmp_path / 'no-such-src').as_posix()}"]
languages = ["ada"]
check_command = ["no-such-compiler-xyz", "-c", "{{file}}"]
tool_path = ["{(tmp_path / 'no-such-gnat-*' / 'bin').as_posix()}"]
'''))
    found = {(f.where, f.level): f for f in diagnose(cfg, connect=False)}
    assert "no-such-src does not exist" in found[("sources.code", "fail")].message
    assert "files" in found[("sources.code", "ok")].message
    assert "no-such-compiler-xyz" in found[("sources.code check_command", "fail")].message
    assert found[("sources.code tool_path", "warn")].fix
    assert ("workspace", "ok") in found and ("python", "ok") in found
    assert not (tmp_path / "ws").exists()                   # it only reads: no workspace created
    assert doctor(cfg) == 1
    out = capsys.readouterr().out
    assert "FAIL  sources.code" in out and "OK    connection llm" in out and "2 failures" in out
    no_ca = load_config(toml(f'''
[llm]
base_url = "{fake[0]}"
api_key = "local"
[network]
no_proxy = "*"
ca_cert = "{(tmp_path / 'no-such-ca.pem').as_posix()}"
[pipeline]
steps = []
'''))
    found = [f for f in diagnose(no_ca) if f.where == "connection llm"]
    assert found[0].level == "fail" and "no-such-ca.pem does not exist" in found[0].message and "1 warning," in out


def test_estimate_then_a_run_with_a_budget_that_goes_on_next_time(toml, tmp_path, fake, fake_chats, capsys):
    """With the slow fake model: atlas estimate says the requests the run will make; a run
    with a small max_requests stops with exit 3 and the state saved; the next run finishes
    without asking again what was done (in all, the requests of the estimate)."""
    import time
    import pytest
    from atlas.cli import estimate, main
    from atlas.pipeline import Ledger
    url, control = fake
    docs = tmp_path / "docs"
    docs.mkdir()
    for d in range(4):
        (docs / f"manual_{d}.md").write_text(f"# Manual {d}\n\n" + "".join(
            f"{k} Part {k}\n\nReq: SRS-0{d}{k}0 The unit {d} shall do the task {k} of manual {d}.\n\n"
            for k in range(1, 4)), encoding="utf-8")
    cfg = load_config(toml(f'''
[llm]
base_url = "{url}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
requests_per_minute = 600
[pipeline]
steps = ["docs"]
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
min_chars = 10
'''))
    assert estimate(cfg) == 0
    out = capsys.readouterr().out
    assert "docs.process: 4 units, 20 chat requests, under 1 min at 600/min" in out    # 4 sections (the title too) + overview

    control.write_text(json.dumps({"slow": 0.05}))
    t0 = time.time()
    assert run(cfg, max_requests=6) == 3
    captured = capsys.readouterr()
    rep = json.loads(captured.out)
    assert rep["stopped_by"] == "budget" and "budget reached" in captured.err
    assert 6 <= fake_chats() < 20 and rep["processed"].get("docs.process", 0) < 4
    ledger = Ledger(cfg.workspace / "atlas.db")
    assert ledger.runs()[0]["report"]["stopped_by"] == "budget"
    left = sum(r["status"] != "ok" for r in ledger.units("docs.process").values()) + 4 - len(ledger.units("docs.process"))
    with pytest.raises(SystemExit) as e:
        main(["run", str(cfg.path)])
    rep = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and rep["processed"] == {"docs.process": left} and fake_chats() == 20

    with pytest.raises(SystemExit) as e:
        main(["run", str(cfg.path), "--until", "25:00"])
    assert "--until must be a time HH:MM" in str(e.value.code)
    assert time.time() - t0 < 60


def test_the_deadline_of_until_and_max_minutes():
    from datetime import datetime
    from atlas.cli import deadline
    now = datetime(2026, 10, 17, 22, 30).timestamp()
    assert deadline(now=now) is None
    assert deadline(minutes=90, now=now) == now + 5400
    assert datetime.fromtimestamp(deadline("07:00", now=now)) == datetime(2026, 10, 18, 7, 0)    # tomorrow
    assert datetime.fromtimestamp(deadline("23:00", now=now)) == datetime(2026, 10, 17, 23, 0)   # today
    assert deadline("23:59", minutes=10, now=now) == now + 600                                  # the earliest


def test_a_doors_requirement_comes_with_the_note_after_it(tmp_path, samples, fake):
    """The index keeps the order of the module: the context of SRS-0004 has SRS-0003 before
    it and the note that follows it in the exchange database (examples/make_samples.py)."""
    from atlas.index import Index
    from atlas.index.search import build_context
    path = tmp_path / "atlas.toml"
    path.write_text(f'''
[project]
workspace = "{(tmp_path / 'ws').as_posix()}"
[llm]
base_url = "{fake[0]}"
model = "fake"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "index"]
[sources.doors]
adapter = "exchange"
modules = ["SRS_TMS"]
[sources.doors.exchange]
path = "{(samples / 'requirements.sqlite').as_posix()}"
kinds = ["requirement", "heading", "information"]
''', encoding="utf-8")
    cfg = load_config(path)
    assert run(cfg) == 0
    idx = Index.from_config(cfg)
    hit = idx.search("srs-4", k=1, kinds=["requirement"])[0]
    context = build_context(idx.store, [hit])[0]["context"]
    assert context.split(" … ") == [
        next(r["text"] for r in idx.store.document_records("doors:SRS_TMS") if r["ref"] == "SRS-0003"), hit["text"],
        "Note: the deadlines of these requirements are measured from the reception of the radar plot."]


def test_cache_export_and_import_make_a_new_workspace_ask_nothing(toml, samples, docs_samples, tmp_path, fake,
                                                                   fake_stats, capsys):
    """run -> export -> a new workspace -> import -> run: no request to the chat model nor
    to the embeddings. Import adds what is missing and never replaces anything."""
    import shutil
    import sqlite3
    import pytest
    from atlas.cli import main
    pytest.importorskip("markitdown")
    pytest.importorskip("tree_sitter_ada")
    url, _ = fake
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
steps = ["doors", "code", "docs", "graph", "index"]
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
lang = "Spanish"
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
languages = ["ada"]
'''
    first = toml(extra)
    second = tmp_path / "second.toml"
    second.write_text(first.read_text().replace(f"{(tmp_path / 'ws').as_posix()}\"",
                                                f"{(tmp_path / 'ws2').as_posix()}\""))
    a, b = load_config(first), load_config(second)
    assert b.workspace == tmp_path / "ws2"

    def cli(*args) -> tuple[int, str, str]:
        with pytest.raises(SystemExit) as e:
            main(list(args))
        out = capsys.readouterr()
        return e.value.code, out.out, out.err

    assert run(a) == 0
    capsys.readouterr()
    computed = fake_stats()
    assert computed["chat"] > 0 and computed["embeddings"] > 0
    # a workspace of atlas 0.2 did not record the model of each vector: export finds it
    # from the texts of the index; a vector of a text no longer indexed stays out
    db = sqlite3.connect(a.workspace / "atlas.db")
    with db:
        vectors = db.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
        db.execute("UPDATE embed_cache SET model = NULL")
        db.execute("INSERT INTO embed_cache VALUES (?, ?, NULL)", ("f" * 32, b"\0" * 16))
    db.close()
    (a.workspace / "reports").mkdir(exist_ok=True)
    (a.workspace / "reports" / ".verify_cache.json").write_text(json.dumps({"k1": {"verdict": "ok"},
                                                                            "k2": {"verdict": "ok"}}))
    file = tmp_path / "share" / "tms-cache.sqlite"
    code, out, err = cli("cache", "export", str(file), str(first), "--with-judgements")
    assert code == 0, err
    analyses = len(list((a.workspace / "docs" / ".cache").glob("*.json")))
    assert analyses > 0
    assert f"{vectors} vectors (embeddinggemma), {analyses} analyses of sections (fake), 2 judgements" in out
    assert "1 cached vectors of an unknown model" in err

    # the new workspace: one judgement of its own, which import does not replace
    (b.workspace / "reports").mkdir(parents=True)
    (b.workspace / "reports" / ".verify_cache.json").write_text(json.dumps({"k1": {"verdict": "mine"}}))
    code, out, err = cli("cache", "import", str(file), str(second))
    assert code == 0, err
    assert f"vectors (embeddinggemma): {vectors} new, 0 already here" in out
    assert f"analyses of sections (fake): {analyses} new, 0 already here" in out
    assert "judgements .verify_cache.json: 1 new, 1 already here" in out
    assert json.loads((b.workspace / "reports" / ".verify_cache.json").read_text()) == \
        {"k1": {"verdict": "mine"}, "k2": {"verdict": "ok"}}

    before = fake_stats()
    assert run(b) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"]["docs.process"] == 2 and rep["processed"]["index.documents"] == 2
    assert rep["processed"]["index.requirements"] >= 1 and rep["processed"]["index.code"] >= 1
    after = fake_stats()
    assert (after["chat"], after["embeddings"]) == (before["chat"], before["embeddings"])

    code, out, _ = cli("cache", "import", str(file), str(second))      # again: nothing new
    assert code == 0 and f"{vectors} already here" in out and f"0 new, {analyses} already here" in out

    # another embeddings model: refused, its keys would never match
    other = tmp_path / "other.toml"
    other.write_text(second.read_text().replace('model = "embeddinggemma"', 'model = "nomic-embed-text"'))
    code, _, _ = cli("cache", "import", str(file), str(other))
    assert "embeddings model 'embeddinggemma'" in code and "uses 'nomic-embed-text'" in code
    for args, error in ((["cache", "import", str(a.workspace / "atlas.db"), str(second)], "is not a cache exported"),
                        (["cache", "import", str(tmp_path / "none.sqlite"), str(second)], "does not exist"),
                        (["cache", "import", str(file), str(second), "--with-judgements"], "an option of export"),
                        (["cache", "list", str(file)], "usage: atlas cache export|import")):
        code, _, _ = cli(*args)
        assert error in str(code)
    empty = tmp_path / "empty.toml"
    empty.write_text(second.read_text().replace("/ws2\"", "/ws3\""))
    code, _, _ = cli("cache", "export", str(file), str(empty))
    assert "has no atlas.db" in str(code) and not (tmp_path / "ws3" / "atlas.db").exists()


def test_shared_cache_is_read_through_and_never_written(toml, docs_samples, tmp_path, fake, fake_stats, capsys,
                                                         caplog):
    """[project] shared_cache: a folder of exported caches that a new workspace reads when
    its own cache lacks an entry. What it finds is copied to the workspace; the folder is
    not written. A folder that cannot be read: a warning, once, and the run goes on."""
    import hashlib
    import logging
    import shutil
    import sqlite3
    import pytest
    from atlas.cache import pack
    pytest.importorskip("markitdown")
    url, _ = fake
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(docs_samples / "hmi_manual.md", docs / "hmi_manual.md")
    base = toml(f'''
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
name = "manuals"
paths = ["{docs.as_posix()}"]
''').read_text()
    ws = f"{(tmp_path / 'ws').as_posix()}\""

    def config(name: str, shared: str = "") -> object:
        p = tmp_path / f"{name}.toml"
        text = base.replace(ws, f"{(tmp_path / name).as_posix()}\"")
        p.write_text(text.replace('name = "t"\n', f'name = "t"\nshared_cache = "{shared}"\n', 1))
        return load_config(p)

    a = config("a")
    assert run(a) == 0
    capsys.readouterr()
    share = tmp_path / "share"
    pack(a, share / "tms-1.sqlite")
    (share / "notes.txt").write_text("not a cache")
    (share / "broken.sqlite").write_text("not a database either")
    sqlite3.connect(share / "other.sqlite").close()           # an empty database: no header
    digest = lambda: {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(share.iterdir())}
    before_share = digest()

    b = config("b", "share")                                   # relative to the folder of atlas.toml
    before = fake_stats()
    with caplog.at_level(logging.WARNING, logger="atlas.cache"):
        assert run(b) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {"docs.process": 1, "index.documents": 1}
    after = fake_stats()
    assert (after["chat"], after["embeddings"]) == (before["chat"], before["embeddings"])
    assert digest() == before_share                            # nothing written in the shared folder
    warned = [r for r in caplog.records if r.name == "atlas.cache" and r.levelno >= logging.WARNING]
    assert {r.getMessage().split(" left out")[0] for r in warned} == \
        {f"shared cache {share / 'broken.sqlite'}", f"shared cache {share / 'other.sqlite'}"}
    # what it read is now in the workspace: the same entries as in the first one
    names = lambda cfg: sorted(f.name for f in (cfg.workspace / "docs" / ".cache").glob("*.json"))
    assert names(b) == names(a)
    count = lambda cfg: sqlite3.connect(cfg.workspace / "atlas.db").execute(
        "SELECT COUNT(*) FROM embed_cache WHERE model = 'embeddinggemma'").fetchone()[0]
    assert count(b) == count(a)

    # a folder that is not there: one warning, and the run goes on asking the models
    c = config("c", (tmp_path / "unplugged").as_posix())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="atlas.cache"):
        assert run(c) == 0
    capsys.readouterr()
    assert fake_stats()["chat"] > after["chat"]
    assert [r.getMessage() for r in caplog.records if "not accessible" in r.getMessage()] == \
        [f"shared cache {tmp_path / 'unplugged'} is not accessible (not a folder): going on without it"]


def test_what_was_sent_to_each_model(toml, docs_samples, tmp_path, fake, capsys):
    """atlas run reports what it sent to each model, by source, and atlas status keeps the
    count and the date of the last one; a local-only source is never in it."""
    import shutil
    import pytest
    from atlas.cli import ask, status
    from atlas.outbound import sent_summary
    pytest.importorskip("markitdown")
    url, _ = fake
    docs, client = tmp_path / "docs", tmp_path / "client"
    docs.mkdir()
    client.mkdir()
    shutil.copy(docs_samples / "hmi_manual.md", docs)
    (client / "annex.md").write_text("# Annex\n\n1 Beacon\n\nReq: SRS-0977 The beacon shall blink.\n")
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
steps = ["doors", "docs", "index"]
[[sources.docs]]
name = "manuals"
paths = ["{docs.as_posix()}"]
[[sources.docs]]
name = "client"
paths = ["{client.as_posix()}"]
model = "none"
'''))

    def go() -> dict:
        assert run(cfg) == 0
        return json.loads(capsys.readouterr().out)["sent"]

    sent = go()
    assert sent["fake"] == {"docs:manuals": {"sections": 4, "overviews": 1}}
    texts = sent["embeddinggemma"]
    assert texts["docs:manuals"]["texts"] > 5 and texts["doors:SRS_TMS"]["texts"] > 5 and "docs:client" not in texts
    assert go() == {}                                                  # nothing changed: nothing sent
    assert ask(cfg, "When does the HMI raise an alarm?") == 0
    capsys.readouterr()
    rows = {(r["model"], r["source"], r["kind"]): r for r in sent_summary(cfg.workspace / "atlas.db")}
    assert rows[("fake", "docs:manuals", "answers")]["count"] == 1 and not [k for k in rows if k[1] == "docs:client"]
    assert status(cfg) == 0
    out = capsys.readouterr().out
    assert 'sent to the models (never: docs:client, model = "none"):' in out
    assert "  fake\n    docs:manuals" in out and "4 sections" in out and "1 answers" in out and "; last 20" in out


def test_status_shows_the_pace_latency_and_concurrency_of_each_step():
    """Little's law with what the profile keeps: the requests in flight are the seconds of the
    server over those of the step, and rpm x latency / 60 of them fill the quota."""
    from atlas.cli import pace_lines
    step = {"seconds": 1860, "api": {"chat": 540, "chat_seconds": 540 * 21, "embeddings": 90,
                                      "embeddings_seconds": 45.0, "api_seconds": 540 * 21 + 45.0}}
    assert pace_lines(step, {"chat": 28, "embeddings": 28}) == [
        "chat: 540 in 31 min = 17/min of 28; latency 21 s; 6.1 in flight; 10 would fill the quota",
        "embeddings: 90 in 31 min = 3/min of 28; latency 0.50 s; 0.0 in flight; 1 would fill the quota"]
    assert pace_lines(step, {})[0] == "chat: 540 in 31 min = 17/min; latency 21 s; 6.1 in flight"
    # a run of atlas 0.2 has only api_seconds, for all the endpoints: shared by requests
    old = {"seconds": 60, "api": {"chat": 10, "embeddings": 10, "api_seconds": 40.0}}
    assert pace_lines(old, {"chat": 20})[0] == "chat: 10 in 60 s = 10/min of 20; latency 2.0 s; 0.3 in flight; " \
                                               "1 would fill the quota"
    assert pace_lines({"seconds": 5, "api": {"other": 1}}, {"chat": 20}) == []


def test_the_concurrency_of_the_model_is_not_the_workers_of_the_documents(toml):
    import pytest
    from atlas.config import ConfigError
    from atlas.llm import JsonLlm, from_config
    cfg = load_config(toml('[llm]\nconcurrency = 8\n[search]\npart_workers = 2\n'))
    service = from_config(cfg).llm
    assert service.concurrency == 8 and JsonLlm(service).concurrency == 8 and JsonLlm(service, 3).concurrency == 3
    assert cfg["search"]["part_workers"] == 2 and load_config(toml())["search"]["part_workers"] == 4
    with pytest.raises(ConfigError, match="concurrency"):
        load_config(toml('[llm]\nconcurrency = -1\n'))
    with pytest.raises(ConfigError, match="'part_workers' must be at least 1"):
        load_config(toml('[search]\npart_workers = 0\n'))


def test_the_output_is_utf_8_in_a_cp1252_console(tmp_path):
    """A Windows console or redirected output is cp1252: without reconfiguring the output,
    printing "→" ends the action with UnicodeEncodeError."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    target = tmp_path / "plan → 2.toml"
    res = subprocess.run([sys.executable, "-m", "atlas.cli", "init", str(target)], capture_output=True,
                         cwd=Path(__file__).resolve().parents[1], timeout=120,
                         env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    assert "plan → 2.toml" in res.stdout.decode("utf-8") and target.exists()


def test_a_scanned_pdf_stays_in_error_in_run_status_and_sections(toml, scanned_pdf, capsys):
    from atlas.cli import sections
    cfg = load_config(toml(f'''
[pipeline]
steps = ["docs"]
[[sources.docs]]
name = "scans"
paths = ["{scanned_pdf.parent.as_posix()}"]
analysis = "regex"
'''))
    message = "scanned PDF: 1 of 1 pages have no text; nothing is sent to the model"
    assert run(cfg) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["errors"] == {"docs.process": 1}
    assert status(cfg) == 0
    assert f"ScannedDocument: {message}" in capsys.readouterr().out
    assert sections(cfg) == 0
    assert f"! {message}" in capsys.readouterr().out
    assert run(cfg) == 1                                        # retried on the next run
    assert json.loads(capsys.readouterr().out)["errors"] == {"docs.process": 1}


def test_versions_and_authority_of_the_documents(toml, docs_samples, tmp_path, capsys):
    from atlas.cli import graph, sections
    from atlas.graph import KnowledgeGraph
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "old_spec.md").write_text(
        "Doc. TMS-SRS-001  Issue 1\n\n# Old specification\n\n1 Timing\n\n"
        "Req: SRS-0001 The TMF shall process track event 0 within 999 ms.\n", encoding="utf-8")
    sources = f'''
[pipeline]
steps = ["docs", "graph"]
[[sources.docs]]
name = "normative"
paths = ["{(docs_samples / 'spec_with_bookmarks.pdf').as_posix()}"]
analysis = "regex"
doc_id_regex = 'Doc\\.\\s+(\\S+)'
version_regex = '\\bIssue\\s+(\\w+)'
authority = 1
[[sources.docs]]
name = "old"
paths = ["{(tmp_path / 'old').as_posix()}"]
analysis = "regex"
doc_id_regex = 'Doc\\.\\s+(\\S+)'
version_regex = '\\bIssue\\s+(\\w+)'
authority = 2
'''
    cfg = load_config(toml(sources))
    assert sections(cfg) == 0
    assert "doc_id: TMS-SRS-001; version: 2" in capsys.readouterr().out
    assert run(cfg) == 0
    assert json.loads(capsys.readouterr().out)["processed"] == {"docs.process": 2, "graph.build": 1}
    assert graph(cfg) == 0
    assert "! old_spec.md (TMS-SRS-001 v1) is older than spec_with_bookmarks.pdf (v2)" in capsys.readouterr().out
    t = KnowledgeGraph.open(cfg.workspace / "atlas.db").trace("SRS-0001")
    old = next(x for x in t["documents"] if x["document"] == "old_spec.md")
    assert old["values_differ"]["against"].startswith("spec_with_bookmarks.pdf v2 §") and old["version"] == "1"

    # the authority is only of the graph: the documents are not processed again
    cfg = load_config(toml(sources.replace("authority = 2", "authority = 0")))
    assert run(cfg) == 0
    assert json.loads(capsys.readouterr().out)["processed"] == {"graph.build": 1}
    t = KnowledgeGraph.open(cfg.workspace / "atlas.db").trace("SRS-0001")
    assert next(x for x in t["documents"] if x["document"] == "old_spec.md")["values_differ"]["against"]
    import pytest
    from atlas.config import ConfigError
    with pytest.raises(ConfigError, match="authority"):
        run(load_config(toml(sources.replace("authority = 2", "authority = -1"))))
