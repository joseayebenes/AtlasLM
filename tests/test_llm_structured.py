import json
import urllib.request

import pytest

from atlas.llm import JsonLlm, TruncatedResponse, parse_json, repair_json

KEYS = ["summary", "requirements"]
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}, "requirements": {"type": "array"}},
          "required": KEYS}


def test_parse_json_finds_the_answer_among_prose_and_blocks():
    text = 'Thinking about {braces}...\n```json\n{"summary": "s", "requirements": [{"id": "R1"}]}\n```\nDone.'
    assert parse_json(text, KEYS) == {"summary": "s", "requirements": [{"id": "R1"}]}
    with pytest.raises(ValueError):
        parse_json("no json here", KEYS)
    with pytest.raises(ValueError):                    # an inner object is not the answer
        parse_json('{"id": "R1", "text": "t"}', KEYS)


def test_repair_json_keeps_what_was_complete():
    cut = '{"summary": "s", "requirements": [{"id": "R1", "text": "a"}, {"id": "R2", "te'
    assert repair_json(cut, KEYS) == {"summary": "s", "requirements": [{"id": "R1", "text": "a"}, {"id": "R2"}]}
    assert repair_json('{"summary": "s", "requirements": [],}', KEYS) == {"summary": "s", "requirements": []}
    assert repair_json("nothing", KEYS) is None


def _chats(llm) -> int:
    return json.load(urllib.request.urlopen(llm.cfg.base_url.removesuffix("/v1") + "/"))["chat"]


def test_ask_json_against_the_fake_api(fake_llm):
    service, control = fake_llm
    llm = JsonLlm(service)
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", SCHEMA, "section")
    assert d["requirements"] == [{"id": "SRS-0001", "text": "The TMF shall work."}]

    # prose: one request for a correction, which the fake answers with JSON
    control.write_text(json.dumps({"section_fail_if": ["broken"]}))
    failures = []
    before = _chats(llm)
    d = llm.ask_json("system", "<section>broken</section>", SCHEMA, "section",
                     on_failure=lambda attempt, text, finish: failures.append(attempt))
    assert d["summary"] and _chats(llm) - before == 2 and failures == [1]

    # prose again after the correction: it gives up
    control.write_text(json.dumps({"prose": True}))
    failures.clear()
    with pytest.raises(RuntimeError, match="valid JSON"):
        llm.ask_json("system", "<section>x</section>", SCHEMA, "section",
                     on_failure=lambda attempt, text, finish: failures.append(attempt))
    assert failures == [1, 2]

    # an answer cut by length is never taken as good
    control.write_text(json.dumps({"reasoning_tokens": 100}))
    service.max_tokens = 50
    with pytest.raises(TruncatedResponse):
        JsonLlm(service).ask_json("system", "<section>x</section>", SCHEMA, "section")
