"""Autodiagnostico de primitivas DXL: generacion de las pruebas e interpretacion.

Lo que se puede comprobar aqui es que las pruebas estan bien construidas y que sus
resultados se interpretan bien. Que cada primitiva funcione o no solo lo dice DOORS.
"""

import pytest

from doors_kb.sources.doors import selftest


def test_hay_una_prueba_por_cada_primitiva_que_uso_el_proyecto():
    """Si el proyecto empieza a usar una primitiva nueva, aqui deberia haber una prueba."""
    cubiertas = " ".join(p.cuerpo for p in selftest.PRUEBAS)

    for primitiva in (
        "first(m)",
        "next(o)",
        "object(n, m)",
        "identifier(o)",
        "number(o)",
        "isDeleted(o)",
        "table(o)",
        "isVisible(o)",
        "attrEnumeration",
        "regexp2",
        "index(",
        "lower(",
        'o -> "*"',
        'o <- "*"',
    ):
        assert primitiva in cubiertas, f"falta una prueba para {primitiva}"


def test_las_pruebas_no_dependen_de_las_funciones_del_protocolo():
    """Si una auxiliar estuviera rota, todas fallarian y el diagnostico no serviria.

    Es lo que ocurrio de verdad: un fallo en `ns` habria hecho fallar cualquier prueba que
    la usara, sin distinguir la causa real.
    """
    for prueba in selftest.PRUEBAS:
        script = selftest.construir_script(prueba, "/P/R", "tok1")

        assert "string ns(" not in script
        assert "nsInt(" not in script


def test_las_dos_direcciones_de_la_concatenacion_se_prueban():
    """Confirmar cual acepta DOORS es lo que evita "incorrect arguments for (=)".

    Se prueban las dos a proposito: sin la comparacion, un fallo aislado no distingue "esta
    forma no vale" de "el problema es otro". Asi fue como se descubrio que la suposicion
    inicial estaba al reves.
    """
    cuerpos = [p.cuerpo for p in selftest.PRUEBAS]

    assert 'string valor = 42 ""' in cuerpos
    assert 'string valor = "" 42' in cuerpos


def test_se_aisla_cada_sospechoso_de_los_fallos_conocidos():
    """Una prueba que mezcla dos primitivas no dice cual de las dos falla.

    Al analizar la primera tabla, siete de ocho fallos se explicaban por la concatenacion
    invertida; el octavo mezclaba el cast de Absolute Number con object(). Cada sospechoso
    tiene ahora su propia prueba.
    """
    nombres = [p.nombre for p in selftest.PRUEBAS]

    assert "Absolute Number con cast explicito" in nombres
    assert "Absolute Number sin cast" in nombres
    assert "object(absno, m) para saltar al cursor" in nombres
    assert "object(m, absno) con los argumentos al reves" in nombres
    assert "length con variable intermedia" in nombres
    assert "length concatenado del tiron" in nombres


def test_cada_prueba_marca_su_respuesta_con_el_testigo():
    """Sin el, un script que aborta devolveria el resultado de la prueba anterior."""
    script = selftest.construir_script(selftest.PRUEBAS[0], "/P/R", "tok1")

    assert 'oleSetResult("tok1|" valor)' in script


def test_las_pruebas_que_necesitan_modulo_lo_abren_y_avisan_si_no_pueden():
    con_modulo = [p for p in selftest.PRUEBAS if p.necesita_modulo]
    assert con_modulo

    script = selftest.construir_script(con_modulo[0], "/P/R", "tok1")
    assert 'read("/P/R", false)' in script
    assert "NO_MODULE" in script


def test_las_pruebas_sin_modulo_no_lo_abren():
    """Comprobar la conversion de numeros no deberia depender de que el modulo se abra."""
    sin_modulo = [p for p in selftest.PRUEBAS if not p.necesita_modulo]
    assert sin_modulo

    assert "read(" not in selftest.construir_script(sin_modulo[0], "/P/R", "tok1")


def test_un_testigo_no_alfanumerico_se_rechaza():
    """El testigo se inserta sin escapar: tiene que ser inofensivo por diseno."""
    with pytest.raises(ValueError, match="alfanumerico"):
        selftest.construir_script(selftest.PRUEBAS[0], "/P/R", 'x"); halt; //')


# ---------------------------------------------------------------------------------------
# Interpretacion de resultados
# ---------------------------------------------------------------------------------------


def test_un_valor_esperado_que_coincide_es_un_exito():
    prueba = selftest.PRUEBAS[0]  # espera "42"

    assert selftest.interpretar(prueba, "tok1|42", "tok1") == (True, "42")


def test_un_valor_distinto_del_esperado_es_un_fallo():
    prueba = selftest.PRUEBAS[0]

    funciona, detalle = selftest.interpretar(prueba, "tok1|otra cosa", "tok1")

    assert funciona is False
    assert "se esperaba '42'" in detalle


def test_una_prueba_sin_valor_esperado_acepta_lo_que_devuelva():
    """Muchas primitivas devuelven datos del modulo, que no se conocen de antemano."""
    prueba = next(p for p in selftest.PRUEBAS if p.esperado is None)

    assert selftest.interpretar(prueba, "tok1|REQ-1", "tok1") == (True, "REQ-1")


def test_una_respuesta_sin_testigo_significa_que_el_script_aborto():
    """Es el caso importante: DOORS devuelve el resultado de la llamada anterior."""
    funciona, detalle = selftest.interpretar(selftest.PRUEBAS[0], "otra-cosa", "tok1")

    assert funciona is False
    assert "DXL output" in detalle


def test_un_modulo_que_no_abre_se_reporta_como_tal():
    funciona, detalle = selftest.interpretar(selftest.PRUEBAS[3], "tok1|NO_MODULE", "tok1")

    assert funciona is False
    assert "no se pudo abrir el modulo" in detalle
