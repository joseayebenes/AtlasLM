import os
import pytest
from atlas.config import ConfigError, load_config


def test_defaults_and_expansion(toml, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "abc")
    cfg = load_config(toml('[llm]\napi_key = "${TEST_KEY}"\nmodel = "${DOES_NOT_EXIST:-fallback}"'))
    assert cfg["llm"]["api_key"] == "abc" and cfg["llm"]["model"] == "fallback"
    assert cfg["pipeline"]["max_attempts"] == 3          # default
    assert cfg.workspace.name == "ws"


def test_unknown_key_fails_with_context(toml):
    with pytest.raises(ConfigError) as e:
        load_config(toml('[pipeline]\nworkerz = 3'))
    assert "[pipeline]" in str(e.value) and "workerz" in str(e.value)


def test_wrong_type(toml):
    with pytest.raises(ConfigError, match="integer"):
        load_config(toml('[pipeline]\nworkers = "many"'))


def test_fingerprint_per_section(toml):
    a = load_config(toml('[index]\nchunk_chars = 1000'))
    b = load_config(toml('[index]\nchunk_chars = 2000'))
    assert a.fingerprint("index") != b.fingerprint("index")
    assert a.fingerprint("sources.doors") == b.fingerprint("sources.doors")
