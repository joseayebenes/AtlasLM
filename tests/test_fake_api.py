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


def _status_error(status: int, message: str, **metadata):
    import httpx
    from openai import OpenAI
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
                              json={"error": {"message": message, "code": status,
                                              **({"metadata": metadata} if metadata else {})}})
    return OpenAI(api_key="x", base_url="https://api.example/v1")._make_status_error_from_response(response)


def test_no_credit_is_fatal_and_the_server_message_is_shown():
    from atlas.llm import diagnose, is_connection_error
    no_credit = _status_error(402, "Insufficient credits. Add more using https://openrouter.ai/settings/credits")
    assert is_connection_error(no_credit)
    assert "HTTP 402" in diagnose(no_credit) and "Insufficient credits" in diagnose(no_credit)
    no_endpoints = _status_error(404, "No endpoints found matching your data policy")
    assert not is_connection_error(no_endpoints)
    assert "model name (the server says: No endpoints found matching your data policy)" in diagnose(no_endpoints)
    upstream = _status_error(429, "Provider returned error", provider_name="Mistral",
                             raw="mistralai/mistral-small-2603 is temporarily rate-limited upstream")
    assert "(the server says: Provider returned error: mistralai/mistral-small-2603 is temporarily rate-limited " \
           "upstream)" in diagnose(upstream)


def test_a_reset_in_epoch_milliseconds_is_not_a_pause_of_minutes():
    import time
    from atlas.llm.connection import _wait_from_headers
    reset = time.time() + 30
    for value in (f"{reset:.0f}", f"{reset * 1000:.0f}"):          # seconds (most) or milliseconds (OpenRouter)
        wait = _wait_from_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": value})
        assert 28 <= wait <= 31, value
    assert _wait_from_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "6m0s"}) == 360


def test_extra_body_goes_as_is_in_every_chat_request(fake, fake_stats, capsys, tmp_path):
    url, control = fake
    from atlas.config import load_config
    from atlas.llm import Connection, JsonLlm, check, from_config
    toml = tmp_path / "atlas.toml"
    toml.write_text(f'[llm]\nbase_url = "{url}"\nmodel = "fake"\n'
                    f'[llm.extra_body]\nprovider = {{ order = ["deepinfra"], allow_fallbacks = false }}\n'
                    f'[network]\nproxy = ""\nno_proxy = "*"\n')
    conn = from_config(load_config(toml))
    provider = {"order": ["deepinfra"], "allow_fallbacks": False}
    assert check(Connection(llm=conn.llm, embeddings=_service(url))) == 0
    assert 'extra_body={"provider": {"order": ["deepinfra"]' in capsys.readouterr().out
    assert fake_stats()["last_chat"]["provider"] == provider
    JsonLlm(conn.llm).ask_json("Answer in JSON.", "Overall?", {"type": "object", "properties": {
        "summary": {"type": "string"}}}, "overview")
    last = fake_stats()["last_chat"]
    assert last["provider"] == provider and last["response_format"]["type"] == "json_schema"
