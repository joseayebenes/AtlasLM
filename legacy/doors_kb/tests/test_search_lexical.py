"""Indice FTS5 y busqueda lexical local (RF-070, RF-031, RNF-016, hito H4)."""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.models import RequirementRecord
from doors_kb.search.lexical import buscar_lexical, preparar_consulta
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


@pytest.fixture
def repo_con_datos(fuente, atributos):
    """Copia local ya sincronizada, que es como se llega al indice en la practica."""
    with SqliteRepository(":memory:") as repo:
        SyncService(fuente, repo, Settings(sync_attributes=atributos)).sync_module(MODULO)
        yield repo, fuente


# ---------------------------------------------------------------------------------------
# Mantenimiento incremental del indice
# ---------------------------------------------------------------------------------------


def test_la_sincronizacion_deja_el_indice_al_dia(repo_con_datos):
    """RF-070: no hace falta un paso aparte para indexar."""
    repo, _ = repo_con_datos

    assert repo.count_indexed(MODULO) == 3
    assert [r.absolute_number for r in buscar_lexical(repo, "timeout")] == [2]


def test_una_modificacion_actualiza_el_indice(repo_con_datos):
    """El texto viejo deja de encontrarse y el nuevo se encuentra."""
    repo, fuente = repo_con_datos
    fuente.modificar(2, text="La conexion se cierra tras 60 segundos.")
    SyncService(fuente, repo, Settings(sync_attributes=("Object Heading", "Object Text",
                                                        "Estado"))).sync_module(MODULO)

    assert buscar_lexical(repo, "30 segundos") == []
    assert [r.absolute_number for r in buscar_lexical(repo, "60 segundos")] == [2]


def test_un_requisito_borrado_sale_del_indice(repo_con_datos):
    """Devolver en las busquedas locales algo que ya no esta en DOORS seria enganoso."""
    repo, fuente = repo_con_datos
    fuente.borrar(2)
    SyncService(fuente, repo, Settings(sync_attributes=("Object Heading", "Object Text",
                                                        "Estado"))).sync_module(MODULO)

    assert buscar_lexical(repo, "timeout") == []
    assert repo.count_indexed(MODULO) == 2


def test_un_requisito_que_reaparece_vuelve_al_indice(repo_con_datos):
    """RF-056 tambien tiene que reflejarse en el indice, no solo en la tabla."""
    repo, fuente = repo_con_datos
    ajustes = Settings(sync_attributes=("Object Heading", "Object Text", "Estado"))
    fuente.borrar(2)
    SyncService(fuente, repo, ajustes).sync_module(MODULO)

    fuente.restaurar(2)
    SyncService(fuente, repo, ajustes).sync_module(MODULO)

    assert [r.absolute_number for r in buscar_lexical(repo, "timeout")] == [2]


def test_el_indice_se_puede_reconstruir_desde_la_copia_local(repo_con_datos):
    """RNF-016: los indices son derivados reconstruibles sin volver a consultar DOORS."""
    repo, _ = repo_con_datos
    repo.conn.execute("DELETE FROM requirements_fts")
    assert buscar_lexical(repo, "timeout") == []

    indexados = repo.rebuild_fts_index(MODULO)

    assert indexados == 3
    assert [r.absolute_number for r in buscar_lexical(repo, "timeout")] == [2]


def test_la_reconstruccion_no_reindexa_los_borrados(repo_con_datos):
    repo, fuente = repo_con_datos
    fuente.borrar(1)
    SyncService(fuente, repo, Settings(sync_attributes=("Object Heading", "Object Text",
                                                        "Estado"))).sync_module(MODULO)

    assert repo.rebuild_fts_index(MODULO) == 2


# ---------------------------------------------------------------------------------------
# Calidad de la busqueda
# ---------------------------------------------------------------------------------------


def test_los_acentos_no_impiden_encontrar_el_requisito(repo_con_datos):
    """El corpus es espanol y se escribe con y sin tildes: el indice no debe distinguirlas."""
    repo, _ = repo_con_datos

    assert [r.absolute_number for r in buscar_lexical(repo, "conexión")] == [2]
    assert [r.absolute_number for r in buscar_lexical(repo, "conexion")] == [2]


