"""Servicio de sincronizacion de un modulo hacia la copia local.

Implementa el algoritmo de la seccion 7 y responde a RF-055..RF-063 y RNF-015. Los
criterios de aceptacion CA-002 a CA-007 se comprueban sobre esta clase.

El orden de los pasos no es casual, y es lo unico que conviene entender antes de tocar
este archivo:

1. Validar **todos** los atributos antes de escribir nada. Descubrir a mitad del recorrido
   que un atributo estaba mal escrito deja la copia local a medias (RF-060, CA-007).
2. Recorrer el modulo por paginas con cursor, cada pagina en su propia transaccion.
3. Marcar ausentes como eliminados **solo si se llego al final del modulo**. Este es el
   punto critico: una sincronizacion interrumpida ha visto una parte del modulo, y marcar
   "lo que no aparecio" borraria logicamente requisitos que si existen (RF-061, CA-005).
4. Cerrar el historial siempre, tanto en exito como en fallo (RF-062).

El servicio no sabe nada de COM ni de DXL: recibe cualquier implementacion del protocolo
``RequirementsSource``, y por eso todo esto se prueba sin abrir DOORS (RNF-013).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ..config import Settings
from ..db.repository import SqliteRepository
from ..errors import SyncError
from ..models import RequirementRecord, SyncStats
from ..sources.base import RequirementsSource

logger = logging.getLogger(__name__)


class SyncService:
    """Copia un modulo de DOORS a la base local de forma incremental y segura."""

    def __init__(
        self,
        source: RequirementsSource,
        repository: SqliteRepository,
        settings: Settings | None = None,
    ) -> None:
        self.source = source
        self.repository = repository
        self.settings = settings or Settings()

    # -----------------------------------------------------------------------------------

    def sync_module(
        self,
        module_path: str,
        *,
        attributes: Sequence[str] | None = None,
        page_size: int | None = None,
        max_attribute_chars: int | None = None,
    ) -> SyncStats:
        """Sincroniza un modulo completo y devuelve sus estadisticas.

        Lanza ``AttributeValidationError`` si algun atributo no existe (antes de tocar la
        copia local) y ``SyncError`` si el recorrido se interrumpe. Esta ultima lleva las
        estadisticas parciales en ``error.stats``, para que quien la capture pueda informar
        de hasta donde se llego.
        """
        atributos = tuple(attributes or self.settings.sync_attributes)
        tamano_pagina = page_size or self.settings.sync_page_size
        max_chars = max_attribute_chars or self.settings.max_attribute_chars
        atributos = self._con_atributo_de_fecha(atributos)

        stats = SyncStats(module_path=module_path)
        run_id = self.repository.start_sync_run(module_path)

        # Paso 1: validacion previa. Si un atributo no existe, se aborta aqui, con la copia
        # local intacta (RF-060). El error se propaga tal cual, sin envolverlo: su mensaje
        # ya dice que nombre falla y que alternativas hay (RF-013), y envolverlo lo
        # confundiria con una interrupcion a mitad del recorrido.
        try:
            self.source.validate_attributes(module_path, atributos).raise_if_invalid()
        except Exception as exc:
            self._registrar_fallo(run_id, stats, exc)
            raise

        try:
            vistos = self._recorrer_modulo(module_path, atributos, tamano_pagina, max_chars, stats)

            # Paso 3: el marcado de ausentes depende de haber visto el final del modulo,
            # no de que no haya habido errores (RF-061).
            if stats.completed_module:
                stats.deleted = self.repository.mark_missing_as_deleted(module_path, vistos)

            self.repository.touch_module(module_path, full=stats.completed_module)
            self.repository.finish_sync_run(run_id, "success", stats)
            logger.info("Sincronizacion de '%s' correcta: %s", module_path, stats.to_dict())
            return stats

        except Exception as exc:
            self._registrar_fallo(run_id, stats, exc)
            fallo = SyncError(
                f"La sincronizacion del modulo '{module_path}' fallo tras {stats.pages} "
                f"pagina(s) y {stats.seen} requisito(s): {stats.error}. "
                "No se ha marcado ningun requisito como eliminado."
            )
            fallo.stats = stats  # type: ignore[attr-defined]
            raise fallo from exc

    def _registrar_fallo(self, run_id: int, stats: SyncStats, exc: BaseException) -> None:
        """Cierra el historial como fallido (RF-062).

        Una sincronizacion que falla y no deja rastro es indistinguible de una que nunca se
        lanzo, y eso impide diagnosticar por que la copia local esta como esta.
        """
        stats.error = f"{type(exc).__name__}: {exc}"
        self.repository.finish_sync_run(run_id, "failed", stats)
        logger.error("Sincronizacion de '%s' fallida: %s", stats.module_path, stats.error)

    # -----------------------------------------------------------------------------------

    def _recorrer_modulo(
        self,
        module_path: str,
        atributos: Sequence[str],
        page_size: int,
        max_attribute_chars: int,
        stats: SyncStats,
    ) -> set[int]:
        """Paso 2: recorre el modulo por paginas y devuelve los Absolute Number vistos."""
        vistos: set[int] = set()
        cursor: int | None = None

        while True:
            pagina = self.source.fetch_page(
                module_path,
                atributos,
                cursor=cursor,
                page_size=page_size,
                max_attribute_chars=max_attribute_chars,
                # El recorrido de sincronizacion cubre SIEMPRE el modulo completo, nunca la
                # vista visible (ADR-012). Con un filtro activo, los objetos ocultos
                # pareceran ausentes y mark_missing_as_deleted los marcaria como eliminados
                # sin haberlo sido. RF-022 es una opcion de consulta, no de sincronizacion.
                respect_display_set=False,
            )
            stats.pages += 1

            # Una transaccion por pagina: si el recorrido se corta, lo ya confirmado se
            # conserva y la pagina a medias se deshace entera (RNF-012).
            with self.repository.transaction():
                for registro in pagina.records:
                    registro = self._aplicar_fecha_de_origen(registro)
                    stats.registrar(self.repository.upsert_requirement(registro))
                    vistos.add(registro.absolute_number)

            logger.debug(
                "Modulo '%s': pagina %d, %d requisito(s), cursor %s",
                module_path,
                stats.pages,
                len(pagina.records),
                pagina.next_cursor,
            )

            if pagina.exhausted:
                stats.completed_module = True
                return vistos
            cursor = pagina.next_cursor

    # -----------------------------------------------------------------------------------
    # Mapeo opcional de un atributo del proyecto a source_last_modified (RF-063)
    # -----------------------------------------------------------------------------------

    def _con_atributo_de_fecha(self, atributos: Sequence[str]) -> tuple[str, ...]:
        """Anade el atributo de fecha al perfil si esta configurado y no estaba.

        Se anade aqui, y no se exige al usuario, para que configurarlo no obligue tambien a
        acordarse de incluirlo en DOORS_SYNC_ATTRIBUTES.
        """
        nombre = self.settings.source_last_modified_attribute
        if nombre and nombre not in atributos:
            return (*atributos, nombre)
        return tuple(atributos)

    def _aplicar_fecha_de_origen(self, registro: RequirementRecord) -> RequirementRecord:
        """Mueve el atributo configurado al campo ``source_last_modified`` (RF-063).

        Se **mueve**, no se copia: la fecha de modificacion es metadato, no contenido, y el
        hash solo cubre el contenido (RF-054). Si se quedara entre los atributos, DOORS
        tocando esa fecha bastaria para clasificar el requisito como ``updated`` y forzar a
        regenerar su embedding aunque el requisito no hubiera cambiado (RF-074).
        """
        nombre = self.settings.source_last_modified_attribute
        if not nombre:
            return registro
        restantes = {k: v for k, v in registro.attributes.items() if k != nombre}
        return replace(
            registro,
            attributes=restantes,
            source_last_modified=registro.attributes.get(nombre) or None,
        )
