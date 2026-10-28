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
    for text in ("- id: a\n  expect: x\n",                                     # nothing to ask
                 "- id: a\n  search: x\n  trace: SRS-0001\n  expect: x\n",     # two things
                 "- id: a\n  matrix: SRS_TMS\n  expect: x\n"):                 # not a query of the set
        bad.write_text(text)
        with pytest.raises(ConfigError, match="one of question, search, ask or a query of the graph"):
            load_cases(bad)
    bad.write_text("- id: a\n  search: x\n  expect: x\n  tags: paraphrase\n")
    with pytest.raises(ConfigError, match="tags is a list of words"):
        load_cases(bad)
    for text, error in (("- id: a\n  ask: x\n  expect: x\n  scope: all\n", "scope is one of auto, passages, whole"),
                        ("- id: a\n  ask: x\n  expect: x\n  cited: yes please\n", "cited is true or false")):
        bad.write_text(text)
        with pytest.raises(ConfigError, match=error):
            load_cases(bad)
    asks = {c["id"]: c for c in load_cases(SET.parent / "assistant.yaml") if c["kind"] == "ask"}
    assert len(asks) == 4 and asks["ask-whole-alarms"]["scope"] == "whole" and asks["ask-days-of-logs"]["cited"]


def test_ask_cases_check_the_answer_and_its_citations():
    """Each expected text in the answer and, with cited, in what a cited source read."""
    from atlas.agents.evaluate import evaluate_answers

    class FakeIndex:
        def answer(self, question, document="", scope=""):
            if question == "broken":
                raise RuntimeError("the model is down")
            sources = [{"n": 1, "cite": "hmi_manual.md › 2 Alarms", "context": "raise an alarm"},
                       {"n": 2, "cite": "hmi_manual.md › 3 Logging", "context": "keep logs for 30 days"}]
            n = 2 if "right" in question else 1
            return {"answer": f"30 days [{n}].", "sources": sources, "scope": scope or "passages",
                    "documents": [], "requests": 2, "status": "answered"}

    cases = [{"id": "right", "kind": "ask", "ask": "right source", "expect": ["30"], "cited": True, "tags": ["t"]},
             {"id": "wrong", "kind": "ask", "ask": "wrong source", "expect": ["30"], "cited": True},
             {"id": "uncited", "kind": "ask", "ask": "wrong source", "expect": ["30 days"], "scope": "whole"},
             {"id": "section", "kind": "ask", "ask": "x", "expect": ["30"], "section": "3"},
             {"id": "broken", "kind": "ask", "ask": "broken", "expect": ["30"]},
             {"id": "agent", "kind": "agent", "question": "x", "expect": ["30"]}]
    seen = []
    res = evaluate_answers(cases, None, runs=2, index=FakeIndex(), on_result=seen.append)
    by = {r["id"]: r for r in res["results"] if r["run"] == 1}
    assert (res["passed"], res["total"], res["requests"]) == (4, 10, 12) and len(seen) == 10
    assert by["right"]["passed"] and by["right"]["cited"] == ["hmi_manual.md › 3 Logging"]
    assert by["right"]["status"] == "answered" and res["tags"] == {"t": {"passed": 2, "total": 2, "rate": 1.0}}
    assert not by["wrong"]["passed"] and by["wrong"]["uncited"] == ["30"] and by["wrong"]["missing"] == []
    assert by["uncited"]["passed"] and by["uncited"]["scope"] == "whole"
    assert "takes no section" in by["section"]["error"] and "the model is down" in by["broken"]["error"]
    assert res["by_case"] == {"right": "2/2", "wrong": "0/2", "uncited": "2/2", "section": "0/2", "broken": "0/2"}


