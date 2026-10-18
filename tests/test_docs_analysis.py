import pytest

from atlas.docs import DocsOptions
from atlas.docs.analysis import (consolidate, merge_analyses, normalize, regex_requirements,
                                 split_in_two, verify)


def test_regex_requirements_boundaries():
    body = ("Req: SRS-0001 The TMF shall process track event 0 of\ngroup 1.1.1 within 10 ms.\n"
            "Track handling 1.1.2\n\n"
            "Req: [SRS-0002]: The TMF shall log.\n"
            "Req: SRS-0003\n\nThe TMF shall restart\nwithin 5 s.\n\nUnrelated paragraph.")
    reqs = regex_requirements(body, r"(?i)\bReq\s*:\s*(\S+)")
    assert reqs == [
        {"id": "SRS-0001", "text": "The TMF shall process track event 0 of group 1.1.1 within 10 ms."},
        {"id": "SRS-0002", "text": "The TMF shall log."},
        {"id": "SRS-0003", "text": "The TMF shall restart within 5 s."}]
    # group 1 with the whole line, as in the example TOMLs: same result
    assert regex_requirements(body, r"(?i)\bReq\s*:\s*([^\n]+)") == reqs
    # a page break (blank lines) in the middle of a sentence does not end it
    assert regex_requirements("Req: R-1 The TMF shall go\n\n\ninto safe mode.\n\nReq: R-2 The TMF shall stop\n\n"
                              "Unrelated paragraph.", r"Req: (\S+)") == \
        [{"id": "R-1", "text": "The TMF shall go into safe mode."}, {"id": "R-2", "text": "The TMF shall stop"}]
    # without a group the text starts at the match and has no ID
    assert regex_requirements("Intro.\nThe HMI shall beep.\n", r"The \w+ shall")[0] == \
        {"id": "", "text": "The HMI shall beep."}


def test_regex_requirements_with_the_ids_of_the_convention():
    from atlas.doors import id_regex
    ids = id_regex({"id_pattern": "^(?:SRS|SSS)-\\d{4}$"})
    body = ("Req: SRS-0004 and SRS-0005 The TMF shall delete.\n"
            "Req: see SRS-0006 The TMF shall stop.\n"
            "Req: SRS-0007 / SRS-0008: The TMF shall go.\n"
            "Req: [SRS-0002]: The TMF shall log.\n"
            "Req: The TMF shall wait as SRS-0100 says.\n")
    assert regex_requirements(body, r"(?i)\bReq\s*:\s*(\S+)") == [          # without it: the first token
        {"id": "SRS-0004", "text": "and SRS-0005 The TMF shall delete."},
        {"id": "see", "text": "SRS-0006 The TMF shall stop."},
        {"id": "SRS-0007", "text": "/ SRS-0008: The TMF shall go."},
        {"id": "SRS-0002", "text": "The TMF shall log."},
        {"id": "The", "text": "TMF shall wait as SRS-0100 says."}]
    expected = [{"id": "SRS-0004", "text": "The TMF shall delete."}, {"id": "SRS-0005", "text": "The TMF shall delete."},
                {"id": "SRS-0006", "text": "The TMF shall stop."},
                {"id": "SRS-0007", "text": "The TMF shall go."}, {"id": "SRS-0008", "text": "The TMF shall go."},
                {"id": "SRS-0002", "text": "The TMF shall log."},
                {"id": "", "text": "The TMF shall wait as SRS-0100 says."}]      # an ID it cites is not its own
    assert regex_requirements(body, r"(?i)\bReq\s*:\s*(\S+)", ids) == expected
    assert regex_requirements(body, r"(?i)\bReq\s*:\s*([^\n]+)", ids) == expected
    with pytest.raises(ValueError, match="id_regex"):
        DocsOptions(id_regex="(")


