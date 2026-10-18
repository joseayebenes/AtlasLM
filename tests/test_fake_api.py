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
    assert "reasoning_effort is empty: documents will be analysed more slowly" in out


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
    out = capsys.readouterr().out
    assert "OK" in out and "[warning]" not in out


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
    assert grown["chat_seconds"] > 0 and grown["embeddings_seconds"] > 0 and grown["other_seconds"] == 0
    assert grown["api_seconds"] == pytest.approx(grown["chat_seconds"] + grown["embeddings_seconds"])
    assert grown["prompt_tokens"] == 0         # a bare client: the tokens are counted by note_usage


def test_note_usage_reads_tokens_reasoning_cache_and_cost(fake_llm):
    """Every answer adds what its usage says; JsonLlm and the embeddings go through it."""
    from types import SimpleNamespace
    from atlas.index import Embedder
    from atlas.llm import JsonLlm, note_usage, usage, usage_since
    answer = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=120, completion_tokens=30, cost=0.002,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=20),
        prompt_tokens_details=SimpleNamespace(cached_tokens=100)))
    before = usage()
    assert note_usage(answer) == {"prompt_tokens": 120, "completion_tokens": 30, "reasoning_tokens": 20,
                                  "cached_tokens": 100, "cost": 0.002}
    assert note_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=9)), "embeddings") == {"embedding_tokens": 9}
    assert note_usage(SimpleNamespace(usage=None)) == {} and note_usage(object()) == {}
    assert usage_since(before) == {"prompt_tokens": 120, "completion_tokens": 30, "reasoning_tokens": 20,
                                   "cached_tokens": 100, "cost": 0.002, "embedding_tokens": 9}

    service, control = fake_llm
    control.write_text(json.dumps({"cost": 0.001, "reasoning_tokens": 7}))
    before = usage()
    JsonLlm(service).ask_text("Answer.", "Reply with: ok")
    Embedder(service).queries(["track deletion"])
    grown = usage_since(before)
    assert grown["chat"] == 1 and grown["embeddings"] == 1 and grown["reasoning_tokens"] == 7
    assert grown["completion_tokens"] > 7 and grown["prompt_tokens"] > 0 and grown["embedding_tokens"] > 0
    assert grown["cost"] == 0.002


def test_the_warnings_of_the_limiter_go_to_the_atlas_llm_logger(fake, caplog, capsys):
    url, control = fake
    from atlas.llm import NetConfig, ServiceConfig, create_client
    cl, _ = create_client(ServiceConfig(base_url=url, model="fake", api_key="logger", net=NetConfig(
        proxy="", no_proxy="*", adaptive=False, max_backoff=0.05, rate_limit_max_wait=0.1)))
    control.write_text(json.dumps({"emb_409_after": 0}))
    with caplog.at_level("WARNING", logger="atlas.llm"), pytest.raises(Exception):
        cl.embeddings.create(model="fake", input=["a"])
    assert any(r.name == "atlas.llm" and "(rate limit): waiting" in r.getMessage() for r in caplog.records)
    assert "rate limit" not in capsys.readouterr().err          # not printed, logged


def test_a_configuration_that_cannot_work_raises_config_error(tmp_path):
    from atlas.config import ConfigError
    from atlas.llm import NetConfig
    from atlas.llm.connection import ssl_context
    with pytest.raises(ConfigError, match="CA certificate .* does not exist"):
        ssl_context(NetConfig(ca_cert=str(tmp_path / "missing.pem")))


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


def _client(url, key, **net):
    from atlas.llm import NetConfig, ServiceConfig, create_client
    return create_client(ServiceConfig(base_url=url, model="fake", api_key=key, net=NetConfig(
        proxy="", no_proxy="*", adaptive=False, **net)))[0]


OK = [{"role": "user", "content": "Reply with: ok"}]