def test_an_id_written_with_a_unicode_dash_is_the_id():
    """gpt-oss-120b writes "SRS‑0004" with U+2011: a literal comparison gave 60 % for ~94 %."""
    from atlas.agents.evaluate import evaluate_answers
    from atlas.text import plain

    class FakeIndex:
        def answer(self, question, document="", scope=""):
            context = "SRS–0004 refines SRS-0003"                       # an en dash, in a PDF
            return {"answer": "SRS‑0005 refines SRS‑0004 [1].", "sources": [{"n": 1, "context": context}]}

    cases = [{"id": "dash", "kind": "ask", "ask": "x", "expect": ["SRS-0005", "srs-0004"], "cited": False},
             {"id": "cited", "kind": "ask", "ask": "x", "expect": ["SRS-0004"], "cited": True},
             {"id": "other", "kind": "ask", "ask": "x", "expect": ["SRS-0006"]}]
    res = evaluate_answers(cases, None, index=FakeIndex())
    assert res["by_case"] == {"dash": "1/1", "cited": "1/1", "other": "0/1"}
    assert plain("SRS‑­0004 −") == "SRS-0004 -"


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
    assert set(report) == {"meta", "retrieval", "usage"} and report["retrieval"]["graph"]["passed"] == 10
    from atlas import __version__
    assert report["meta"]["set"] == "retrieval" and report["meta"]["version"] == __version__
    kept = list((indexed.workspace / "reports").glob(f"eval-retrieval-{__version__}-*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text()) == report


def test_an_ask_case_with_the_fake_api_and_the_meta_of_the_report(indexed, tmp_path, monkeypatch, capsys):
    from atlas.cli import evaluate
    cases = tmp_path / "answers.yaml"
    cases.write_text("- id: whole\n  ask: When does the HMI raise an alarm?\n  document: hmi_manual.md\n"
                     "  scope: whole\n  expect: [answer]\n  tags: [whole]\n"
                     "- id: cited\n  ask: When does the HMI raise an alarm?\n  expect: [answer]\n  cited: true\n")
    monkeypatch.setitem(indexed["agents"], "eval", {"sets": [str(cases)], "runs": 1})
    assert evaluate(indexed) == 0
    out = capsys.readouterr().out
    assert "ok   whole #1" in out and "[whole, 2 requests]" in out          # the rewriting and the answer
    assert "FAIL cited #1" in out and "not in a cited source: answer" in out
    assert "answers: 1 of 2 right (50%)" in out and "answers whole: 1 of 1" in out
    report = json.loads((indexed.workspace / "reports" / "eval-answers.json").read_text())
    assert set(report) == {"meta", "answers", "usage"} and report["answers"]["requests"] == 4
    meta = report["meta"]
    assert (meta["model"], meta["embeddings_model"], meta["runs"], meta["provider"]) == ("fake", "embeddinggemma", 1, None)
    assert len(meta["yaml_sha256"]) == 16 and meta["set"] == "answers" and "reasoning_effort" in meta


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
    assert "compared with atlas 0.0.9 of 2026-01-01T10:00:00" in out
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


def test_a_set_generated_from_the_workspace(indexed, tmp_path, capsys):
    """atlas eval --generate: search and graph cases from atlas.db and the graph, the same
    each time, that the evaluation then finds."""
    from atlas.agents.evaluate import evaluate_retrieval, generate_cases, load_cases
    from atlas.cli import main
    cases = generate_cases(indexed, 12)
    assert cases == generate_cases(indexed, 12) and len(cases) == 12          # a fixed seed
    kinds = [c["tags"] for c in cases]
    assert all(t[0] == "generated" for t in kinds) and {t[1] for t in kinds} == {"id", "section", "trace", "impact"}
    for c in cases:
        if "trace" in c:            # the requirement, its code and its tests
            assert c["expect"][0] == c["trace"] and len(c["expect"]) >= 2
        elif "impact" in c:         # its callers
            assert c["expect"] and c["impact"] not in c["expect"]
        elif "section" in c["tags"]:
            assert c["expect"][0].startswith("section:") and "#" in c["expect"][0]
        else:
            assert c["expect"] == [c["search"]]

    path = tmp_path / "project.yaml"
    with pytest.raises(SystemExit) as e:
        main(["eval", "--generate", str(path), "--n", "12", str(indexed.path)])
    assert e.value.code == 0 and "12 cases (" in capsys.readouterr().out
    assert path.read_text().startswith("# Generated by atlas eval --generate from atlas.toml")
    generated = load_cases(path)
    assert [c["id"] for c in generated] == [c["id"] for c in cases]
    res = evaluate_retrieval(generated, indexed)
    assert res["graph"]["passed"] == res["graph"]["total"] > 0
    assert res["search"]["lexical"]["hit@10"] == 1.0                          # every ID and title found

    with pytest.raises(SystemExit) as e:                                       # written again: the same
        main(["eval", "--generate", str(path), "--n", "12", str(indexed.path)])
    assert load_cases(path) == generated
    hand = tmp_path / "hand.yaml"
    hand.write_text("- id: a\n  search: x\n  expect: x\n")
    for args, error in ((["--generate", str(hand)], "was not generated by atlas eval --generate: it is not overwritten"),
                        (["--n", "3"], "--n goes with --generate FILE"),
                        (["--generate", str(path), "--compare"], "not --compare")):
        with pytest.raises(SystemExit) as e:
            main(["eval", *args, str(indexed.path)])
        assert error in str(e.value.code)
    assert hand.read_text() == "- id: a\n  search: x\n  expect: x\n"


DEVELOP = Path(__file__).resolve().parents[1] / "hephestolm" / "examples" / "develop.yaml"


def test_develop_cases(tmp_path):
    from atlas.agents.evaluate import load_cases
    case = load_cases(DEVELOP)[0]
    assert (case["kind"], case["develop"], case["from"], case["expect_tests_passed"]) == (
        "develop", "hephestolm/examples/develop/calc/plan.toml", "hephestolm/examples/develop/calc/start", 2)
    bad = tmp_path / "bad.yaml"
    for text, error in (("- id: a\n  develop: plan.toml\n  expect_tests_passed: 2\n", "has id, develop, from"),
                        ("- id: a\n  develop: p\n  from: s\n  expect_tests_passed: 2\n  expect: [x]\n", "not \\['expect'\\]"),
                        ("- id: a\n  develop: p\n  from: s\n  expect_tests_passed: all\n", "integers"),
                        ("- id: a\n  search: x\n  expect: x\n  runs: 2\n", "unknown keys \\['runs'\\]")):
        bad.write_text(text)
        with pytest.raises(ConfigError, match=error):
            load_cases(bad)


def test_atlas_eval_leaves_the_develop_cases_to_hephesto(toml, capsys):
    """atlas eval checks a develop case and counts it, without running it (no runner)."""
    from atlas.cli import evaluate
    cfg = load_config(toml())
    assert evaluate(cfg, set=str(DEVELOP)) == 0
    assert "develop: 1 cases not run: hephesto eval runs them" in capsys.readouterr().out
    assert json.loads((cfg.workspace / "reports" / "eval-develop.json").read_text())["skipped"] == {"develop": 1}
