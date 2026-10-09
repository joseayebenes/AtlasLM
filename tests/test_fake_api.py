import json

import pytest


def _service(url):
    from atlas.llm import NetConfig, ServiceConfig
    return ServiceConfig(base_url=url, model="fake", api_key="x", net=NetConfig(proxy="", no_proxy="*"))


def test_chat_and_embeddings_without_a_model(fake):
    url, control = fake
    from atlas.llm import create_client
    cl, _ = create_client(_service(url))
    r = cl.chat.completions.create(model="fake", messages=[{"role": "user", "content": "Reply with: ok"}])
    assert r.choices[0].message.content == "ok"
    e = cl.embeddings.create(model="fake", input=["a", "b"])
    assert len(e.data) == 2 and len(e.data[0].embedding) == 64
    control.write_text(json.dumps({"down": True}))
    with pytest.raises(Exception) as ex:
        cl.chat.completions.create(model="fake", messages=[{"role": "user", "content": "x"}])
    assert "401" in str(ex.value) or "AuthenticationError" in type(ex.value).__name__


def test_check_reports_a_reasoning_model(fake, capsys):
    url, control = fake
    from atlas.llm import Connection, check
    control.write_text(json.dumps({"reasoning_tokens": 20}))
    assert check(Connection(llm=_service(url), embeddings=_service(url))) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "'ok' (reasoning model:" in out


def test_check_fails_when_reasoning_leaves_no_answer(fake, capsys):
    url, control = fake
    from atlas.llm import Connection, check
    from atlas.llm.connection import CHECK_MAX_TOKENS
    control.write_text(json.dumps({"reasoning_tokens": CHECK_MAX_TOKENS}))
    assert check(Connection(llm=_service(url), embeddings=_service(url))) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "budget reasoning" in out
