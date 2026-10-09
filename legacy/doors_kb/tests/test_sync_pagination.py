"""Coste de la paginacion (RNF-015, CA-006, ADR-006).

La primera implementacion del proyecto paginaba por offset: cada pagina volvia a recorrer
todos los objetos anteriores, con coste cuadratico, y acababa agotando el watchdog interno
de DXL en modulos grandes (seccion 7.1). Estas pruebas fijan el comportamiento por cursor
para que esa regresion no pueda volver sin que falle la suite.
"""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.sources.fake import FakeDoorsSource
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"
ATRIBUTOS = ("Object Heading", "Object Text")


def _modulo_de(n_objetos: int) -> FakeDoorsSource:
    origen = FakeDoorsSource(module_path=MODULO)
    for numero in range(1, n_objetos + 1):
        origen.anadir(numero, heading=f"Requisito {numero}", text="texto de prueba")
    return origen


@pytest.mark.parametrize("n_objetos", [50, 100, 200])
def test_cada_objeto_se_visita_una_sola_vez(n_objetos):
    """Coste lineal: N visitas para N objetos, sea cual sea el tamano del modulo.

    Con offset serian del orden de N^2/(2*page_size) visitas.
    """
    origen = _modulo_de(n_objetos)
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=ATRIBUTOS, sync_page_size=10)

        stats = SyncService(origen, repo, ajustes).sync_module(MODULO)

    assert stats.seen == n_objetos
    assert origen.objects_visited == n_objetos


def test_el_recorrido_no_pierde_ni_repite_objetos():
    """El cursor avanza estrictamente: ni saltos ni relecturas."""
    origen = _modulo_de(37)
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=ATRIBUTOS, sync_page_size=10)

        stats = SyncService(origen, repo, ajustes).sync_module(MODULO)

        assert stats.inserted == 37
        assert repo.count_requirements(MODULO) == 37
        numeros = [r["absolute_number"] for r in repo.list_requirements(MODULO, limit=100)]
        assert numeros == list(range(1, 38))


def test_los_huecos_en_la_numeracion_no_rompen_el_cursor():
    """Los Absolute Number de un modulo real tienen huecos: los objetos se borran.

    El cursor guarda el ultimo numero visitado, no una posicion, asi que debe funcionar
    igual con numeracion discontinua.
    """
    origen = FakeDoorsSource(module_path=MODULO)
    for numero in (3, 17, 18, 40, 41, 42, 99):
        origen.anadir(numero, heading=f"Requisito {numero}", text="texto")

    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=ATRIBUTOS, sync_page_size=2)

        stats = SyncService(origen, repo, ajustes).sync_module(MODULO)

        assert stats.seen == 7
        numeros = [r["absolute_number"] for r in repo.list_requirements(MODULO, limit=100)]
        assert numeros == [3, 17, 18, 40, 41, 42, 99]