def test_se_buscan_tambien_los_atributos_del_proyecto(repo_con_datos):
    """Los atributos entran en el indice sin necesitar una columna FTS por atributo."""
    repo, _ = repo_con_datos

    encontrados = {r.absolute_number for r in buscar_lexical(repo, "Propuesto")}

    assert encontrados == {3}


def test_la_busqueda_se_puede_limitar_a_una_columna(repo_con_datos):
    """RF-031: buscar solo en el titulo evita el ruido del cuerpo del requisito."""
    repo, _ = repo_con_datos

    assert [r.absolute_number for r in buscar_lexical(repo, "Registro", columna="heading")] == [3]
    assert buscar_lexical(repo, "Propuesto", columna="heading") == []


def test_el_resultado_incluye_un_fragmento_citable(repo_con_datos):
    """El agente debe poder citar la parte relevante sin descargarse el requisito entero."""
    repo, _ = repo_con_datos

    resultado = buscar_lexical(repo, "segundos")[0]

    assert "[segundos]" in resultado.snippet
    assert resultado.identifier == "REQ-2"


def test_los_resultados_llegan_ordenados_por_relevancia(repo_con_datos):
    """bm25: mas negativo es mas relevante, y ese es el orden de salida."""
    repo, _ = repo_con_datos
    repo.upsert_requirement(
        RequirementRecord(
            module_path=MODULO, absolute_number=9, identifier="REQ-9",
            heading="Timeout", text="timeout timeout timeout de conexion",
        )
    )

    resultados = buscar_lexical(repo, "timeout")

    assert [r.absolute_number for r in resultados] == [9, 2]
    assert resultados[0].rank <= resultados[1].rank


def test_se_puede_limitar_el_ambito_a_un_modulo(repo_con_datos):
    """RF-077: el filtro estructurado mas habitual."""
    repo, _ = repo_con_datos
    repo.upsert_requirement(
        RequirementRecord(module_path="/Otro/Modulo", absolute_number=1, heading="Timeout",
                          text="otro modulo")
    )

    assert len(buscar_lexical(repo, "timeout")) == 2
    assert len(buscar_lexical(repo, "timeout", module_path=MODULO)) == 1


# ---------------------------------------------------------------------------------------
# Escapado de la consulta: el equivalente lexical de RF-041
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada",
    ["timeout AND", "fallo \"grave", "NEAR(", "a OR OR b", "*", "-", '""', "() AND NOT"],
)
def test_ninguna_cadena_del_usuario_rompe_la_consulta(repo_con_datos, entrada):
    """Una consulta escrita a mano no puede provocar un error de sintaxis de FTS5."""
    repo, _ = repo_con_datos

    buscar_lexical(repo, entrada)  # no debe lanzar


def test_en_modo_literal_los_operadores_son_palabras(repo_con_datos):
    """'AND' se busca como texto, no como operador: el significado no cambia solo."""
    repo, _ = repo_con_datos

    assert preparar_consulta("timeout AND conexion") == '"timeout" "AND" "conexion"'
    assert buscar_lexical(repo, "timeout AND conexion") == []


def test_el_modo_avanzado_es_una_eleccion_explicita(repo_con_datos):
    """Quien conoce la sintaxis de FTS5 puede usarla, pero nunca por defecto."""
    repo, _ = repo_con_datos

    encontrados = {r.absolute_number for r in
                   buscar_lexical(repo, "timeout OR registro", modo="advanced")}

    assert encontrados == {2, 3}


def test_una_comilla_dentro_de_un_termino_se_escapa(repo_con_datos):
    assert preparar_consulta('dice "hola"') == '"dice" """hola"""'
    buscar_lexical(repo_con_datos[0], 'dice "hola"')  # no debe lanzar


def test_un_modo_o_una_columna_desconocidos_fallan_pronto(repo_con_datos):
    repo, _ = repo_con_datos

    with pytest.raises(ValueError, match="Modo de consulta desconocido"):
        buscar_lexical(repo, "x", modo="magico")
    with pytest.raises(ValueError, match="Columna de busqueda desconocida"):
        buscar_lexical(repo, "x", columna="inventada")