def test_the_wait_follows_retry_after_and_the_ratelimit_headers(fake):
    import time
    from atlas.llm import usage
    url, control = fake
    cl = _client(url, "retry-after", max_backoff=0.05)
    control.write_text(json.dumps({"chat_status_once": [429], "retry_after": 1}))
    before, t0 = usage(), time.monotonic()
    assert cl.chat.completions.create(model="fake", messages=OK).choices[0].message.content == "ok"
    assert time.monotonic() - t0 >= 1 and usage()["rejected"] - before["rejected"] == 1

    # no Retry-After, but no requests left until the reset: everything waits for it
    control.write_text(json.dumps({"chat_status_once": [503], "ratelimit_headers": {"remaining": 0, "reset": 1}}))
    t0 = time.monotonic()
    assert cl.chat.completions.create(model="fake", messages=OK).choices[0].message.content == "ok"
    assert time.monotonic() - t0 >= 0.9


def test_server_errors_are_retried_and_a_rate_limit_that_lasts_is_given_up(fake, fake_stats):
    from openai import RateLimitError
    from atlas.llm import usage
    url, control = fake
    cl = _client(url, "statuses", max_backoff=0.05, rate_limit_max_wait=0.3)
    control.write_text(json.dumps({"chat_status_once": [500, 503], "retry_after": 0}))
    before = usage()
    assert cl.chat.completions.create(model="fake", messages=OK).choices[0].message.content == "ok"
    assert usage()["retried"] - before["retried"] == 2 and fake_stats()["rejections"] == 2

    control.write_text(json.dumps({"chat_429_after": fake_stats()["chat"] + 1, "retry_after": 0.1}))
    cl.chat.completions.create(model="fake", messages=OK)                        # the last one let through
    with pytest.raises(RateLimitError):
        cl.chat.completions.create(model="fake", messages=OK)
    assert fake_stats()["rejections"] >= 2 + 3


def test_usage_comes_from_the_size_of_the_request_and_the_answer(fake):
    import math
    url, control = fake
    cl = _client(url, "usage-size")
    control.write_text(json.dumps({"usage_chars_per_token": 4}))
    r = cl.chat.completions.create(model="fake", messages=OK)
    assert r.usage.prompt_tokens == math.ceil(len(json.dumps(OK)) / 4) and r.usage.completion_tokens == 1
    e = cl.embeddings.create(model="fake", input=["abcdefgh", "abc"])
    assert e.usage.prompt_tokens == 2 + 1


def test_probe_against_a_server_that_calls_tools(fake):
    from atlas.llm.probe import probe
    url, control = fake
    control.write_text(json.dumps({"tool_call": {"name": "word_length", "args": {"word": "atlas"}},
                                   "reasoning_tokens": 10, "usage_chars_per_token": 4}))
    lines = []
    assert probe(_service(url), out=lines.append) == 0
    out = "\n".join(lines)
    assert '1 OK' in out and 'word_length({"word": "atlas"})' in out and "it reasoned" in out
    assert '2 OK' in out and '"letters": 5' in out
    assert "3 OK: the server puts the reasoning back" in out


def test_check_says_which_structured_output_the_server_accepts(fake, capsys):
    url, control = fake
    from atlas.llm import Connection, check
    assert check(Connection(llm=_service(url), embeddings=_service(url))) == 0
    assert "structured output: json_schema" in capsys.readouterr().out
    control.write_text(json.dumps({"no_structured_output": "json_schema"}))
    service = _service(url)
    service.model = "fake-without-schema"
    assert check(Connection(llm=service, embeddings=_service(url))) == 0
    assert "structured output: json_object" in capsys.readouterr().out
    control.write_text(json.dumps({"no_structured_output": True}))
    service.model = "fake-without-any"
    assert check(Connection(llm=service, embeddings=_service(url))) == 0
    assert "structured output: none" in capsys.readouterr().out


def _until(condition, seconds=3.0):
    import time
    end = time.time() + seconds
    while not condition() and time.time() < end:
        time.sleep(0.05)
    return condition()


def test_a_streamed_answer_comes_in_chunks_with_the_usage_at_the_end(fake, fake_stats):
    url, control = fake
    control.write_text(json.dumps({"reasoning_tokens": 10, "chunk_chars": 3, "usage_chars_per_token": 4}))
    chunks = list(_client(url, "x").chat.completions.create(
        model="fake", messages=OK, stream=True, stream_options={"include_usage": True}))
    deltas = [c.choices[0].delta for c in chunks if c.choices]
    assert "".join(d.content or "" for d in deltas) == "ok"
    assert "".join((d.model_extra or {}).get("reasoning") or "" for d in deltas) == "The user wants a short answer. "
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "stop"
    assert chunks[-1].choices == [] and chunks[-1].usage.completion_tokens == 11
    assert fake_stats()["streams"] == 1 and fake_stats()["open"] == 0


