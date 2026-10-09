"""Modelo de dominio compartido por todas las capas.

Cubre RF-025 (contenido minimo de un requisito), RF-052 (identidad logica),
RF-054 (hash de contenido) y RF-011 (metadatos de atributo).

Estas estructuras son el idioma comun entre la fuente de datos (DOORS o una fuente falsa),
el repositorio SQLite y los servidores MCP. No dependen de COM, ni de sqlite3, ni de MCP:
esa independencia es la que permite probar la logica sin DOORS (ADR-008, RNF-013).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum


class ChangeType(StrEnum):
    """Resultado de comparar un requisito de DOORS con su copia local (RF-055).

    Es un ``StrEnum`` para poder serializarse a JSON y guardarse en SQLite sin conversiones.
    """

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def _normalizar(texto: str) -> str:
    """Normaliza un texto antes de que entre en el hash.

    DOORS devuelve saltos de linea CRLF y, segun la version del cliente, a veces CR sueltos.
    Esa diferencia no es un cambio de requisito: si no se normalizara, el mismo objeto
    podria clasificarse como ``updated`` en cada sincronizacion y forzaria a regenerar su
    embedding sin motivo (RF-074).
    """
    return texto.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class AttributeDefinition:
    """Definicion de un atributo tal y como lo declara el modulo (RF-011).

    Los metadatos importan para el agente: saber que ``Estado`` es una enumeracion con
    valores concretos evita que busque por un valor que no existe.
    """

    name: str
    type_name: str = ""
    is_object: bool = True
    is_module: bool = False
    is_system: bool = False
    multi_valued: bool = False
    enum_values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type_name,
            "is_object": self.is_object,
            "is_module": self.is_module,
            "is_system": self.is_system,
            "multi_valued": self.multi_valued,
            "enum_values": list(self.enum_values),
        }


@dataclass(frozen=True)
class RequirementRecord:
    """Un objeto de DOORS con los datos que se sincronizan y se exponen al agente.

    La identidad logica es ``(module_path, absolute_number)`` (RF-052): el Absolute Number
    por si solo no basta, porque se repite entre modulos distintos.
    """

    module_path: str
    absolute_number: int
    identifier: str = ""
    outline_number: str = ""
    heading: str = ""
    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    source_last_modified: str | None = None

    def canonical_payload(self) -> str:
        """Representacion canonica de los campos que definen el contenido.

        Deliberadamente **no** incluye ``module_path`` ni ``absolute_number``: esos son la
        identidad, no el contenido. Tampoco incluye ``source_last_modified``, que puede
        cambiar sin que cambie ningun dato del requisito.

        ``sort_keys`` garantiza que el hash no dependa del orden en que la fuente devuelva
        los atributos, que en DXL no esta garantizado.
        """
        return json.dumps(
            {
                "identifier": _normalizar(self.identifier),
                "outline_number": _normalizar(self.outline_number),
                "heading": _normalizar(self.heading),
                "text": _normalizar(self.text),
                "attributes": {k: _normalizar(v) for k, v in self.attributes.items()},
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        """SHA-256 del contenido, base de la deteccion incremental (RF-054, ADR-005)."""
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """Vista serializable para las respuestas MCP (RF-025)."""
        datos: dict[str, object] = {
            "module_path": self.module_path,
            "absolute_number": self.absolute_number,
            "identifier": self.identifier,
            "outline_number": self.outline_number,
            "heading": self.heading,
            "text": self.text,
            "attributes": dict(self.attributes),
        }
        if self.source_last_modified:
            datos["source_last_modified"] = self.source_last_modified
        return datos


@dataclass(frozen=True)
class RequirementPage:
    """Una pagina del recorrido de un modulo (RF-057, RF-058).

    ``next_cursor`` es el **ultimo Absolute Number visitado**, no un desplazamiento: la
    siguiente pagina continua desde ahi en lugar de reescanear el prefijo del modulo
    (ADR-006, seccion 7.1).

    ``next_cursor is None`` significa "se llego al final del modulo". El sincronizador solo
    puede marcar ausentes como eliminados cuando ha visto ese final (RF-061).
    """

    records: tuple[RequirementRecord, ...]
    next_cursor: int | None

    @property
    def exhausted(self) -> bool:
        return self.next_cursor is None


@dataclass(frozen=True)
class LinkRecord:
    """Un enlace de trazabilidad entre dos objetos (RF-035).

    No cubre enlaces externos OSLC; asi se declara explicitamente en las respuestas
    del servidor MCP (RF-037).
    """

    source_module: str
    source_absolute_number: int
    target_module: str
    target_absolute_number: int
    link_module: str = ""
    direction: str = "outgoing"  # "outgoing" | "incoming"

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "source_module": self.source_module,
            "source_absolute_number": self.source_absolute_number,
            "target_module": self.target_module,
            "target_absolute_number": self.target_absolute_number,
            "link_module": self.link_module,
        }


@dataclass(frozen=True)
class AttributeValidation:
    """Resultado de validar nombres de atributo contra el esquema del modulo (RF-012).

    Se devuelve como dato en lugar de lanzar directamente porque las dos capas que lo usan
    necesitan cosas distintas: la tool MCP ``validate_attributes`` quiere ensenar el informe
    completo al agente (RF-013), mientras que el sincronizador quiere abortar antes de
    escribir nada (RF-060). El segundo caso se resuelve con ``raise_if_invalid()``.
    """

    module_path: str
    valid: dict[str, AttributeDefinition] = field(default_factory=dict)
    unknown: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unknown

    def raise_if_invalid(self) -> None:
        """Lanza ``AttributeValidationError`` si algun nombre no existe."""
        if self.unknown:
            from .errors import AttributeValidationError

            raise AttributeValidationError(
                self.module_path, {k: list(v) for k, v in self.unknown.items()}
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "valid": [d.to_dict() for d in self.valid.values()],
            "unknown": [
                {"name": nombre, "suggestions": list(sug)} for nombre, sug in self.unknown.items()
            ],
            "ok": self.ok,
        }


@dataclass(frozen=True)
class SearchHit:
    """Una coincidencia de busqueda dentro de un requisito (RF-034).

    Indica en que atributo se encontro y en que posicion, para que el agente pueda citar
    la parte relevante sin descargarse el objeto entero.
    """

    record: RequirementRecord
    matched_attribute: str
    match_start: int = -1
    match_text: str = ""

    def to_dict(self) -> dict[str, object]:
        datos = self.record.to_dict()
        datos["match"] = {
            "attribute": self.matched_attribute,
            "start": self.match_start,
            "text": self.match_text,
        }
        return datos


@dataclass(frozen=True)
class SearchPage:
    """Pagina de resultados de busqueda, con el mismo modelo de cursor que el recorrido."""

    hits: tuple[SearchHit, ...]
    next_cursor: int | None

    @property
    def exhausted(self) -> bool:
        return self.next_cursor is None


@dataclass
class SyncStats:
    """Contadores y resultado de una sincronizacion (RF-062).

    Es mutable porque el servicio la va rellenando pagina a pagina; se vuelca al historial
    ``sync_runs`` al terminar, tanto si la sincronizacion acaba bien como si falla.
    """

    module_path: str = ""
    pages: int = 0
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    completed_module: bool = False
    error: str | None = None

    def registrar(self, cambio: ChangeType) -> None:
        """Suma un requisito procesado al contador que le corresponde."""
        self.seen += 1
        if cambio is ChangeType.INSERTED:
            self.inserted += 1
        elif cambio is ChangeType.UPDATED:
            self.updated += 1
        else:
            self.unchanged += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "pages": self.pages,
            "requirements_seen": self.seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "completed_module": self.completed_module,
            "error": self.error,
        }
