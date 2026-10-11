"""Busqueda vectorial e hibrida (RF-075 a RF-077, hito H6).

Se usa el proveedor de embeddings falso: es determinista y no necesita red. No es un modelo
semantico real, asi que lo que se comprueba aqui es el **mecanismo** -filtros, fusion de
rankings, procedencia, pesos-, no la calidad semantica, que solo se puede valorar con el
proveedor de produccion.
"""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.embeddings import EmbeddingService, FakeEmbeddingProvider
from doors_kb.models import AttributeDefinition
from doors_kb.search import buscar_hibrida, buscar_vectorial
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"
ATRIBUTOS = ("Object Heading", "Object Text", "Estado")


@pytest.fixture
def indexado(fuente):
    """Copia local sincronizada, indexada por FTS y con embeddings generados."""
    fuente.definir_atributo(AttributeDefinition("Criticidad", "String"))
    fuente.modificar(1, attributes={"Estado": "Aprobado", "Criticidad": "alta"})
    fuente.modificar(2, attributes={"Estado": "Aprobado", "Criticidad": "baja"})
    fuente.modificar(3, attributes={"Estado": "Propuesto", "Criticidad": "alta"})

    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=(*ATRIBUTOS, "Criticidad"))
        SyncService(fuente, repo, ajustes).sync_module(MODULO)
        proveedor = FakeEmbeddingProvider()
        EmbeddingService(proveedor, repo, ajustes).update_index(MODULO)
        yield repo, proveedor, fuente


# ---------------------------------------------------------------------------------------
# Busqueda vectorial (RF-075, RF-077)
# ---------------------------------------------------------------------------------------


def test_la_busqueda_vectorial_devuelve_resultados_ordenados(indexado):
    repo, proveedor, _ = indexado

    resultados = buscar_vectorial(repo, "la conexion se cierra", proveedor, module_path=MODULO)

    assert resultados
    assert resultados[0].absolute_number == 2
    assert resultados == sorted(resultados, key=lambda r: -r.score)


def test_se_respeta_el_limite_de_resultados(indexado):
    repo, proveedor, _ = indexado

    assert len(buscar_vectorial(repo, "conexion", proveedor, limit=2)) == 2


def test_los_filtros_estructurados_se_aplican_antes_de_puntuar(indexado):
    """RF-077: filtrar despues del top-k devolveria menos resultados de los pedidos."""
    repo, proveedor, _ = indexado

    solo_altas = buscar_vectorial(
        repo, "conexion", proveedor, filtros_atributos={"Criticidad": "alta"}, limit=1
    )

    # Aunque el mas parecido a "conexion" es el REQ-2 (criticidad baja), con el filtro
    # activo se devuelve un resultado valido, no una lista vacia.
    assert len(solo_altas) == 1
    assert solo_altas[0].absolute_number in (1, 3)


def test_el_filtro_por_modulo_acota_el_ambito(indexado):
    repo, proveedor, _ = indexado

    assert buscar_vectorial(repo, "conexion", proveedor, module_path="/Otro/Modulo") == []


def test_una_consulta_vacia_no_llama_al_proveedor(indexado):
    """Evita gastar una peticion de embeddings en una busqueda sin contenido."""
    repo, proveedor, _ = indexado

    assert buscar_vectorial(repo, "   ", proveedor) == []


def test_un_indice_generado_con_otro_modelo_da_un_error_claro(indexado):
    """Cambiar de modelo sin reindexar produciria comparaciones sin sentido."""
    from doors_kb.errors import DoorsKbError

    repo, _, _ = indexado
    otro = FakeEmbeddingProvider(model="fake-embeddings-64", dim=128)  # mismo nombre, otra dim

    with pytest.raises(DoorsKbError, match="reconstruyelo con doors-embed"):
        buscar_vectorial(repo, "conexion", otro)


# ---------------------------------------------------------------------------------------
# Busqueda hibrida (RF-076)
# ---------------------------------------------------------------------------------------


def test_la_hibrida_reune_lo_que_encuentra_cada_via(indexado):
    repo, proveedor, _ = indexado

    resultados = buscar_hibrida(repo, "timeout", proveedor, module_path=MODULO)

    assert resultados
    encontrado = next(r for r in resultados if r.absolute_number == 2)
    assert "lexical" in encontrado.procedencia


def test_un_resultado_encontrado_por_las_dos_vias_sube_en_el_ranking(indexado):
    """Es el efecto que justifica la fusion: el acuerdo entre rankings refuerza."""
    repo, proveedor, _ = indexado

    resultados = buscar_hibrida(repo, "timeout tcp conexion", proveedor, module_path=MODULO)

    primero = resultados[0]
    assert primero.absolute_number == 2
    assert set(primero.procedencia) == {"lexical", "semantic"}
    assert primero.posicion_lexical == 1


def test_cada_resultado_explica_por_que_aparece(indexado):
    """Sin la procedencia, una busqueda hibrida es una caja negra irrevisable."""
    repo, proveedor, _ = indexado

    resultado = buscar_hibrida(repo, "registro", proveedor, module_path=MODULO)[0].to_dict()

    assert resultado["matched_by"]
    assert "lexical_rank" in resultado and "semantic_rank" in resultado


def test_los_pesos_permiten_quedarse_con_una_sola_via(indexado):
    """peso_vectorial=0 da la linea base de FTS puro con la que comparar (ADR-007)."""
    repo, proveedor, _ = indexado

    solo_lexical = buscar_hibrida(repo, "timeout", proveedor, peso_vectorial=0)
    solo_semantica = buscar_hibrida(repo, "timeout", proveedor, peso_lexical=0)

    assert all(r.procedencia == ["lexical"] for r in solo_lexical)
    assert all(r.procedencia == ["semantic"] for r in solo_semantica)


def test_la_hibrida_encuentra_lo_que_fts_no_encuentra(indexado):
    """El caso que justifica el hito: la consulta no comparte palabras exactas.

    Con el proveedor falso la similitud es aproximada, no semantica real, pero el mecanismo
    es el mismo: la via vectorial aporta candidatos que la lexical no devuelve.
    """
    from doors_kb.search import buscar_lexical

    repo, proveedor, _ = indexado
    consulta = "cuanto tarda en cerrarse"

    assert buscar_lexical(repo, consulta) == []
    assert buscar_hibrida(repo, consulta, proveedor, module_path=MODULO)


def test_el_orden_es_estable_entre_ejecuciones(indexado):
    """Sin un desempate explicito, dos ejecuciones identicas podrian diferir."""
    repo, proveedor, _ = indexado

    primera = [r.absolute_number for r in buscar_hibrida(repo, "conexion", proveedor)]
    segunda = [r.absolute_number for r in buscar_hibrida(repo, "conexion", proveedor)]

    assert primera == segunda


def test_se_respeta_el_limite_final(indexado):
    repo, proveedor, _ = indexado

    assert len(buscar_hibrida(repo, "conexion", proveedor, limit=2)) == 2
