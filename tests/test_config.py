import os
import pytest
from atlas.config import ConfigError, load_config


def test_defaults_and_expansion(toml, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "abc")
    cfg = load_config(toml('[llm]\napi_key = "${TEST_KEY}"\nmodel = "${DOES_NOT_EXIST:-fallback}"'))
    assert cfg["llm"]["api_key"] == "abc" and cfg["llm"]["model"] == "fallback"
    assert cfg["pipeline"]["max_attempts"] == 3          # default
    assert cfg.workspace.name == "ws"


def test_the_variables_this_process_lacks(toml, monkeypatch):
    """An MCP client may start atlas without the variables of the terminal: the
    configuration says which ones expanded to nothing."""
    from atlas.config import unset_hint
    for name in ("TEST_USER", "TEST_PASS", "TEST_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_KEY", "abc")
    monkeypatch.setenv("TEST_EMPTY", "")
    cfg = load_config(toml('[llm]\napi_key = "${TEST_KEY}"\nmodel = "${TEST_MODEL:-gemma}"\n'
                           'base_url = "https://${TEST_EMPTY}gateway/v1"\n'
                           '[network]\nproxy = "http://${TEST_USER}:${TEST_PASS:-}@proxy:8080"\n'
                           'no_proxy = "${TEST_USER}"\n'))
    # a default value is not missing; an empty variable is; each name once
    assert cfg.unset == ["TEST_EMPTY", "TEST_USER", "TEST_PASS"]
    hint = unset_hint(cfg)
    assert hint.startswith("TEST_EMPTY, TEST_USER, TEST_PASS of atlas.toml not set") and "MCP client" in hint
    monkeypatch.setenv("TEST_USER", "jane")
    monkeypatch.setenv("TEST_PASS", "secret")
    monkeypatch.setenv("TEST_EMPTY", "api.")
    cfg = load_config(cfg.path)
    assert cfg.unset == [] and unset_hint(cfg) == ""


def test_the_error_of_a_tool_names_the_variables_it_lacks(toml, monkeypatch):
    import httpx
    import openai
    from atlas.llm import model_failed
    monkeypatch.delenv("TEST_PROXY", raising=False)
    cfg = load_config(toml('[network]\nproxy = "${TEST_PROXY:-}"\n'))
    e = openai.APIConnectionError(request=httpx.Request("POST", "https://gateway/v1/chat/completions"))
    error = model_failed(e, cfg)["error"]
    assert error.startswith("the model server failed: ") and "TEST_PROXY of atlas.toml not set" in error
    monkeypatch.setenv("TEST_PROXY", "http://proxy:8080")
    assert "not set" not in model_failed(e, load_config(cfg.path))["error"]
    assert "not set" not in model_failed(e)["error"]


def test_unknown_key_fails_with_context(toml):
    with pytest.raises(ConfigError) as e:
        load_config(toml('[pipeline]\nworkerz = 3'))
    assert "[pipeline]" in str(e.value) and "workerz" in str(e.value)


def test_search_settings(toml):
    cfg = load_config(toml('[search]\nresults = 12'))
    assert cfg["search"]["results"] == 12 and cfg["search"]["scope"] == "auto"          # the rest by default
    with pytest.raises(ConfigError, match="scope must be one of auto, passages, whole"):
        load_config(toml('[search]\nscope = "all"'))
    with pytest.raises(ConfigError, match="'results' must be at least 1"):
        load_config(toml('[search]\nresults = 0'))
    # what search and ask take changes nothing of the index
    assert load_config(toml('[search]\nresults = 3')).fingerprint("index") == cfg.fingerprint("index")


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


def test_the_context_of_the_model_sizes_what_search_reads(toml):
    from atlas.config import search_budget
    cfg = load_config(toml('[llm]\ncontext_tokens = 200000\nmax_tokens = 8192\n'))
    assert cfg["search"]["scope_chars"] == search_budget(200000, 8192) == 460339
    assert cfg["search"]["context_chars"] == 14000                       # passages: never more than before
    small = load_config(toml('[llm]\ncontext_tokens = 4096\nmax_tokens = 1024\n'))
    assert small["search"]["scope_chars"] == small["search"]["context_chars"] == 7372
    given = load_config(toml('[llm]\ncontext_tokens = 4096\n[search]\nscope_chars = 50000\n'))
    assert given["search"]["scope_chars"] == 50000 and given["search"]["context_chars"] == 9830
    assert load_config(toml())["search"]["scope_chars"] == 100000          # unknown context: as before
    with pytest.raises(ConfigError, match="context_tokens"):
        load_config(toml('[llm]\ncontext_tokens = 1000\nmax_tokens = 8192\n'))
