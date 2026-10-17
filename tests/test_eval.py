"""atlas eval without a model: the search cases ranked in the index (hybrid and lexical) and
the graph cases, over the samples processed with the fake API. The agent cases are in
test_agents.py."""
import contextlib
import io
import json
from pathlib import Path

import pytest

from atlas.config import ConfigError, load_config

SET = Path(__file__).resolve().parents[1] / "examples" / "eval" / "retrieval.yaml"


@pytest.fixture
def indexed(toml, samples, docs_samples, fake):
    """The samples as `atlas run` leaves them: DOORS, code, documents, graph and index."""
    pytest.importorskip("tree_sitter_ada")
    pytest.importorskip("pymupdf")
    from atlas.cli import run
    url, _ = fake
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
[[sources.docs]]
name = "standards"
paths = ["{docs_samples.as_posix()}"]
patterns = ["*.pdf"]
analysis = "regex"
[[sources.docs]]
name = "manuals"
paths = ["{docs_samples.as_posix()}"]
patterns = ["*.md"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
'''))
    with contextlib.redirect_stdout(io.StringIO()):
        assert run(cfg) == 0
    return cfg


def test_cases_of_three_kinds(tmp_path):
    from atlas.agents.evaluate import load_cases
    cases = {c["id"]: c for c in load_cases(SET)}
    assert cases["search-logs-30-days"]["kind"] == "search" and cases["search-doors-deadline"]["kinds"] == ["requirement"]
    assert cases["graph-impact-log"]["kind"] == "graph" and cases["graph-impact-log"]["query"] == "impact"
    assert cases["search-spanish-logs"]["tags"] == ["paraphrase", "spanish"] and cases["graph-impact-log"]["tags"] == ["impact"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("- id: a\n  search: x\n  expect: x\n  tags: paraphrase\n")
    with pytest.raises(ConfigError, match="tags is a list of words"):
        load_cases(bad)
    for text in ("- id: a\n  expect: x\n",                                     # nothing to ask
                 "- id: a\n  search: x\n  trace: SRS-0001\n  expect: x\n",     # two things
                 "- id: a\n  matrix: SRS_TMS\n  expect: x\n"):                 # not a query of the set
        bad.write_text(text)
        with pytest.raises(ConfigError, match="one of question, search or a query of the graph"):
            load_cases(bad)


def test_search_and_graph_are_scored_without_a_model(indexed, fake_stats):
    from atlas.agents.evaluate import evaluate_retrieval, load_cases
    cases = load_cases(SET)
    before = fake_stats()
    res = evaluate_retrieval(cases, indexed)
    after = fake_stats()
    searches = [c for c in cases if c["kind"] == "search"]
    # no chat; one request of embeddings per hybrid search, none for the lexical ones
    assert after["chat"] == before["chat"] and after["embeddings"] - before["embeddings"] == len(searches)

    assert {k: v for k, v in res["graph"].items() if k != "tags"} == {"passed": 10, "total": 10, "rate": 1.0}
    assert res["graph"]["tags"]["trace"] == {"passed": 4, "total": 4, "rate": 1.0}
    assert res["search"]["cases"] == len(searches) == 20
    tags = res["search"]["tags"]
    assert list(tags) == ["id", "words", "paraphrase", "spanish", "section", "code"]
    assert tags["paraphrase"]["cases"] == 6 and tags["spanish"]["cases"] == 2
    assert set(tags["spanish"]["lexical"]) == {"hit@1", "hit@3", "hit@5", "hit@10", "mrr"}

    # the vectors of the queries stay in the cache: a second pass makes no request
    again = fake_stats()["embeddings"]
    assert evaluate_retrieval(cases, indexed)["search"] == res["search"]
    assert fake_stats()["embeddings"] == again
    assert set(res["search"]["lexical"]) == {"hit@1", "hit@3", "hit@5", "hit@10", "mrr"}
    by_id = {r["id"]: r for r in res["results"]}
    for case in ("search-id-srs-0102", "search-logs-30-days", "search-doors-deadline", "search-section-logging",
                 "search-code-remove-track"):
        assert by_id[case]["lexical"]["rank"] == 1, case
    assert by_id["search-id-srs-0102"]["lexical"]["top"][0] == "hmi_manual.md › 2 Alarms (requirement SRS-0102)"

    # a case is found at the position of its last expected text, and not found if one is missing
    wrong = [{"id": "two", "kind": "search", "search": "SRS-0102 alarm", "expect": ["SRS-0102", "no such text"]},
             {"id": "none", "kind": "graph", "query": "trace", "trace": "SRS-9999", "expect": ["SRS-9999"]}]
    res = evaluate_retrieval(wrong, indexed)
    two, none = res["results"]
    assert two["lexical"]["rank"] is None and two["lexical"]["ranks"]["SRS-0102"] == 1
    assert res["search"]["lexical"] == {"hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "hit@10": 0.0, "mrr": 0.0}
    assert none["passed"] is False and "error" in none and "tags" not in res["search"]

    # section:<document>#<id> is found only in a result of that section
    section = [{"id": "s", "kind": "search", "search": "3 Logging", "expect": ["section:hmi_manual.md#004"]},
               {"id": "t", "kind": "search", "search": "3 Logging", "expect": ["section:hmi_manual.md#999"]}]
    s, t = evaluate_retrieval(section, indexed)["results"]
    assert s["lexical"]["rank"] == 1 and t["lexical"]["rank"] is None


def test_atlas_eval_with_only_retrieval_needs_no_agent(indexed, monkeypatch, capsys):
    from atlas.cli import evaluate
    monkeypatch.setitem(indexed["agents"], "eval", {"sets": [str(SET)]})
    assert evaluate(indexed) == 0
    out = capsys.readouterr().out
    assert "ok   graph-impact-log  impact Logger.Log" in out
    assert "search, lexical: hit@1" in out and "graph: 10 of 10 right (100%)" in out and "agents:" not in out
    assert "    paraphrase (6): hybrid hit@3" in out and "    trace: 4 of 4" in out
    report = json.loads((indexed.workspace / "reports" / "eval-retrieval.json").read_text())
    assert set(report) == {"meta", "retrieval"} and report["retrieval"]["graph"]["passed"] == 10
    from atlas import __version__
    assert report["meta"]["set"] == "retrieval" and report["meta"]["version"] == __version__
    kept = list((indexed.workspace / "reports").glob(f"eval-retrieval-{__version__}-*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text()) == report


def test_compare_with_the_report_of_another_version(indexed, monkeypatch, capsys):
    """eval-<set>-<version>-<date>.json stays; --compare lists each case against the last
    report of another version, and the regressions."""
    import atlas
    from atlas.agents.evaluate import compare_reports, previous_report
    from atlas.cli import evaluate
    monkeypatch.setitem(indexed["agents"], "eval", {"sets": [str(SET)]})
    assert evaluate(indexed, compare=True) == 0
    assert "nothing to compare with: no report of retrieval from a version other than" in capsys.readouterr().out

    # an older version that ranked a search better and failed a query of the graph
    reports = indexed.workspace / "reports"
    old = json.loads((reports / "eval-retrieval.json").read_text())
    lower = next(r for r in old["retrieval"]["results"] if r["kind"] == "search" and (r["lexical"]["rank"] or 99) > 1)
    now = lower["lexical"]["rank"] or "-"
    for r in old["retrieval"]["results"]:
        if r["id"] == lower["id"]:
            r["lexical"]["rank"] = 1             # first before: what it gets now is a regression
        if r["id"] == "graph-impact-log":
            r["passed"] = False
    old["retrieval"]["results"].append({"id": "gone", "kind": "graph", "passed": True})
    old["meta"].update(version="0.0.9", date="2026-01-01T10:00:00")
    (reports / "eval-retrieval-0.0.9-2026-01-01.json").write_text(json.dumps(old))
    older = {**old, "meta": {**old["meta"], "version": "0.0.8", "date": "2025-12-01T10:00:00"}}
    (reports / "eval-retrieval-0.0.8-2025-12-01.json").write_text(json.dumps(older))

    assert evaluate(indexed, compare=True) == 1           # a regression
    out = capsys.readouterr().out
    assert f"compared with atlas 0.0.9 of 2026-01-01T10:00:00" in out
    hybrid = lower["hybrid"]["rank"] or "-"
    assert f"    REGRESSION {lower['id']}  hybrid {hybrid} lexical 1 -> hybrid {hybrid} lexical {now}" in out
    assert "    better     graph-impact-log  fail -> ok" in out and "    same       search-id-srs-0102" in out
    assert "no longer in the set: gone" in out and f"regressions: {lower['id']}\n" in out

    new = json.loads((reports / "eval-retrieval.json").read_text())
    cmp = compare_reports(old, new)
    assert cmp["regressions"] == [lower["id"]] and cmp["removed"] == ["gone"]
    assert cmp["against"]["version"] == "0.0.9" and len(cmp["cases"]) == 30
    # from 0.0.9, the last report of another version is the one just written
    assert previous_report(indexed, "retrieval", "0.0.9")["meta"]["version"] == atlas.__version__
