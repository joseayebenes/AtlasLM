"""Worker COM: serializacion, timeouts, bloqueo y reintentos (RNF-002..RNF-006).

Se prueba sin Windows inyectando la inicializacion COM y funciones falsas. Lo que se
verifica aqui no es COM, sino las reglas que rodean a COM, que es donde estuvieron los
fallos reales del proyecto.
"""

import threading
import time

import pytest

from doors_kb.errors import WorkerPoisonedError, WorkerTimeoutError
from doors_kb.sources.doors.com_worker import (
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    ComWorker,
    es_error_transitorio,
)


class ErrorComFalso(Exception):
    """Imita un ``pywintypes.com_error``: el HRESULT va en el primer argumento."""


@pytest.fixture
def worker():
    """Worker con COM simulado y sin esperas reales entre reintentos."""
    w = ComWorker(
        reintentos=3,
        espera_inicial=0.01,
        inicializador=lambda: None,
        finalizador=lambda: None,
        dormir=lambda _segundos: None,
    )
    yield w
    w.stop()


def test_todas_las_llamadas_se_ejecutan_en_el_mismo_hilo(worker):
    """RNF-002: el objeto Automation vive en el apartamento del hilo que lo creo."""
    hilos = {worker.call(threading.get_ident, timeout=5) for _ in range(20)}

    assert len(hilos) == 1
    assert hilos.pop() != threading.get_ident()


def test_las_llamadas_se_atienden_en_orden(worker):
    """La cola es FIFO: DOORS no admite llamadas concurrentes."""
    orden = []

    for numero in range(10):
        worker.call(lambda n=numero: orden.append(n), timeout=5)

    assert orden == list(range(10))


def test_un_timeout_marca_el_worker_como_bloqueado(worker):
    """RNF-004 y RNF-005: el timeout no solo corta la espera, invalida la sesion."""
    with pytest.raises(WorkerTimeoutError, match="reinicia el proceso"):
        worker.call(lambda: time.sleep(0.5), timeout=0.05)

    assert worker.poisoned is True


def test_tras_un_timeout_toda_llamada_posterior_falla(worker):
    """El script DXL puede seguir corriendo dentro de DOORS: reusar la sesion es inseguro."""
    with pytest.raises(WorkerTimeoutError):
        worker.call(lambda: time.sleep(0.5), timeout=0.05)

    with pytest.raises(WorkerPoisonedError, match="Reinicia el proceso"):
        worker.call(lambda: 42, timeout=5)


def test_un_error_de_la_funcion_llega_intacto_a_quien_llama(worker):
    """Los errores de DOORS no se convierten en resultados vacios (seccion 2.1)."""
    def explota():
        raise ValueError("el modulo no existe")

    with pytest.raises(ValueError, match="el modulo no existe"):
        worker.call(explota, timeout=5)

    assert worker.poisoned is False  # un error de negocio no invalida la sesion


@pytest.mark.parametrize("hresult", [RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER])
def test_los_errores_de_doors_ocupado_se_reintentan(worker, hresult):
    """RNF-006: 'ocupado, reintente mas tarde' es transitorio."""
    intentos = []

    def ocupado_y_luego_bien():
        intentos.append(1)
        if len(intentos) < 3:
            raise ErrorComFalso(hresult, "Call was rejected by callee")
        return "listo"

    assert worker.call(ocupado_y_luego_bien, timeout=5) == "listo"
    assert len(intentos) == 3


def test_un_error_com_no_transitorio_no_se_reintenta(worker):
    """Un error de permisos no mejora repitiendo la llamada."""
    intentos = []

    def sin_permisos():
        intentos.append(1)
        raise ErrorComFalso(-2147024891, "Access is denied")

    with pytest.raises(ErrorComFalso):
        worker.call(sin_permisos, timeout=5)

    assert len(intentos) == 1


def test_si_doors_sigue_ocupado_el_error_acaba_saliendo(worker):
    """Los reintentos son finitos: no se puede esperar indefinidamente."""
    intentos = []

    def siempre_ocupado():
        intentos.append(1)
        raise ErrorComFalso(RPC_E_CALL_REJECTED, "Call was rejected by callee")

    with pytest.raises(ErrorComFalso):
        worker.call(siempre_ocupado, timeout=5)

    assert len(intentos) == 3


def test_se_reconoce_el_hresult_venga_como_atributo_o_como_argumento():
    """pywin32 expone el codigo de formas distintas segun la version."""
    como_argumento = ErrorComFalso(RPC_E_CALL_REJECTED, "rechazada")
    como_atributo = ErrorComFalso("rechazada")
    como_atributo.hresult = RPC_E_SERVERCALL_RETRYLATER

    assert es_error_transitorio(como_argumento) is True
    assert es_error_transitorio(como_atributo) is True
    assert es_error_transitorio(ValueError("otra cosa")) is False


def test_el_timeout_cubre_la_llamada_entera_con_sus_reintentos():
    """El timeout es el limite de paciencia del proceso, no el de cada intento."""
    worker = ComWorker(
        reintentos=5,
        espera_inicial=0.05,
        inicializador=lambda: None,
        finalizador=lambda: None,
        dormir=time.sleep,
    )
    try:
        def siempre_ocupado():
            raise ErrorComFalso(RPC_E_CALL_REJECTED, "rechazada")

        with pytest.raises(WorkerTimeoutError):
            worker.call(siempre_ocupado, timeout=0.08)
    finally:
        worker.stop()
