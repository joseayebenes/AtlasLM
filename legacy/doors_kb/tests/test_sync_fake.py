"""Ciclo de vida completo de un requisito, sin abrir DOORS (RNF-013).

Cubre los criterios de aceptacion CA-002, CA-003 y CA-004 de extremo a extremo:
alta, modificacion, desaparicion y reaparicion.
"""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


@pytest.fixture
def servicio(fuente, atributos):
    """Servicio de sincronizacion sobre una copia local en memoria."""
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=atributos, sync_page_size=2)
        yield SyncService(fuente, repo, ajustes), fuente, repo


def test_primera_sincronizacion_inserta_todo(servicio):
    sync, _, repo = servicio

    stats = sync.sync_module(MODULO)

    assert (stats.inserted, stats.updated, stats.unchanged) == (3, 0, 0)
    assert stats.completed_module is True
    assert repo.count_requirements(MODULO) == 3


def test_dos_sincronizaciones_seguidas_sin_cambios(servicio):
    """CA-002: 0 inserted, 0 updated y N unchanged."""
    sync, _, _ = servicio
    sync.sync_module(MODULO)

    stats = sync.sync_module(MODULO)

    assert (stats.inserted, stats.updated, stats.unchanged, stats.deleted) == (0, 0, 3, 0)


def test_una_modificacion_produce_un_solo_updated(servicio):
    """CA-003: solo el objeto tocado se clasifica como modificado."""
    sync, origen, _ = servicio
    sync.sync_module(MODULO)
    origen.modificar(2, text="La conexion se cierra tras 60 segundos.")

    stats = sync.sync_module(MODULO)

    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 2)


def test_un_alta_y_una_baja_en_la_misma_pasada(servicio):
    """CA-004: el alta se inserta y la baja se marca tras completar el recorrido."""
    sync, origen, repo = servicio
    sync.sync_module(MODULO)

    origen.anadir(4, heading="Cifrado", text="El canal usa TLS 1.3.")
    origen.borrar(1)
    stats = sync.sync_module(MODULO)

    assert (stats.inserted, stats.deleted) == (1, 1)
    assert repo.get_requirement(MODULO, 1) is None
    assert repo.get_requirement(MODULO, 4)["heading"] == "Cifrado"


def test_un_requisito_que_reaparece_vuelve_a_estar_activo(servicio):
    """RF-056: reaparece con el mismo contenido y debe reactivarse igualmente."""
    sync, origen, repo = servicio
    sync.sync_module(MODULO)
    origen.borrar(1)
    sync.sync_module(MODULO)
    assert repo.get_requirement(MODULO, 1) is None

    origen.restaurar(1)
    stats = sync.sync_module(MODULO)

    assert stats.updated == 1
    assert repo.get_requirement(MODULO, 1) is not None
    assert repo.count_requirements(MODULO) == 3


def test_la_frescura_se_marca_como_completa_tras_una_pasada_entera(servicio):
    """Riesgo R-007: el agente necesita saber si la copia refleja el modulo entero."""
    sync, _, repo = servicio

    sync.sync_module(MODULO)

    frescura = repo.module_freshness(MODULO)
    assert frescura["last_full_sync_at"] is not None
    assert frescura["active_requirements"] == 3


def test_el_atributo_de_fecha_configurado_no_entra_en_el_hash(fuente, atributos):
    """RF-063: la fecha de modificacion es metadato, no contenido.

    Si entrara en el hash, DOORS tocando la fecha bastaria para marcar el requisito como
    modificado y regenerar su embedding sin motivo (RF-074).
    """
    from doors_kb.models import AttributeDefinition

    fuente.definir_atributo(AttributeDefinition("Last Modified On", "Date"))
    fuente.modificar(1, attributes={"Estado": "Aprobado", "Last Modified On": "2026-01-01"})

    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(
            sync_attributes=atributos, source_last_modified_attribute="Last Modified On"
        )
        sync = SyncService(fuente, repo, ajustes)
        sync.sync_module(MODULO)

        fuente.modificar(1, attributes={"Estado": "Aprobado", "Last Modified On": "2026-02-02"})
        stats = sync.sync_module(MODULO)

        # El requisito no cuenta como modificado, pero la fecha almacenada se refresca.
        assert stats.unchanged == 3
        assert repo.get_requirement(MODULO, 1)["source_last_modified"] == "2026-02-02"