def test_a_streamed_tool_call_comes_by_its_index(fake):
    url, control = fake
    control.write_text(json.dumps({"tool_call": {"name": "word_length", "args": {"word": "atlas"}}}))
    tool = {"type": "function", "function": {"name": "word_length", "parameters": {"type": "object"}}}
    chunks = list(_client(url, "x").chat.completions.create(model="fake", messages=OK, tools=[tool], stream=True))
    calls = [t for c in chunks if c.choices for t in c.choices[0].delta.tool_calls or []]
    assert calls[0].index == 0 and calls[0].id and calls[0].function.name == "word_length"
    assert "".join(t.function.arguments or "" for t in calls) == '{"word": "atlas"}' and len(calls) > 2
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


def test_a_server_without_streaming_rejects_it_or_ignores_it(fake):
    url, control = fake
    client = _client(url, "x")
    control.write_text(json.dumps({"no_stream": True}))
    with pytest.raises(Exception, match="stream is not supported"):
        client.chat.completions.create(model="fake", messages=OK, stream=True)
    control.write_text(json.dumps({"no_stream": "ignore"}))
    stream = client.chat.completions.create(model="fake", messages=OK, stream=True)
    assert stream.response.headers["content-type"] == "application/json"
    assert json.loads(stream.response.read())["choices"][0]["message"]["content"] == "ok"


def test_a_streamed_answer_that_hangs_holds_the_connection_until_the_client_closes_it(fake, fake_stats):
    import time
    url, control = fake
    control.write_text(json.dumps({"hang_after_chunks": 2, "slow_between_chunks": 0.05}))
    client = _client(url, "x", timeout=0.5)
    t0 = time.time()
    with pytest.raises(Exception) as hung:
        for _ in client.chat.completions.create(model="fake", messages=OK, stream=True):
            pass
    assert "timed out" in str(hung.value).lower() and time.time() - t0 < 2
    assert _until(lambda: fake_stats()["open"] == 0)            # the server sees the client close it


def test_the_own_tls_context_is_not_strict_on_python_3_13():
    import ssl
    import sys
    from atlas.llm import NetConfig, diagnose
    from atlas.llm.connection import ssl_context
    ctx = ssl_context(NetConfig(ca_cert="", insecure=False))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname       # the chain and the name, verified
    if sys.version_info >= (3, 13):
        assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT
    strict = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                             "Missing Authority Key Identifier (_ssl.c:1028)")
    assert 'ca_cert = "system"' in diagnose(strict) and "Python 3.12" in diagnose(strict)
    other = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                            "unable to get local issuer certificate")
    assert "corporate root CA" in diagnose(other)


def test_check_asks_the_server_for_the_context_of_the_model(fake, capsys):
    """OpenRouter publishes context_length in GET /models, vLLM max_model_len, and Ollama the
    num_ctx of the model in /api/show, outside /v1: check proposes it or compares it."""
    from dataclasses import replace
    from atlas.llm import Connection, check
    url, control = fake

    def out(service, **faults):
        control.write_text(json.dumps(faults))
        assert check(Connection(llm=service, embeddings=_service(url))) == 0
        return capsys.readouterr().out

    assert "context: 200000 tokens (context_length of GET /models, as OpenRouter publishes it): set [llm] " \
           "context_tokens = 200000" in out(_service(url), context_length=200000)
    mine = replace(_service(url), context_tokens=16384)
    assert "WARNING context: [llm] context_tokens = 16384, but the server says 32768 (max_model_len" in \
        out(mine, max_model_len=32768)
    assert "context: 16384 tokens, as [llm] context_tokens (num_ctx of the model in Ollama" in out(mine, num_ctx=16384)
    assert "context: the server does not publish it; [llm] context_tokens = 16384" in out(mine)
    assert "set [llm] context_tokens by hand" in out(_service(url))
