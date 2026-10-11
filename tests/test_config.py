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


@pytest.mark.parametrize("toml_text, where", [
    ('[llm]\nbase_url = "https://gateway.company/v1"\napi_key = "sk-0123456789"', "[llm] api_key"),
    ('[llm]\nbase_url = "https://gateway.company/v1"\n[embeddings]\napi_key = "sk-0123456789"',
     "[embeddings] api_key"),
    ('[network]\nproxy = "http://jane:Secret1@proxy.company:8080"', "[network] proxy"),
    ('[llm]\nnetwork = { proxy = "http://jane:Secret1@proxy:8080" }', "[llm.network] proxy"),
    ('[sources.doors.database]\ndriver = "odbc"\ndsn = "DSN=DOORS;UID=jane;PWD=Secret1"',
     "[sources.doors.database] dsn"),
])
def test_secrets_written_in_the_file_are_refused(tmp_path, toml_text, where):
    (tmp_path / "atlas.toml").write_text(toml_text)
    with pytest.raises(ConfigError, match="environment") as e:
        load_config(tmp_path / "atlas.toml")
    assert where in str(e.value) and "Secret1" not in str(e.value) and "sk-0123456789" not in str(e.value)


def test_secrets_from_the_environment_and_keys_of_local_servers_are_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("GW_KEY", "sk-0123456789")
    monkeypatch.setenv("PROXY_PASS", "Secret1")
    (tmp_path / "atlas.toml").write_text(
        '[llm]\nbase_url = "https://gateway.company/v1"\napi_key = "${GW_KEY}"\n'
        '[embeddings]\nbase_url = "http://localhost:11434/v1"\napi_key = "ollama"\n'
        '[network]\nproxy = "http://jane:${PROXY_PASS}@proxy.company:8080"\n'
        '[sources.doors]\nadapter = "database"\n'
        '[sources.doors.database]\ndriver = "odbc"\ndsn = "DSN=DOORS;UID=jane;PWD=${PROXY_PASS}"\n')
    cfg = load_config(tmp_path / "atlas.toml")
    assert cfg["llm"]["api_key"] == "sk-0123456789" and "Secret1" in cfg["network"]["proxy"]


def test_fingerprints_ignore_secrets(tmp_path, monkeypatch):
    def fingerprint(password: str, server: str = "DOORS") -> str:
        monkeypatch.setenv("DB_PASS", password)
        (tmp_path / "atlas.toml").write_text(
            f'[sources.doors]\nadapter = "database"\n[sources.doors.database]\ndriver = "odbc"\n'
            f'dsn = "DSN={server};UID=jane;PWD=${{DB_PASS}}"\n')
        return load_config(tmp_path / "atlas.toml").fingerprint("sources.doors")
    # a new password reprocesses nothing; another server does
    assert fingerprint("old") == fingerprint("new") != fingerprint("new", server="DOORS2")


def test_check_masks_the_credentials_of_urls():
    from atlas.llm import NetConfig, ServiceConfig, describe
    text = describe(ServiceConfig(base_url="https://jane:Secret1@gateway/v1", model="m", api_key="sk-0123456789",
                                  net=NetConfig(proxy="http://jane:Secret1@proxy:8080", no_proxy="")))
    assert "Secret1" not in text and "sk-0123456789" not in text and "gateway/v1" in text