def test_the_ids_that_the_analysis_gives_in_the_graph():
    """An ID of the model as "SRS-0004 / SRS-0005" or "Req. SRS-0006" is split with the
    expression of an ID, and id_canonical brings "SRS 7" to the form of DOORS."""
    import re
    pytest.importorskip("networkx")
    from atlas.doors import Canonical, id_regex, normalize_id
    from atlas.graph import build_docs_graph
    structure = {"title": "Spec", "sections": [{"id": "001", "title": "1", "body": "", "analysis": {"requirements": [
        {"id": "SRS-0004 / SRS-0005", "text": "The TMF shall delete."}, {"id": "Req. SRS-0006", "text": "Stop."},
        {"id": "SRS 7", "text": "Go."}]}}]}
    doc = [{"name": "spec.pdf", "path": "spec.pdf", "out_dir": "out", "structure": structure}]
    G, stats = build_docs_graph(doc)
    assert sorted(n for n, d in G.nodes(data=True) if d["kind"] == "requirement") == ["REQ.SRS-0006", "SRS7"]  # before
    canonical = Canonical({"regex": r"^(SRS|SSS)[-_ ]?0*(\d+)$", "format": "{1}-{2:04d}"})
    loose = re.compile(r"(?i)\b(?:SRS|SSS)[-_ ]?\d+\b")
    G, stats = build_docs_graph(doc, normalize=lambda r: normalize_id(r, "upper", canonical), ids=loose)
    assert sorted(n for n, d in G.nodes(data=True) if d["kind"] == "requirement") == \
        ["SRS-0004", "SRS-0005", "SRS-0006", "SRS-0007"]
    assert [s["prefix"] + s["separator"] for s in stats["id_shapes"]] == ["SRS-", "SRS "]
    G, _ = build_docs_graph(doc, ids=re.compile(id_regex({"id_pattern": "^SRS-\\d{4}$"})))
    assert G.nodes["doc:spec.pdf#001"]["requirements"] == [
        {"id": "SRS-0004", "text": "The TMF shall delete."}, {"id": "SRS-0005", "text": "The TMF shall delete."},
        {"id": "SRS-0006", "text": "Stop."}]


def test_verify_consolidate_and_merge():
    assert verify("The TMF shall log.", "x\nThe TMF shall\nlog. y") == "literal"
    assert verify("The TMF shall log every event", "The TMF shall log each event") == "not_found"
    a = normalize({"requirements": [{"id": "R-1", "text": "The TMF shall log."}],
                   "definitions": [{"term": "TMF", "definition": "Track Management Function"}],
                   "entities": [{"name": "TMF", "kind": "function", "description": ""}],
                   "open_issues": ["latency TBD"]})
    b = normalize({"requirements": [{"id": "r-1", "text": "duplicate by ID"}],
                   "entities": [{"name": "tmf", "kind": "system", "description": "the function"},
                                {"name": "TMF", "kind": "function", "description": ""}]})
    sections = [{"id": "001", "title": "One", "body": "The TMF shall log.", "analysis": a},
                {"id": "002", "title": "Two", "body": "other", "analysis": b}]
    cons = consolidate(sections)
    assert [(r["id"], r["status"], r["section"]) for r in cons["requirements"]] == [("R-1", "literal", "001")]
    assert cons["glossary"][0]["sections"] == ["001"]
    assert cons["entities"] == [{"name": "TMF", "kind": "function", "description": "the function",
                                 "sections": ["001", "002"]}]
    assert cons["open_issues"] == [{"text": "latency TBD", "section": "001", "section_title": "One"}]
    merged = merge_analyses([a, b])
    assert [r["text"] for r in merged["requirements"]] == ["The TMF shall log."]
    # same text with another ID: two requirements
    c = normalize({"requirements": [{"id": "R-2", "text": "The TMF shall log."}]})
    assert [r["id"] for r in merge_analyses([a, c])["requirements"]] == ["R-1", "R-2"]
    assert split_in_two("one\n\ntwo\n\nthree") == ["one\n\ntwo", "three"]


def test_options_are_validated():
    assert DocsOptions.from_source({"name": "n", "paths": ["x"], "analysis": "regex", "max_chars": 4000}) == \
        DocsOptions(analysis="regex", max_chars=4000)
    for bad, match in (({"analysis": "gpt"}, "analysis"), ({"structure": "pages"}, "structure"),
                       ({"req_regex": "(unclosed"}, "req_regex"), ({"max_chars": 10}, "max_chars")):
        with pytest.raises(ValueError, match=match):
            DocsOptions(**bad)


def test_normalize_turns_objects_into_texts():
    # what gemma4 answered for open_issues when the server could not enforce the schema
    d = normalize({"open_issues": [{"issue": "Alarm latency is TBD", "description": "not defined yet"}],
                   "key_points": ["a", None, ""], "summary": None,
                   "requirements": [{"id": None, "text": "The HMI shall beep."}, "not an object"]})
    assert d["open_issues"] == ["Alarm latency is TBD: not defined yet"]
    assert d["key_points"] == ["a"] and d["summary"] == ""
    assert d["requirements"] == [{"id": "", "text": "The HMI shall beep."}]
