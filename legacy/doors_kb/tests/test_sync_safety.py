"""Reglas de seguridad de la sincronizacion (RF-060, RF-061, CA-005, CA-007).

Son las pruebas que impiden el peor fallo posible del sistema: que una sincronizacion
interrumpida marque como eliminados requisitos que si existen en DOORS.
"""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.errors import AttributeValidationError, SyncError
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


@pytest.fixture
def entorno(fuente, atributos):
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=atributos, sync_page_size=2)
        yield SyncService(fuente, repo, ajustes), fuente, repo


def test_un_atributo_inexistente_se_rechaza_antes_de_extraer_nada(entorno):
    """CA-007 y RF-060: se valida una sola vez, antes de escribir en la copia local."""
    sync, origen, repo = entorno

    with pytest.raises(AttributeValidationError, match="Atributos inexistentes"):
        sync.sync_module(MODULO, attributes=("Object Text", "Atributo Que No Existe"))

    assert repo.count_requirements(MODULO, include_deleted=True) == 0
    assert origen.pages_served == 0  # ni siquiera se llego a pedir la primera pagina
    assert repo.last_sync_run(MODULO)["status"] == "failed"


def test_un_corte_a_mitad_no_marca_nada_como_eliminado(entorno):
    """CA-005: la regla mas importante del sistema.

    La primera sincronizacion copia los tres requisitos. La segunda falla despues de la
    primera pagina, habiendo visto solo dos. Si el marcado de ausentes dependiera de "no
    haber visto" en lugar de "haber llegado al final", el tercer requisito quedaria
    borrado logicamente sin haber desaparecido de DOORS.
    """
    sync, origen, repo = entorno
    sync.sync_module(MODULO)
    assert repo.count_requirements(MODULO) == 3

    origen.pages_served = 0
    origen.fail_after_pages = 1  # falla al pedir la segunda pagina

    with pytest.raises(SyncError, match="No se ha marcado ningun requisito como eliminado"):
        sync.sync_module(MODULO)

    assert repo.count_requirements(MODULO) == 3
    assert repo.get_requirement(MODULO, 3) is not None


def test_un_corte_a_mitad_queda_registrado_como_fallido(entorno):
    """RF-062: el historial distingue una sincronizacion fallida de una que no ocurrio."""
    sync, origen, repo = entorno
    origen.fail_after_pages = 1

    with pytest.raises(SyncError):
        sync.sync_module(MODULO)

    registro = repo.last_sync_run(MODULO)
    assert registro["status"] == "failed"
    assert registro["completed_module"] == 0
    assert registro["deleted"] == 0
    assert "fallo simulado" in registro["error"]


def test_las_paginas_confirmadas_antes_del_corte_se_conservan(entorno):
    """RNF-012: transaccion por pagina, no por sincronizacion entera.

    Perder el trabajo ya hecho obligaria a repetir horas de extraccion en modulos grandes.
    """
    sync, origen, repo = entorno
    origen.fail_after_pages = 1

    with pytest.raises(SyncError):
        sync.sync_module(MODULO)

    assert repo.count_requirements(MODULO) == 2  # la primera pagina (2 objetos) se conservo


def test_una_sincronizacion_incompleta_no_marca_el_modulo_como_completo(entorno):
    """La frescura debe reflejar que la copia local no cubre el modulo entero (R-007)."""
    sync, origen, repo = entorno
    origen.fail_after_pages = 1

    with pytest.raises(SyncError):
        sync.sync_module(MODULO)

    assert repo.module_freshness(MODULO)["last_full_sync_at"] is None


def test_el_error_de_sincronizacion_lleva_las_estadisticas_parciales(entorno):
    """Quien captura el error debe poder decir hasta donde se llego."""
    sync, origen, _ = entorno
    origen.fail_after_pages = 1

    with pytest.raises(SyncError) as excepcion:
        sync.sync_module(MODULO)

    stats = excepcion.value.stats
    assert stats.seen == 2
    assert stats.completed_module is False
    assert stats.deleted == 0
