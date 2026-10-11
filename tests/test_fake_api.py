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


def test_check_and_from_config_use_reasoning_effort(fake, capsys, tmp_path):
    url, control = fake
    from atlas.config import load_config
    from atlas.llm import Connection, check, from_config
    from atlas.llm.connection import CHECK_MAX_TOKENS
    control.write_text(json.dumps({"reasoning_tokens": CHECK_MAX_TOKENS}))
    toml = tmp_path / "atlas.toml"
    toml.write_text(f'[llm]\nbase_url = "{url}"\nmodel = "fake"\nreasoning_effort = "none"\n'
                    f'[network]\nproxy = ""\nno_proxy = "*"\n')
    conn = from_config(load_config(toml))
    assert conn.llm.reasoning_effort == "none"
    assert check(Connection(llm=conn.llm, embeddings=_service(url))) == 0
    assert "OK" in capsys.readouterr().out


def test_usage_counts_the_requests_by_endpoint_and_the_rejections(fake):
    url, control = fake
    from atlas.llm import NetConfig, ServiceConfig, create_client, usage
    cl, _ = create_client(ServiceConfig(base_url=url, model="fake", api_key="usage", net=NetConfig(
        proxy="", no_proxy="*", adaptive=False, max_backoff=0.05, rate_limit_max_wait=0.2)))
    before = usage()
    cl.chat.completions.create(model="fake", messages=[{"role": "user", "content": "Reply with: ok"}])
    cl.embeddings.create(model="fake", input=["a", "b"])
    control.write_text(json.dumps({"emb_409_after": 0}))       # every embeddings request is rejected
    with pytest.raises(Exception):
        cl.embeddings.create(model="fake", input=["a"])
    grown = {k: v - before[k] for k, v in usage().items()}
    assert grown["chat"] == 1 and grown["other"] == 0 and grown["retried"] == 0
    assert grown["rejected"] >= 2 and grown["embeddings"] == 1 + grown["rejected"]
    assert grown["wait_seconds"] > 0 and grown["api_seconds"] > 0 and grown["sent_kb"] > 0
