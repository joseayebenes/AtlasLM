"""Hash canonico de contenido: la base de la deteccion incremental (RF-054, ADR-005)."""

from doors_kb.models import ChangeType, RequirementPage, RequirementRecord, SyncStats


def _req(**cambios) -> RequirementRecord:
    base = {
        "module_path": "/Proyecto/Reqs",
        "absolute_number": 7,
        "identifier": "REQ-7",
        "outline_number": "1.2",
        "heading": "Timeout de conexion",
        "text": "El sistema debera cerrar la conexion TCP tras 30 segundos.",
        "attributes": {"Estado": "Aprobado", "Prioridad": "Alta"},
    }
    base.update(cambios)
    return RequirementRecord(**base)


def test_el_hash_no_depende_del_orden_de_los_atributos():
    """DXL no garantiza el orden de los atributos: el hash debe ser estable igualmente."""
    a = _req(attributes={"Estado": "Aprobado", "Prioridad": "Alta"})
    b = _req(attributes={"Prioridad": "Alta", "Estado": "Aprobado"})

    assert a.content_hash() == b.content_hash()


def test_el_hash_ignora_la_diferencia_entre_crlf_y_lf():
    """Un cambio de fin de linea no es un cambio de requisito: evita 'updated' espurios."""
    a = _req(text="Linea uno\r\nLinea dos")
    b = _req(text="Linea uno\nLinea dos")

    assert a.content_hash() == b.content_hash()


def test_el_hash_cambia_si_cambia_el_texto():
    original = _req()
    modificado = _req(text="El sistema debera cerrar la conexion TCP tras 60 segundos.")

    assert original.content_hash() != modificado.content_hash()


def test_el_hash_cambia_si_cambia_el_valor_de_un_atributo():
    original = _req()
    modificado = _req(attributes={"Estado": "Rechazado", "Prioridad": "Alta"})

    assert original.content_hash() != modificado.content_hash()


def test_el_hash_no_depende_de_la_identidad_ni_de_la_fecha_de_modificacion():
    """La identidad y la marca temporal no son contenido: no deben provocar 'updated'."""
    a = _req()
    b = _req(module_path="/Otro/Modulo", absolute_number=99, source_last_modified="2026-01-01")

    assert a.content_hash() == b.content_hash()


def test_una_pagina_sin_cursor_indica_el_final_del_modulo():
    """RF-061: solo con el modulo agotado se puede marcar ausentes como eliminados."""
    assert RequirementPage(records=(), next_cursor=None).exhausted is True
    assert RequirementPage(records=(_req(),), next_cursor=7).exhausted is False


def test_los_contadores_de_sincronizacion_clasifican_cada_requisito():
    """RF-062: el historial distingue altas, cambios y objetos sin cambios."""
    stats = SyncStats(module_path="/Proyecto/Reqs")
    stats.registrar(ChangeType.INSERTED)
    stats.registrar(ChangeType.UPDATED)
    stats.registrar(ChangeType.UNCHANGED)
    stats.registrar(ChangeType.UNCHANGED)

    assert (stats.seen, stats.inserted, stats.updated, stats.unchanged) == (4, 1, 1, 2)
