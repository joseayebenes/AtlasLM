"""Jerarquia de errores del proyecto.

Principio de diseno aplicado (seccion 2.1 de la especificacion): **fallos explicitos**. Un
atributo mal escrito, una sesion caida o un modulo inaccesible deben producir un error que
diga que paso y que hacer, en lugar de una lista vacia que el agente interpretaria como
"no hay requisitos que cumplan la condicion".

Todos los errores heredan de ``DoorsKbError`` para que las capas superiores (CLI, servidor
MCP) puedan distinguir un fallo previsto del proyecto de un fallo inesperado de Python.
"""

from __future__ import annotations


class DoorsKbError(Exception):
    """Error base de todo el proyecto."""


# --------------------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------------------


class ConfigurationError(DoorsKbError):
    """Una variable de entorno tiene un valor invalido.

    Se lanza al arrancar, no en mitad de una sincronizacion: es preferible no arrancar a
    trabajar con un timeout de 0 segundos porque alguien escribio ``"treinta"``.
    """


# --------------------------------------------------------------------------------------
# Sesion y acceso a DOORS
# --------------------------------------------------------------------------------------


class DoorsSessionError(DoorsKbError):
    """No se pudo crear, iniciar o usar la sesion Automation de DOORS."""


class DoorsModuleError(DoorsKbError):
    """El modulo solicitado no se pudo abrir en lectura."""

    def __init__(self, module_path: str, detail: str) -> None:
        super().__init__(
            f"No se pudo abrir el modulo '{module_path}' en lectura: {detail}. "
            "Comprueba la ruta completa (fullName) y los permisos del usuario en DOORS."
        )
        self.module_path = module_path
        self.detail = detail


class DxlExecutionError(DoorsKbError):
    """El interprete DXL devolvio un error al ejecutar el script generado."""

    def __init__(self, message: str, script: str | None = None) -> None:
        super().__init__(message)
        self.script = script


class WorkerTimeoutError(DoorsKbError):
    """Una llamada al worker COM supero su timeout externo (RNF-004)."""


class WorkerPoisonedError(DoorsKbError):
    """El worker COM quedo inutilizable tras un timeout previo (RNF-005).

    Tras un timeout no se sabe si el script DXL sigue ejecutandose dentro de DOORS. Reusar
    esa sesion podria mezclar la respuesta de una llamada con la siguiente, asi que el
    worker se marca como envenenado y exige reiniciar el proceso.
    """


# --------------------------------------------------------------------------------------
# Atributos
# --------------------------------------------------------------------------------------


class AttributeValidationError(DoorsKbError):
    """Uno o mas nombres de atributo no existen en el modulo (RF-012, RF-013).

    Incluye sugerencias de nombres proximos porque la causa habitual es una diferencia de
    mayusculas o un espacio de mas: ``"Object text"`` en lugar de ``"Object Text"``.
    """

    def __init__(self, module_path: str, unknown: dict[str, list[str]]) -> None:
        self.module_path = module_path
        self.unknown = unknown
        detalles = []
        for nombre, sugerencias in unknown.items():
            if sugerencias:
                detalles.append(f"'{nombre}' (quizas: {', '.join(sugerencias)})")
            else:
                detalles.append(f"'{nombre}'")
        super().__init__(
            f"Atributos inexistentes en el modulo '{module_path}': {'; '.join(detalles)}. "
            "Usa la herramienta list_object_attributes para ver los nombres exactos."
        )


# --------------------------------------------------------------------------------------
# Sincronizacion y respuestas
# --------------------------------------------------------------------------------------


class SyncError(DoorsKbError):
    """Fallo durante la sincronizacion de un modulo."""


class EmbeddingError(DoorsKbError):
    """Fallo al generar embeddings contra el proveedor configurado (hito H5)."""


class ResponseTooLargeError(DoorsKbError):
    """La respuesta no cabe en el limite duro configurado ni tras truncarla (RF-044)."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"La respuesta ocupa {size} caracteres y el limite es {limit}. "
            "Reduce 'limit', pide menos atributos o baja max_attribute_chars."
        )
        self.size = size
        self.limit = limit
