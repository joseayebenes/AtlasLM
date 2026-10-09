import os
import pytest
from atlas.config import ConfigError, load_config


def test_carga_defaults_y_expansion(toml, monkeypatch):
    monkeypatch.setenv("CLAVE_PRUEBA", "abc")
    cfg = load_config(toml('[llm]\napi_key = "${CLAVE_PRUEBA}"\nmodel = "${NO_EXISTE:-por-defecto}"'))
    assert cfg["llm"]["api_key"] == "abc" and cfg["llm"]["model"] == "por-defecto"
    assert cfg["pipeline"]["max_attempts"] == 3          # default
    assert cfg.workspace.name == "ws"


def test_clave_desconocida_falla_con_contexto(toml):
    with pytest.raises(ConfigError) as e:
        load_config(toml('[pipeline]\nworkerz = 3'))
    assert "[pipeline]" in str(e.value) and "workerz" in str(e.value)


def test_tipo_incorrecto(toml):
    with pytest.raises(ConfigError, match="entero"):
        load_config(toml('[pipeline]\nworkers = "muchos"'))


def test_fingerprint_por_seccion(toml):
    a = load_config(toml('[index]\nchunk_chars = 1000'))
    b = load_config(toml('[index]\nchunk_chars = 2000'))
    assert a.fingerprint("index") != b.fingerprint("index")
    assert a.fingerprint("sources.doors") == b.fingerprint("sources.doors")
