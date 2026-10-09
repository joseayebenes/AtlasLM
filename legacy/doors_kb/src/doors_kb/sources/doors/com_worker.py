"""Worker de hilo unico para todas las llamadas COM a DOORS.

Responde a RNF-002 a RNF-006. Existe por tres motivos, todos aprendidos durante el
desarrollo (seccion 10):

1. **Un solo hilo.** Las tools de un servidor MCP pueden ejecutarse desde hilos distintos,
   y el objeto Automation de DOORS vive en el apartamento COM del hilo que lo creo. Todas
   las llamadas se encolan hacia un hilo dedicado que hace ``CoInitialize`` una vez.
2. **Timeout externo.** ``future.result()`` sin timeout puede dejar el servidor MCP colgado
   para siempre si DOORS abre un dialogo modal y nadie lo atiende.
3. **Bloqueo declarado.** Tras un timeout no se sabe si el script DXL sigue corriendo dentro
   de DOORS. Reutilizar esa sesion podria mezclar la respuesta de una llamada con la
   siguiente, asi que el worker se marca como envenenado y exige reiniciar el proceso.

La logica de este modulo no depende de pywin32: la inicializacion COM se inyecta. Por eso
sus reglas (serializacion, timeout, envenenamiento, reintentos) se prueban sin Windows.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from ...errors import WorkerPoisonedError, WorkerTimeoutError

logger = logging.getLogger(__name__)

# HRESULT que DOORS devuelve cuando esta ocupado y pide reintentar (RNF-006). Son
# transitorios: ocurren, por ejemplo, mientras el usuario interactua con la ventana.
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
HRESULTS_TRANSITORIOS = frozenset({RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER})


def inicializar_com() -> None:
    """Inicializa COM en el hilo actual.

    pywin32 se importa aqui dentro y no arriba para que este modulo pueda importarse (y
    probarse) en Linux. Fuera de Windows no hay nada que inicializar.
    """
    try:
        import pythoncom  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("pythoncom no disponible: no se inicializa COM (entorno sin Windows)")
        return
    pythoncom.CoInitialize()


def finalizar_com() -> None:
    """Libera COM en el hilo actual."""
    try:
        import pythoncom  # type: ignore[import-not-found]
    except ImportError:
        return
    pythoncom.CoUninitialize()


def es_error_transitorio(exc: BaseException) -> bool:
    """Decide si un error COM merece un reintento (RNF-006).

    Se mira el HRESULT en lugar del tipo de excepcion para no tener que importar
    ``pywintypes`` en este modulo. Solo se reintentan los codigos de "ocupado, reintente":
    un error de permisos o un modulo inexistente no mejoran repitiendo la llamada.
    """
    codigo = getattr(exc, "hresult", None)
    if codigo is None and exc.args:
        codigo = exc.args[0] if isinstance(exc.args[0], int) else None
    return codigo in HRESULTS_TRANSITORIOS


class ComWorker:
    """Cola de trabajo serializada sobre un hilo con su propio apartamento COM."""

    def __init__(
        self,
        *,
        nombre: str = "doors-com",
        reintentos: int = 3,
        espera_inicial: float = 0.5,
        inicializador: Callable[[], None] = inicializar_com,
        finalizador: Callable[[], None] = finalizar_com,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.nombre = nombre
        self.reintentos = reintentos
        self.espera_inicial = espera_inicial
        self._inicializador = inicializador
        self._finalizador = finalizador
        self._dormir = dormir

        self._cola: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._hilo: threading.Thread | None = None
        self._envenenado = False
        self._motivo_envenenamiento = ""

    # -----------------------------------------------------------------------------------

    @property
    def poisoned(self) -> bool:
        """El worker quedo inutilizable tras un timeout (RNF-005)."""
        return self._envenenado

    def start(self) -> None:
        """Arranca el hilo si no estaba en marcha."""
        if self._hilo is not None and self._hilo.is_alive():
            return
        self._hilo = threading.Thread(target=self._bucle, name=self.nombre, daemon=True)
        self._hilo.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Pide al hilo que termine y espera a que libere COM."""
        if self._hilo is None:
            return
        self._cola.put(None)
        self._hilo.join(timeout=timeout)
        self._hilo = None

    def call(self, funcion: Callable[[], Any], *, timeout: float) -> Any:
        """Ejecuta ``funcion`` en el hilo COM y devuelve su resultado.

        El timeout cubre la llamada **entera**, reintentos incluidos: es un limite de
        paciencia del proceso, no de cada intento.
        """
        if self._envenenado:
            raise WorkerPoisonedError(
                f"El worker COM esta bloqueado desde una llamada anterior "
                f"({self._motivo_envenenamiento}). Reinicia el proceso antes de "
                "volver a consultar DOORS."
            )

        self.start()
        futuro: Future = Future()
        self._cola.put((funcion, futuro))
        try:
            return futuro.result(timeout=timeout)
        except FutureTimeoutError as exc:
            # No se cancela la llamada en curso porque no se puede: DXL sigue corriendo
            # dentro de DOORS. Lo unico honesto es dejar de usar esta sesion.
            self._envenenar(f"timeout de {timeout} s")
            raise WorkerTimeoutError(
                f"La llamada a DOORS supero el timeout de {timeout} segundos. "
                "El script DXL puede seguir ejecutandose dentro de DOORS, asi que la "
                "sesion queda marcada como no utilizable: reinicia el proceso. "
                "Si el modulo es grande, prueba con una pagina mas pequena."
            ) from exc

    # -----------------------------------------------------------------------------------

    def _envenenar(self, motivo: str) -> None:
        self._envenenado = True
        self._motivo_envenenamiento = motivo
        logger.error("Worker COM marcado como bloqueado: %s", motivo)

    def _bucle(self) -> None:
        """Cuerpo del hilo: inicializa COM una vez y atiende la cola en orden."""
        self._inicializador()
        try:
            while True:
                tarea = self._cola.get()
                if tarea is None:
                    return
                funcion, futuro = tarea
                if not futuro.set_running_or_notify_cancel():
                    continue
                try:
                    futuro.set_result(self._ejecutar_con_reintentos(funcion))
                except BaseException as exc:  # se traslada intacta a quien espera
                    futuro.set_exception(exc)
        finally:
            self._finalizador()

    def _ejecutar_con_reintentos(self, funcion: Callable[[], Any]) -> Any:
        """Reintenta solo los errores de 'DOORS ocupado' (RNF-006).

        La espera crece en cada intento: si DOORS esta ocupado porque el usuario tiene un
        dialogo abierto, insistir cada pocos milisegundos no ayuda.
        """
        espera = self.espera_inicial
        for intento in range(1, self.reintentos + 1):
            try:
                return funcion()
            except Exception as exc:
                if intento >= self.reintentos or not es_error_transitorio(exc):
                    raise
                logger.warning(
                    "DOORS ocupado (intento %d de %d); reintentando en %.1f s",
                    intento,
                    self.reintentos,
                    espera,
                )
                self._dormir(espera)
                espera *= 2
        raise AssertionError("inalcanzable")  # pragma: no cover
