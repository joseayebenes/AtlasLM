"""La fuente falsa debe imitar los rasgos de DOORS que condicionan el diseno.

Si esta imitacion se desvia, el resto de tests dejan de demostrar lo que dicen demostrar.
"""

import pytest

from doors_kb.errors import AttributeValidationError
from doors_kb.sources.base import truncar


def test_el_recorrido_por_cursor_no_reescanea_el_prefijo(fuente, atributos):
    """RNF-015 / CA-006: coste lineal, no cuadratico como con offset (ADR-006)."""
    for numero in range(4, 31):
        fuente.anadir(numero, heading=f"Req {numero}", text="texto")

    cursor, paginas, recogidos = None, 0, []
    while True:
        pagina = fuente.fetch_page("/Demo/Reqs", atributos, cursor=cursor, page_size=10)
        recogidos += [r.absolute_number for r in pagina.records]
        paginas += 1
        if pagina.exhausted:
            break
        cursor = pagina.next_cursor

    assert recogidos == list(range(1, 31))
    assert paginas == 3
    # Cada objeto se visita una sola vez en todo el recorrido. Con paginacion por offset
    # habrian sido 10 + 20 + 30 = 60 visitas para el mismo resultado.
    assert fuente.objects_visited == 30


def test_los_objetos_borrados_se_excluyen_por_defecto(fuente, atributos):
    """RF-023: un objeto borrado en DOORS deja de aparecer en el recorrido."""
    fuente.borrar(2)

    pagina = fuente.fetch_page("/Demo/Reqs", atributos)

    assert [r.absolute_number for r in pagina.records] == [1, 3]


def test_las_filas_internas_de_tabla_se_excluyen_por_defecto(fuente, atributos):
    """RF-024: los objetos internos de tablas nativas no son requisitos."""
    fuente.anadir(4, heading="celda", text="valor", table_internal=True)

    visibles = fuente.fetch_page("/Demo/Reqs", atributos).records
    con_tablas = fuente.fetch_page("/Demo/Reqs", atributos, include_table_internals=True).records

    assert [r.absolute_number for r in visibles] == [1, 2, 3]
    assert [r.absolute_number for r in con_tablas] == [1, 2, 3, 4]


def test_un_atributo_inexistente_se_rechaza_con_sugerencias(fuente):
    """RF-012 y RF-013: error explicito en lugar de una cadena vacia silenciosa."""
    informe = fuente.validate_attributes("/Demo/Reqs", ["Object Text", "Object text "])

    assert informe.ok is False
    assert informe.unknown["Object text "] == ("Object Text",)  # y no todo el prefijo comun
    with pytest.raises(AttributeValidationError, match="quizas: Object Text"):
        informe.raise_if_invalid()


def test_el_truncado_deja_marca_visible():
    """RNF-011: el agente debe poder distinguir un texto corto de uno recortado."""
    recortado = truncar("x" * 100, 30)

    assert len(recortado) == 30
    assert recortado.endswith("... [truncado]")
    assert truncar("corto", 30) == "corto"


def test_la_busqueda_indica_atributo_y_posicion(fuente, atributos):
    """RF-034: la respuesta dice donde se encontro la coincidencia."""
    pagina = fuente.search("/Demo/Reqs", "30 segundos", atributos)

    assert len(pagina.hits) == 1
    hit = pagina.hits[0]
    assert hit.record.absolute_number == 2
    assert hit.matched_attribute == "Object Text"
    assert hit.match_text == "30 segundos"
    assert hit.match_start == fuente.get_requirement(
        "/Demo/Reqs", 2, atributos
    ).text.index("30 segundos")


def test_la_busqueda_distingue_mayusculas_solo_si_se_pide(fuente, atributos):
    """RF-032: la sensibilidad a mayusculas es configurable."""
    assert fuente.search("/Demo/Reqs", "timeout", atributos).hits
    assert not fuente.search("/Demo/Reqs", "timeout", atributos, case_sensitive=True).hits


def test_la_busqueda_admite_expresiones_regulares(fuente, atributos):
    """RF-033: busqueda por expresion regular."""
    pagina = fuente.search("/Demo/Reqs", r"\d+ segundos", atributos, regex=True)

    assert [h.record.absolute_number for h in pagina.hits] == [2]
