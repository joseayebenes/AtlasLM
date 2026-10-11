"""Repositorio SQLite: clasificacion de cambios y borrado logico (RF-050..RF-062).

Estas pruebas son la evidencia de los criterios de aceptacion CA-002, CA-003 y CA-004
sobre la copia local, sin intervenir DOORS.
"""

import pytest

from doors_kb.db import SqliteRepository
from doors_kb.models import ChangeType, RequirementRecord, SyncStats

MODULO = "/Demo/Reqs"


@pytest.fixture
def repo() -> SqliteRepository:
    """Repositorio en memoria: cada test arranca con una copia local vacia."""
    with SqliteRepository(":memory:") as repositorio:
        yield repositorio


def _req(numero: int = 1, **cambios) -> RequirementRecord:
    base = {
        "module_path": MODULO,
        "absolute_number": numero,
        "identifier": f"REQ-{numero}",
        "outline_number": str(numero),
        "heading": "Timeout TCP",
        "text": "La conexion se cierra tras 30 segundos.",
        "attributes": {"Estado": "Aprobado"},
    }
    base.update(cambios)
    return RequirementRecord(**base)


def test_primera_insercion_y_segunda_sin_cambios(repo):
    """CA-002: dos pasadas identicas dan 1 inserted y despues 1 unchanged."""
    assert repo.upsert_requirement(_req()) is ChangeType.INSERTED
    assert repo.upsert_requirement(_req()) is ChangeType.UNCHANGED


def test_una_modificacion_produce_exactamente_updated(repo):
    """CA-003: cambiar el texto de un objeto lo clasifica como updated."""
    repo.upsert_requirement(_req())

    cambio = repo.upsert_requirement(_req(text="La conexion se cierra tras 60 segundos."))

    assert cambio is ChangeType.UPDATED
    assert repo.get_requirement(MODULO, 1)["text"].endswith("60 segundos.")


def test_cambiar_solo_un_atributo_tambien_es_updated(repo):
    """El hash cubre los atributos del proyecto, no solo heading y text (RF-054)."""
    repo.upsert_requirement(_req())

    cambio = repo.upsert_requirement(_req(attributes={"Estado": "Rechazado"}))

    assert cambio is ChangeType.UPDATED
    assert repo.get_requirement(MODULO, 1)["attributes"] == {"Estado": "Rechazado"}


def test_los_atributos_se_guardan_normalizados(repo):
    """RF-053: un atributo nuevo del proyecto no obliga a migrar el esquema."""
    repo.upsert_requirement(_req(attributes={"Estado": "Aprobado", "Criticidad": "SIL-2"}))

    guardado = repo.get_requirement(MODULO, 1)

    assert guardado["attributes"] == {"Estado": "Aprobado", "Criticidad": "SIL-2"}


def test_un_atributo_que_sale_del_perfil_desaparece_de_la_copia(repo):
    """Si deja de sincronizarse un atributo, no puede quedarse un valor fantasma."""
    repo.upsert_requirement(_req(attributes={"Estado": "Aprobado", "Criticidad": "SIL-2"}))

    repo.upsert_requirement(_req(attributes={"Estado": "Aprobado"}))

    assert repo.get_requirement(MODULO, 1)["attributes"] == {"Estado": "Aprobado"}


def test_marcar_ausentes_es_un_borrado_logico(repo):
    """RF-061: el requisito deja de estar activo pero su fila permanece."""
    repo.upsert_requirement(_req(1))
    repo.upsert_requirement(_req(2))

    borrados = repo.mark_missing_as_deleted(MODULO, seen={1})

    assert borrados == 1
    assert repo.get_requirement(MODULO, 2) is None
    assert repo.get_requirement(MODULO, 2, include_deleted=True)["is_deleted"] is True
    assert repo.count_requirements(MODULO) == 1
    assert repo.count_requirements(MODULO, include_deleted=True) == 2


