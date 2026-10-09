"""Recorrido por display set / vista visible (RF-022) y su prohibicion al sincronizar.

La segunda mitad de este archivo es la mas importante: fija por test la regla de ADR-012,
que impide que un filtro de vista provoque borrados logicos falsos.
"""

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.sources.doors import dxl
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


# ---------------------------------------------------------------------------------------
# Consulta: la vista visible es opcional (RF-022)
# ---------------------------------------------------------------------------------------


def test_por_defecto_se_recorre_el_modulo_completo(fuente, atributos):
    """Sin pedirlo, un objeto oculto por un filtro sigue apareciendo."""
    fuente.ocultar(2)

    pagina = fuente.fetch_page(MODULO, atributos)

    assert [r.absolute_number for r in pagina.records] == [1, 2, 3]


def test_con_display_set_se_omiten_los_objetos_ocultos(fuente, atributos):
    """RF-022: el recorrido se limita a la vista visible."""
    fuente.ocultar(2)

    pagina = fuente.fetch_page(MODULO, atributos, respect_display_set=True)

    assert [r.absolute_number for r in pagina.records] == [1, 3]


def test_ocultar_no_es_borrar(fuente, atributos):
    """Un filtro de DOORS oculta sin borrar: son dos estados distintos."""
    fuente.ocultar(2)

    assert fuente.get_requirement(MODULO, 2, atributos) is not None
    assert [r.absolute_number for r in fuente.fetch_page(MODULO, atributos).records] == [1, 2, 3]


def test_la_busqueda_tambien_respeta_la_vista_cuando_se_pide(fuente, atributos):
    fuente.ocultar(2)

    completa = fuente.search(MODULO, "segundos", atributos)
    visible = fuente.search(MODULO, "segundos", atributos, respect_display_set=True)

    assert [h.record.absolute_number for h in completa.hits] == [2]
    assert visible.hits == ()


def test_el_dxl_generado_filtra_por_visibilidad_solo_cuando_se_pide():
    """El predicado viaja dentro del script; su nombre exacto se valida con DOORS real."""
    completo = dxl.script_fetch_page("/P/R", ["Object Text"], "testigo01")
    visible = dxl.script_fetch_page(
        "/P/R", ["Object Text"], "testigo01", respect_display_set=True
    )

    assert "isVisible(o)" not in completo
    assert "!isVisible(o)" in visible
    busqueda = dxl.script_search("/P/R", "x", ["Object Text"], "testigo01",
                                 respect_display_set=True)
    assert "!isVisible(o)" in busqueda


# ---------------------------------------------------------------------------------------
# Sincronizacion: la vista visible NUNCA se usa (ADR-012)
# ---------------------------------------------------------------------------------------


class FuenteQueVigilaElParametro:
    """Envuelve la fuente falsa y registra con que valor se pidio cada pagina."""

    def __init__(self, fuente) -> None:
        self.fuente = fuente
        self.valores_recibidos: list[bool] = []

    def fetch_page(self, *args, respect_display_set: bool = False, **kwargs):
        self.valores_recibidos.append(respect_display_set)
        return self.fuente.fetch_page(*args, respect_display_set=respect_display_set, **kwargs)

    def __getattr__(self, nombre):
        return getattr(self.fuente, nombre)


def test_la_sincronizacion_nunca_pide_la_vista_visible(fuente, atributos):
    """ADR-012: el recorrido de sincronizacion cubre siempre el modulo completo."""
    espia = FuenteQueVigilaElParametro(fuente)
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=atributos, sync_page_size=2)

        SyncService(espia, repo, ajustes).sync_module(MODULO)

    assert espia.valores_recibidos and not any(espia.valores_recibidos)


def test_un_objeto_oculto_por_un_filtro_no_se_marca_como_eliminado(fuente, atributos):
    """El fallo que ADR-012 evita.

    Si la sincronizacion respetara el display set, el objeto oculto pareceria ausente y
    quedaria marcado como eliminado en la copia local sin haber desaparecido de DOORS.
    """
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=atributos, sync_page_size=2)
        sync = SyncService(fuente, repo, ajustes)
        sync.sync_module(MODULO)

        fuente.ocultar(2)
        stats = sync.sync_module(MODULO)

        assert stats.deleted == 0
        assert repo.get_requirement(MODULO, 2) is not None


@pytest.mark.parametrize("herramienta", ["list_requirements", "search_requirements"])
def test_las_tools_exponen_el_parametro(herramienta, fuente, atributos):
    """RF-022 llega hasta el agente, que decide si quiere la vista o el modulo entero."""
    import asyncio

    from doors_kb.servers.doors_server import crear_servidor

    servidor = crear_servidor(Settings(module_path=MODULO, sync_attributes=atributos), fuente)
    esquema = {t.name: t for t in asyncio.run(servidor.list_tools())}[herramienta]

    assert "respect_display_set" in esquema.input_schema["properties"]
