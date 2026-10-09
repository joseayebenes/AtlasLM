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