def test_un_requisito_borrado_que_reaparece_se_reactiva_aunque_el_hash_coincida(repo):
    """RF-056: el caso que pierde una comparacion basada solo en el hash.

    El objeto vuelve de DOORS byte a byte identico. Si se clasificara como 'unchanged',
    seguiria con is_deleted = 1 y quedaria invisible en la copia local para siempre.
    """
    repo.upsert_requirement(_req(1))
    repo.mark_missing_as_deleted(MODULO, seen=set())
    assert repo.get_requirement(MODULO, 1) is None

    cambio = repo.upsert_requirement(_req(1))

    assert cambio is ChangeType.UPDATED
    assert repo.get_requirement(MODULO, 1) is not None


def test_marcar_ausentes_no_toca_lo_ya_borrado(repo):
    """Un segundo marcado no vuelve a contar los que ya estaban borrados."""
    repo.upsert_requirement(_req(1))
    assert repo.mark_missing_as_deleted(MODULO, seen=set()) == 1

    assert repo.mark_missing_as_deleted(MODULO, seen=set()) == 0


def test_el_historial_registra_el_resultado_de_cada_sincronizacion(repo):
    """RF-062: estado, tiempos y contadores quedan auditados."""
    run_id = repo.start_sync_run(MODULO)
    stats = SyncStats(module_path=MODULO, pages=2, seen=10, inserted=3, updated=1, unchanged=6)
    stats.completed_module = True

    repo.finish_sync_run(run_id, "success", stats)

    registro = repo.last_sync_run(MODULO)
    assert registro["status"] == "success"
    assert (registro["inserted"], registro["updated"], registro["unchanged"]) == (3, 1, 6)
    assert registro["finished_at"] is not None
    assert registro["error"] is None


def test_la_frescura_del_modulo_distingue_sincronizacion_completa(repo):
    """Riesgo R-007: el agente debe poder saber si la copia local esta al dia."""
    repo.upsert_requirement(_req(1))
    repo.touch_module(MODULO, full=False)
    parcial = repo.module_freshness(MODULO)

    repo.touch_module(MODULO, full=True)
    completa = repo.module_freshness(MODULO)

    assert parcial["last_sync_at"] is not None
    assert parcial["last_full_sync_at"] is None
    assert completa["last_full_sync_at"] is not None
    assert completa["active_requirements"] == 1


def test_una_transaccion_fallida_no_deja_escrituras_a_medias(repo):
    """RNF-012: la pagina que falla se deshace entera."""
    repo.upsert_requirement(_req(1))

    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.upsert_requirement(_req(2))
            raise RuntimeError("corte a mitad de la pagina")

    assert repo.count_requirements(MODULO, include_deleted=True) == 1


def test_el_repositorio_se_puede_usar_desde_varios_hilos(repo):
    """El servidor MCP ejecuta cada tool en un hilo de trabajo del SDK.

    Una conexion de sqlite3 esta atada por defecto al hilo que la creo, asi que sin
    serializar el acceso el servidor local fallaba en cuanto atendia una peticion.
    """
    import threading

    repo.upsert_requirement(_req(1))
    errores: list[BaseException] = []
    leidos: list[int] = []

    def consultar() -> None:
        try:
            for _ in range(20):
                leidos.append(repo.count_requirements(MODULO))
        except BaseException as exc:  # noqa: BLE001 - el test necesita el fallo, no el tipo
            errores.append(exc)

    hilos = [threading.Thread(target=consultar) for _ in range(4)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert errores == []
    assert leidos == [1] * 80


def test_las_escrituras_concurrentes_no_se_pisan(repo):
    """Cada requisito escrito desde un hilo distinto acaba en la base, una sola vez."""
    import threading

    def escribir(inicio: int) -> None:
        for numero in range(inicio, inicio + 25):
            with repo.transaction():
                repo.upsert_requirement(_req(numero))

    hilos = [threading.Thread(target=escribir, args=(base,)) for base in (1, 101, 201, 301)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert repo.count_requirements(MODULO) == 100
