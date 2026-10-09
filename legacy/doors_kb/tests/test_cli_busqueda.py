"""Comandos doors-embed y doors-search de extremo a extremo, sin DOORS y sin red."""

import json

import pytest

from doors_kb.cli.embed import main as embed_main
from doors_kb.cli.search import main as search_main
from doors_kb.cli.sync_doors import main as sync_main

MODULO = "/Demo/Reqs"


@pytest.fixture
def base(tmp_path, capsys):
    """Copia local sincronizada y con embeddings generados por el proveedor falso."""
    ruta = tmp_path / "kb.sqlite3"
    # Se sincroniza Estado porque el perfil de embeddings solo puede usar atributos que la
    # copia local contenga: pedir uno sin sincronizar no aporta nada al texto.
    sync_main(["--source", "fake", "--module", MODULO, "--db", str(ruta),
               "--attributes", "Object Heading,Object Text,Estado"])
    embed_main(["--module", MODULO, "--db", str(ruta), "--provider", "fake"])
    capsys.readouterr()
    return ruta


def test_la_primera_generacion_embebe_todo_el_modulo(tmp_path, capsys):
    ruta = tmp_path / "kb.sqlite3"
    sync_main(["--source", "fake", "--module", MODULO, "--db", str(ruta)])
    capsys.readouterr()

    codigo = embed_main(["--module", MODULO, "--db", str(ruta), "--provider", "fake"])

    assert codigo == 0
    stats = json.loads(capsys.readouterr().out)
    assert (stats["candidates"], stats["generated"], stats["skipped"]) == (25, 25, 0)


def test_la_segunda_generacion_no_pide_ni_un_embedding(base, capsys):
    """RF-074 comprobado desde el comando: con un proveedor de pago, esto es la factura."""
    codigo = embed_main(["--module", MODULO, "--db", str(base), "--provider", "fake"])

    assert codigo == 0
    stats = json.loads(capsys.readouterr().out)
    assert (stats["generated"], stats["skipped"]) == (0, 25)


def test_cambiar_el_perfil_de_atributos_regenera(base, capsys):
    """El requisito no cambia, pero el texto embebido si (RF-072)."""
    embed_main(
        ["--module", MODULO, "--db", str(base), "--provider", "fake", "--attributes", "Estado"]
    )

    stats = json.loads(capsys.readouterr().out)
    assert stats["generated"] == 25


def test_la_busqueda_lexical_no_necesita_embeddings(base, capsys):
    codigo = search_main(["--module", MODULO, "--db", str(base), "--mode", "lexical",
                          "condicion numero 7"])

    assert codigo == 0
    salida = json.loads(capsys.readouterr().out)
    assert [r["absolute_number"] for r in salida["results"]][:1] == [7]
    assert "[condicion]" in salida["results"][0]["snippet"]


def test_la_busqueda_hibrida_explica_la_procedencia(base, capsys):
    codigo = search_main(["--module", MODULO, "--db", str(base), "--mode", "hybrid",
                          "--provider", "fake", "--limit", "3", "condicion numero 7"])

    assert codigo == 0
    salida = json.loads(capsys.readouterr().out)
    assert all(r["matched_by"] for r in salida["results"])


def test_la_salida_siempre_lleva_la_frescura(base, capsys):
    """Quien lee esto en una terminal necesita saber a que fecha corresponde (R-007)."""
    search_main(["--module", MODULO, "--db", str(base), "--mode", "lexical", "condicion"])

    salida = json.loads(capsys.readouterr().out)
    assert salida["freshness"]["last_full_sync_at"]


def test_un_filtro_mal_escrito_se_rechaza_con_un_mensaje_util(base, capsys):
    codigo = search_main(["--module", MODULO, "--db", str(base), "--mode", "semantic",
                          "--provider", "fake", "--filter", "Criticidad-alta", "condicion"])

    assert codigo == 1
    assert "ATRIBUTO=VALOR" in capsys.readouterr().err


def test_sin_proveedor_configurado_la_busqueda_semantica_dice_que_falta(base, capsys, monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_BASE_URL", raising=False)

    codigo = search_main(["--module", MODULO, "--db", str(base), "--mode", "semantic", "condicion"])

    assert codigo == 1
    assert "EMBEDDINGS_BASE_URL" in capsys.readouterr().err
