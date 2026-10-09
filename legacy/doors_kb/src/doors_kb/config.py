"""Configuracion del sistema a partir de variables de entorno.

Cubre la seccion 9.1 de la especificacion, mas los ajustes propios de la copia local
(RF-005, RF-059, RF-063, RF-064, RNF-003, RNF-004, RNF-007, RNF-010).

Se lee **una sola vez al arrancar** y se valida de forma estricta: un valor mal escrito
detiene el arranque en lugar de degradarse a un valor por defecto silencioso, que es
justo el tipo de fallo que luego aparece como un timeout inexplicable a mitad de una
sincronizacion de dos horas.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import ConfigurationError

# Atributos de contenido predeterminados para lectura, busqueda y sincronizacion (RF-014).
DEFAULT_CONTENT_ATTRIBUTES: tuple[str, ...] = ("Object Heading", "Object Text")


def _leer_entero(entorno: Mapping[str, str], nombre: str, defecto: int, *, minimo: int = 0) -> int:
    crudo = entorno.get(nombre, "").strip()
    if not crudo:
        return defecto
    try:
        valor = int(crudo)
    except ValueError as exc:
        raise ConfigurationError(
            f"{nombre} debe ser un numero entero; se recibio '{crudo}'."
        ) from exc
    if valor < minimo:
        raise ConfigurationError(f"{nombre} debe ser >= {minimo}; se recibio {valor}.")
    return valor


def _leer_lista(
    entorno: Mapping[str, str], nombre: str, defecto: tuple[str, ...]
) -> tuple[str, ...]:
    """Lee una lista separada por comas conservando los espacios internos.

    Los nombres de atributo de DOORS llevan espacios ("Object Heading"), asi que solo se
    recortan los extremos de cada elemento, nunca su interior.
    """
    crudo = entorno.get(nombre, "").strip()
    if not crudo:
        return defecto
    valores = tuple(parte.strip() for parte in crudo.split(",") if parte.strip())
    if not valores:
        raise ConfigurationError(f"{nombre} no contiene ningun nombre de atributo valido.")
    return valores


@dataclass(frozen=True)
class Settings:
    """Configuracion efectiva del proceso.

    Es inmutable a proposito: si una tool MCP necesita otro modulo o otro tamano de pagina,
    lo recibe como parametro de la llamada (RF-006) en vez de mutar el estado global.
    """

    # --- Sesion y acceso a DOORS -------------------------------------------------------
    module_path: str = ""
    prog_id: str = "DOORS.Application"
    start_timeout_seconds: int = 30
    dxl_timeout_seconds: int = 90
    dxl_run_limit_cycles: int = 0

    # --- Limites de respuesta del MCP --------------------------------------------------
    hard_max_response_chars: int = 500_000

    # --- Copia local y sincronizacion --------------------------------------------------
    db_path: str = "./doors_kb.sqlite3"
    sync_attributes: tuple[str, ...] = field(default=DEFAULT_CONTENT_ATTRIBUTES)
    sync_page_size: int = 25
    max_attribute_chars: int = 20_000
    source_last_modified_attribute: str = ""

    # --- Embeddings (hito H5) ----------------------------------------------------------
    embeddings_base_url: str = ""
    embeddings_api_key: str = ""
    embeddings_model: str = ""
    embeddings_attributes: tuple[str, ...] = ()
    embeddings_batch_size: int = 32
    embeddings_timeout_seconds: int = 60

    @classmethod
    def from_env(cls, entorno: Mapping[str, str] | None = None) -> Settings:
        """Construye la configuracion desde el entorno (por defecto ``os.environ``)."""
        env = os.environ if entorno is None else entorno
        return cls(
            module_path=env.get("DOORS_MODULE_PATH", "").strip(),
            prog_id=env.get("DOORS_PROG_ID", "").strip() or "DOORS.Application",
            start_timeout_seconds=_leer_entero(env, "DOORS_START_TIMEOUT_SECONDS", 30, minimo=1),
            dxl_timeout_seconds=_leer_entero(env, "DOORS_DXL_TIMEOUT_SECONDS", 90, minimo=1),
            # 0 desactiva el watchdog interno de DXL y deja el control al timeout externo
            # de Python, que es la configuracion recomendada para sincronizar (RNF-007).
            dxl_run_limit_cycles=_leer_entero(env, "DOORS_DXL_RUN_LIMIT_CYCLES", 0),
            hard_max_response_chars=_leer_entero(
                env, "DOORS_HARD_MAX_RESPONSE_CHARS", 500_000, minimo=1_000
            ),
            db_path=env.get("DOORS_DB_PATH", "").strip() or "./doors_kb.sqlite3",
            sync_attributes=_leer_lista(env, "DOORS_SYNC_ATTRIBUTES", DEFAULT_CONTENT_ATTRIBUTES),
            sync_page_size=_leer_entero(env, "DOORS_SYNC_PAGE_SIZE", 25, minimo=1),
            max_attribute_chars=_leer_entero(env, "DOORS_MAX_ATTRIBUTE_CHARS", 20_000, minimo=1),
            source_last_modified_attribute=env.get(
                "DOORS_SOURCE_LAST_MODIFIED_ATTRIBUTE", ""
            ).strip(),
            embeddings_base_url=env.get("EMBEDDINGS_BASE_URL", "").strip().rstrip("/"),
            embeddings_api_key=env.get("EMBEDDINGS_API_KEY", "").strip(),
            embeddings_model=env.get("EMBEDDINGS_MODEL", "").strip(),
            embeddings_attributes=_leer_lista(env, "EMBEDDINGS_ATTRIBUTES", ()),
            embeddings_batch_size=_leer_entero(env, "EMBEDDINGS_BATCH_SIZE", 32, minimo=1),
            embeddings_timeout_seconds=_leer_entero(
                env, "EMBEDDINGS_TIMEOUT_SECONDS", 60, minimo=1
            ),
        )

    def resolve_module_path(self, module_path: str | None = None) -> str:
        """Devuelve el modulo a usar: el explicito de la llamada o el predeterminado.

        Permite que las tools MCP consulten varios modulos sin reiniciar el proceso
        (RF-005, RF-006).
        """
        elegido = (module_path or self.module_path).strip()
        if not elegido:
            raise ConfigurationError(
                "No se indico ningun modulo: pasa 'module_path' en la llamada o define "
                "la variable de entorno DOORS_MODULE_PATH."
            )
        return elegido

    def describe(self) -> dict[str, object]:
        """Vista serializable de la configuracion, para la tool ``doors_configuration``.

        **No incluye la clave de la API de embeddings**, ni ningun otro secreto. Esta vista
        se envia al agente, que a su vez la pone en su contexto y puede reproducirla en una
        respuesta: una clave que entra aqui se considera comprometida. Solo se informa de si
        hay clave configurada o no, que es lo unico util para diagnosticar.
        """
        return {
            "module_path": self.module_path,
            "prog_id": self.prog_id,
            "start_timeout_seconds": self.start_timeout_seconds,
            "dxl_timeout_seconds": self.dxl_timeout_seconds,
            "dxl_run_limit_cycles": self.dxl_run_limit_cycles,
            "hard_max_response_chars": self.hard_max_response_chars,
            "db_path": self.db_path,
            "sync_attributes": list(self.sync_attributes),
            "sync_page_size": self.sync_page_size,
            "max_attribute_chars": self.max_attribute_chars,
            "source_last_modified_attribute": self.source_last_modified_attribute,
            "embeddings_base_url": self.embeddings_base_url,
            "embeddings_model": self.embeddings_model,
            "embeddings_attributes": list(self.embeddings_attributes),
            "embeddings_batch_size": self.embeddings_batch_size,
            "embeddings_api_key_configured": bool(self.embeddings_api_key),
        }
