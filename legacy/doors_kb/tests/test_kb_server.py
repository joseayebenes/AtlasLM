"""Servidor MCP sobre la copia local (RF-078).

Lo mas importante que se fija aqui es que **ninguna respuesta sale sin la frescura de la
copia local** (riesgo R-007): un indice desactualizado que responde sin avisar es peor que
uno que no responde.
"""

import asyncio
import json

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.embeddings import EmbeddingService, FakeEmbeddingProvider
from doors_kb.servers.kb_server import crear_servidor
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


@pytest.fixture
def servidor(fuente, atributos):
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(module_path=MODULO, sync_attributes=atributos)
        SyncService(fuente, repo, ajustes).sync_module(MODULO)
        proveedor = FakeEmbeddingProvider()
        EmbeddingService(proveedor, repo, ajustes).update_index(MODULO)
        yield crear_servidor(ajustes, repo, proveedor), repo, fuente


def llamar(servidor, herramienta, **argumentos) -> dict:
    resultado = asyncio.run(servidor.call_tool(herramienta, argumentos))
    return json.loads(resultado.content[0].text)


# ---------------------------------------------------------------------------------------
# Catalogo y seguridad
# ---------------------------------------------------------------------------------------


def test_el_catalogo_cubre_las_tres_busquedas_y_la_frescura(servidor):
    """RF-078: estadisticas, textual, semantica, hibrida y consulta de requisito local."""
    srv, _, _ = servidor

    nombres = {t.name for t in asyncio.run(srv.list_tools())}

    assert nombres == {
        "kb_status",
        "kb_search",
        "kb_semantic_search",
        "kb_hybrid_search",
        "kb_get_requirement",
        "kb_list_requirements",
    }


def test_todas_las_herramientas_son_de_solo_lectura(servidor):
    """La copia local tampoco se modifica desde el agente (RNF-017)."""
    srv, _, _ = servidor

    for herramienta in asyncio.run(srv.list_tools()):
        assert herramienta.annotations.read_only_hint is True


# ---------------------------------------------------------------------------------------
# Frescura en todas las respuestas (riesgo R-007)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("herramienta", "argumentos"),
    [
        ("kb_status", {}),
        ("kb_search", {"query": "timeout"}),
        ("kb_semantic_search", {"query": "conexion"}),
        ("kb_hybrid_search", {"query": "conexion"}),
        ("kb_get_requirement", {"absolute_number": 2}),
        ("kb_list_requirements", {}),
    ],
)
def test_ninguna_respuesta_sale_sin_frescura(servidor, herramienta, argumentos):
    """El agente debe poder saber siempre a que fecha corresponde lo que le contestan."""
    srv, _, _ = servidor

    respuesta = llamar(srv, herramienta, **argumentos)

    assert respuesta["freshness"]["synchronized"] is True
    assert respuesta["freshness"]["last_full_sync_at"]


def test_un_modulo_nunca_sincronizado_avisa_en_lugar_de_callar(servidor):
    """Cero resultados por falta de datos no puede parecer cero resultados por ausencia."""
    srv, _, _ = servidor

    respuesta = llamar(srv, "kb_search", query="timeout", module_path="/Modulo/Sin/Sincronizar")

    assert respuesta["records"] == []
    assert respuesta["freshness"]["synchronized"] is False
    assert "nunca" in respuesta["freshness"]["warning"]


def test_una_sincronizacion_incompleta_se_advierte(servidor):
    """Si el recorrido nunca termino, la copia puede estar a medias."""
    srv, repo, _ = servidor
    repo.conn.execute("UPDATE modules SET last_full_sync_at = NULL WHERE module_path = ?",
                      (MODULO,))

    respuesta = llamar(srv, "kb_status")

    assert "puede faltar informacion" in respuesta["freshness"]["warning"]


# ---------------------------------------------------------------------------------------
# Comportamiento de las busquedas
# ---------------------------------------------------------------------------------------


def test_la_busqueda_textual_devuelve_fragmentos_citables(servidor):
    srv, _, _ = servidor

    respuesta = llamar(srv, "kb_search", query="timeout")

    assert [r["absolute_number"] for r in respuesta["records"]] == [2]
    assert "[Timeout]" in respuesta["records"][0]["snippet"]


def test_la_busqueda_textual_no_necesita_embeddings(fuente, atributos):
    """Exigir el proveedor para arrancar dejaria inutil el servidor por una funcion opcional."""
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(module_path=MODULO, sync_attributes=atributos)
        SyncService(fuente, repo, ajustes).sync_module(MODULO)
        srv = crear_servidor(ajustes, repo)  # sin proveedor de embeddings

        respuesta = llamar(srv, "kb_search", query="timeout")

        assert respuesta["records"]


def test_la_busqueda_semantica_sin_proveedor_configurado_explica_que_falta(fuente, atributos):
    srv_repo = SqliteRepository(":memory:")
    with srv_repo as repo:
        ajustes = Settings(module_path=MODULO, sync_attributes=atributos)
        SyncService(fuente, repo, ajustes).sync_module(MODULO)
        srv = crear_servidor(ajustes, repo)

        respuesta = llamar(srv, "kb_semantic_search", query="conexion")

        assert respuesta["error"] == "EmbeddingError"
        assert "EMBEDDINGS_BASE_URL" in respuesta["message"]


def test_la_hibrida_explica_la_procedencia_de_cada_resultado(servidor):
    srv, _, _ = servidor

    respuesta = llamar(srv, "kb_hybrid_search", query="timeout tcp conexion")

    primero = respuesta["records"][0]
    assert primero["absolute_number"] == 2
    assert set(primero["matched_by"]) == {"lexical", "semantic"}


def test_una_consulta_con_sintaxis_de_fts_no_rompe_la_tool(servidor):
    """El escapado literal por defecto tambien protege al servidor local."""
    srv, _, _ = servidor

    respuesta = llamar(srv, "kb_search", query="timeout AND")

    assert "error" not in respuesta


def test_un_requisito_ausente_no_es_un_error(servidor):
    srv, _, _ = servidor

    respuesta = llamar(srv, "kb_get_requirement", absolute_number=999)

    assert respuesta["record"] is None
    assert "copia local" in respuesta["message"]


def test_el_recorrido_local_avanza_con_cursor(servidor):
    """Mismo modelo de paginacion que el servidor directo, para no tener dos idiomas."""
    srv, _, _ = servidor

    primera = llamar(srv, "kb_list_requirements", limit=2)

    assert [r["absolute_number"] for r in primera["records"]] == [1, 2]
    assert primera["next_cursor"] == 2

    segunda = llamar(srv, "kb_list_requirements", limit=2, cursor=2)

    assert [r["absolute_number"] for r in segunda["records"]] == [3]
    assert segunda["next_cursor"] is None
