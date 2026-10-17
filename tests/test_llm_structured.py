import json

import pytest

from atlas.llm import JsonLlm, TruncatedResponse, parse_json, repair_json, validate

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


def test_validate_names_the_first_place_that_breaks_the_schema():
    schema = {"type": "object", "required": ["verdict", "requirements"], "properties": {
        "verdict": {"type": "string", "enum": ["yes", "no"]},
        "count": {"type": "integer"},
        "requirements": {"type": "array", "items": {"type": "object", "required": ["id", "text"], "properties": {
            "id": {"type": "string"}, "text": {"type": "string"}}}}}}
    good = {"verdict": "yes", "count": 2, "requirements": [{"id": "R1", "text": "a"}]}
    assert validate(good, schema) == []
    bad = {"verdict": "maybe", "count": True,
           "requirements": [{"id": "R1", "text": "a"}, {"id": "R2"}, {"id": "R3", "text": 3}]}
    assert validate(bad, schema) == ['verdict must be one of "yes", "no"', "count must be an integer",
                                     "requirements[1].text is missing", "requirements[2].text must be a string"]
    assert validate([], schema) == ["the answer must be an object"]
    assert validate({}, schema) == ["verdict is missing", "requirements is missing"]


def test_the_correction_quotes_what_breaks_the_schema(fake_llm):
    service, control = fake_llm
    llm = JsonLlm(service)
    sent, real = [], llm.client.chat.completions.create
    llm.client.chat.completions.create = lambda **kwargs: sent.append(kwargs["messages"]) or real(**kwargs)
    numbered = {**SCHEMA, "properties": {**SCHEMA["properties"], "requirements": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}}}}}}
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", numbered, "section")
    assert len(sent) == 2
    assert sent[1][-1]["content"].startswith("That JSON object does not follow the schema: "
                                             "requirements[0].id must be an integer.")
    # the correction (the fake answers an overview without requirements) is no better: the
    # first answer is kept
    assert d["requirements"][0]["id"] == "SRS-0001"


def test_ask_json_against_the_fake_api(fake_llm, fake_chats):
    service, control = fake_llm
    llm = JsonLlm(service)
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", SCHEMA, "section")
    assert d["requirements"] == [{"id": "SRS-0001", "text": "The TMF shall work."}]

    # prose: one request for a correction, which the fake answers with JSON
    control.write_text(json.dumps({"section_fail_if": ["broken"]}))
    failures = []
    before = fake_chats()
    d = llm.ask_json("system", "<section>broken</section>", SCHEMA, "section",
                     on_failure=lambda attempt, text, finish: failures.append(attempt))
    assert d["summary"] and fake_chats() - before == 2 and failures == [1]

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


def test_servers_without_structured_output(fake_llm, fake_chats):
    service, control = fake_llm
    control.write_text(json.dumps({"no_structured_output": True}))
    llm = JsonLlm(service)
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", SCHEMA, "section")
    assert d["requirements"][0]["id"] == "SRS-0001" and llm.structured == "none"
    before = fake_chats()                       # from then on, no request with response_format,
    JsonLlm(service).ask_json("system", "<section>x</section>", SCHEMA, "section")   # in any instance
    assert fake_chats() - before == 1


def test_reasoning_can_be_turned_off(fake_llm):
    service, control = fake_llm
    control.write_text(json.dumps({"reasoning_tokens": 100}))
    service.max_tokens = 50                      # all of it would go to reasoning
    with pytest.raises(TruncatedResponse):
        JsonLlm(service).ask_json("system", "<section>x</section>", SCHEMA, "section")
    service.reasoning_effort = "none"
    assert JsonLlm(service).ask_json("system", "<section>x</section>", SCHEMA, "section")["summary"]


def test_concurrency_caps_the_requests_in_flight(fake_llm):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from atlas.llm import JsonLlm
    llm = JsonLlm(fake_llm[0], concurrency=2)
    state, lock, real = {"now": 0, "max": 0}, threading.Lock(), llm.client.chat.completions.create

    def create(**kwargs):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        try:
            return real(**kwargs)
        finally:
            with lock:
                state["now"] -= 1

    llm.client.chat.completions.create = create
    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(lambda _: llm.ask_text("system", "Reply with: ok"), range(6)))
    assert answers == ["ok"] * 6 and state["max"] == 2


def test_a_server_without_json_schema_gets_json_object(fake_llm, fake_stats):
    service, control = fake_llm
    control.write_text(json.dumps({"no_structured_output": "json_schema"}))
    llm = JsonLlm(service)
    assert llm.structured == "json_schema"
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", SCHEMA, "section")
    assert d["requirements"][0]["id"] == "SRS-0001" and llm.structured == "json_object"
    assert fake_stats()["last_chat"]["response_format"] == {"type": "json_object"}
    before = fake_stats()["chat"]                 # another instance, same server: no rejected request
    JsonLlm(service).ask_json("system", "<section>x</section>", SCHEMA, "section")
    assert fake_stats()["chat"] - before == 1 and fake_stats()["last_chat"]["response_format"]["type"] == "json_object"
    other = JsonLlm(type(service)(**{**service.__dict__, "model": "another"}))   # another model starts again
    assert other.structured == "json_schema"


