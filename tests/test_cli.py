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
    assert status(cfg) == 0


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