def test_ask_text_asks_again_when_the_answer_is_cut(fake_llm, fake_chats):
    from dataclasses import replace
    service, control = fake_llm
    control.write_text(json.dumps({"usage_chars_per_token": 4}))
    question = "Sources:\n\n[1] Some text.\n\nQuestion: what?"      # "Answer taken from the sources [1]." is 9 tokens
    before = fake_chats()
    text = JsonLlm(replace(service, max_tokens=5)).ask_text("system", question)   # cut at 5, whole at 10
    assert (text, text.truncated, text.requests) == ("Answer taken from the sources [1].", False, 2)
    assert fake_chats() - before == 2
    text = JsonLlm(replace(service, max_tokens=2)).ask_text("system", question)   # cut at 2 and at 4
    assert (text, text.truncated, text.requests) == ("Answer taken fro", True, 2)
    text = JsonLlm(replace(service, max_tokens=50)).ask_text("system", question)  # whole: one request
    assert (text.truncated, text.requests) == (False, 1)

    # a reasoning model that spends the whole budget thinking: empty, and asked again with twice
    control.write_text(json.dumps({"reasoning_tokens": 80}))
    text = JsonLlm(replace(service, max_tokens=50)).ask_text("system", "Reply with: ok")
    assert (text, text.truncated) == ("ok", False)
    text = JsonLlm(replace(service, max_tokens=20)).ask_text("system", "Reply with: ok")
    assert (text, text.truncated) == ("", True)


def test_answers_come_streamed_and_put_together(fake_llm, fake_stats):
    service, control = fake_llm
    control.write_text(json.dumps({"reasoning_tokens": 10, "usage_chars_per_token": 4, "chunk_chars": 3}))
    llm = JsonLlm(service)
    d = llm.ask_json("system", "<section>Req: SRS-0001 The TMF shall work.</section>", SCHEMA, "section")
    assert d["requirements"] == [{"id": "SRS-0001", "text": "The TMF shall work."}]
    text = llm.ask_text("system", "Sources:\n\n[1] Some text.\n\nQuestion: what?")
    assert (text, text.truncated) == ("Answer taken from the sources [1].", False)
    stats = fake_stats()
    assert stats["streams"] == stats["chat"] == 2 and stats["last_chat"]["stream_options"] == {"include_usage": True}

    from atlas.llm.chat import complete
    kwargs = {"model": "fake", "messages": [{"role": "user", "content": "Reply with: ok"}]}
    response = complete(llm.client, service, kwargs)                  # the reasoning and the usage come too
    assert response.choices[0].message.content == "ok" and response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.model_extra["reasoning"] == "The user wants a short answer. "
    assert response.usage.completion_tokens == 11


def test_a_server_that_cannot_stream_is_asked_without_it_for_the_rest_of_the_process(fake_llm, fake_stats, caplog):
    from dataclasses import replace
    service, control = fake_llm
    control.write_text(json.dumps({"no_stream": True}))               # it rejects stream: 400 that names it
    assert JsonLlm(service).ask_text("system", "Reply with: ok") == "ok"
    assert fake_stats()["chat"] == 2 and "does not stream" in caplog.text      # the rejected one and again
    assert JsonLlm(service).ask_text("system", "Reply with: ok") == "ok"   # another instance: straight away
    assert "stream" not in fake_stats()["last_chat"] and fake_stats()["chat"] == 3

    ignores = replace(service, model="ignores")                      # it ignores stream: that answer is taken
    control.write_text(json.dumps({"no_stream": "ignore"}))
    assert JsonLlm(ignores).ask_text("system", "Reply with: ok") == "ok"
    assert fake_stats()["chat"] == 4 and fake_stats()["streams"] == 0
    assert JsonLlm(ignores).ask_text("system", "Reply with: ok") == "ok" and "stream" not in fake_stats()["last_chat"]


def test_stream_true_or_false_forces_it(fake_llm, fake_stats, tmp_path):
    from dataclasses import replace
    from atlas.config import ConfigError, load_config
    service, control = fake_llm
    JsonLlm(replace(service, stream="false")).ask_text("system", "Reply with: ok")
    assert fake_stats()["streams"] == 0 and "stream" not in fake_stats()["last_chat"]
    control.write_text(json.dumps({"no_stream": True}))
    with pytest.raises(RuntimeError, match="stream = true, but the server does not stream"):
        JsonLlm(replace(service, stream=True)).ask_text("system", "Reply with: ok")
    (tmp_path / "atlas.toml").write_text('[llm]\nstream = "sometimes"\n')
    with pytest.raises(ConfigError, match="stream must be true, false"):
        load_config(tmp_path / "atlas.toml")
    (tmp_path / "atlas.toml").write_text('[llm]\nstream = false\n')
    from atlas.llm import from_config
    from atlas.llm.chat import streaming
    assert streaming(from_config(load_config(tmp_path / "atlas.toml")).llm) is False
