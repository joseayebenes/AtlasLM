"""Fuente de requisitos en memoria que imita el comportamiento de DOORS.

Existe para cumplir RNF-013: poder probar la logica de sincronizacion, la clasificacion de
cambios y las reglas de borrado seguro **sin abrir DOORS**. Tambien es la forma mas rapida
de entender el sistema (``examples/demo_sync_fake.py``).

Imita deliberadamente los rasgos de DOORS que afectan al diseno:

* recorrido ordenado por Absolute Number con cursor, no con offset (ADR-006);
* truncado por atributo (RNF-011);
* objetos borrados que siguen existiendo en el modulo pero no se devuelven (RF-023);
* validacion de atributos contra el esquema del modulo (RF-012).

Si esta imitacion se aleja del comportamiento real, los tests dejan de demostrar lo que
dicen demostrar: cualquier hallazgo nuevo sobre DOORS deberia reflejarse aqui.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from ..errors import DoorsModuleError, DxlExecutionError
from ..models import (
    AttributeDefinition,
    AttributeValidation,
    LinkRecord,
    RequirementPage,
    RequirementRecord,
    SearchHit,
    SearchPage,
)
from .base import truncar, validar_nombres

# Atributos de sistema que DOORS define en todos los modulos formales.
ATRIBUTOS_DE_SISTEMA = (
    AttributeDefinition("Object Heading", "Text", is_system=True),
    AttributeDefinition("Object Text", "Text", is_system=True),
    AttributeDefinition("Object Identifier", "String", is_system=True),
    AttributeDefinition("Absolute Number", "Integer", is_system=True),
)


@dataclass
class _ObjetoFalso:
    """Un objeto dentro del modulo simulado.

    ``deleted`` reproduce el borrado de DOORS: el objeto sigue en el modulo pero no se
    devuelve en los recorridos normales. Es lo que permite probar la desaparicion y la
    posterior reaparicion de un requisito (RF-056).
    """

    record: RequirementRecord
    deleted: bool = False
    table_internal: bool = False
    visible: bool = True
    """Visible en la vista actual del modulo. Un filtro de DOORS puede ocultarlo sin
    borrarlo, que es la distincion que hace falta para probar RF-022."""


@dataclass
class FakeDoorsSource:
    """Modulo de DOORS simulado, manipulable desde los tests.

    Implementa el protocolo ``RequirementsSource``.
    """

    module_path: str = "/Demo/Reqs"
    attribute_definitions: list[AttributeDefinition] = field(default_factory=list)
    _objetos: dict[int, _ObjetoFalso] = field(default_factory=dict, repr=False)

    # Instrumentacion para pruebas -------------------------------------------------------
    fail_after_pages: int | None = None
    """Si se indica, la fuente falla al pedir la pagina numero N+1 (simula un corte)."""

    pages_served: int = 0
    objects_visited: int = 0
    """Objetos recorridos en total. Sirve para demostrar que el cursor no reescanea
    el prefijo del modulo en cada pagina (RNF-015, CA-006)."""

    def __post_init__(self) -> None:
        if not self.attribute_definitions:
            self.attribute_definitions = list(ATRIBUTOS_DE_SISTEMA)

    # -----------------------------------------------------------------------------------
    # API de manipulacion (solo para pruebas y demos)
    # -----------------------------------------------------------------------------------

    def definir_atributo(self, definicion: AttributeDefinition) -> None:
        self.attribute_definitions.append(definicion)

    def anadir(
        self,
        absolute_number: int,
        *,
        heading: str = "",
        text: str = "",
        attributes: dict[str, str] | None = None,
        outline_number: str = "",
        table_internal: bool = False,
        visible: bool = True,
    ) -> RequirementRecord:
        """Crea un objeto nuevo en el modulo."""
        record = RequirementRecord(
            module_path=self.module_path,
            absolute_number=absolute_number,
            identifier=f"REQ-{absolute_number}",
            outline_number=outline_number or str(absolute_number),
            heading=heading,
            text=text,
            attributes=dict(attributes or {}),
        )
        self._objetos[absolute_number] = _ObjetoFalso(
            record, table_internal=table_internal, visible=visible
        )
        return record

    def modificar(self, absolute_number: int, **campos) -> RequirementRecord:
        """Cambia campos de un objeto existente (simula una edicion en DOORS)."""
        objeto = self._objetos[absolute_number]
        objeto.record = replace(objeto.record, **campos)
        return objeto.record

    def borrar(self, absolute_number: int) -> None:
        """Marca el objeto como borrado en DOORS: deja de aparecer en los recorridos."""
        self._objetos[absolute_number].deleted = True

    def restaurar(self, absolute_number: int) -> None:
        """Deshace el borrado sin tocar el contenido.

        Este es el caso que hace falta para RF-056: el objeto vuelve con el **mismo** hash
        que tenia, asi que una comparacion basada solo en el hash lo daria por 'unchanged'
        y lo dejaria marcado como eliminado en la copia local para siempre.
        """
        self._objetos[absolute_number].deleted = False

    # -----------------------------------------------------------------------------------
    # Protocolo RequirementsSource
    # -----------------------------------------------------------------------------------

    def _comprobar_modulo(self, module_path: str) -> None:
        if module_path != self.module_path:
            raise DoorsModuleError(module_path, "la fuente falsa solo conoce su propio modulo")

    def list_object_attributes(self, module_path: str) -> list[AttributeDefinition]:
        self._comprobar_modulo(module_path)
        return list(self.attribute_definitions)

    def validate_attributes(self, module_path: str, names: Sequence[str]) -> AttributeValidation:
        self._comprobar_modulo(module_path)
        return validar_nombres(module_path, names, self.attribute_definitions)

    def ocultar(self, absolute_number: int) -> None:
        """Saca el objeto de la vista visible sin borrarlo (simula un filtro de DOORS)."""
        self._objetos[absolute_number].visible = False

    def _ordenados(
        self,
        *,
        include_deleted: bool,
        include_table_internals: bool,
        respect_display_set: bool = False,
    ):
        """Recorrido del modulo en el orden en que DOORS entrega los objetos."""
        for numero in sorted(self._objetos):
            objeto = self._objetos[numero]
            if objeto.deleted and not include_deleted:
                continue
            if objeto.table_internal and not include_table_internals:
                continue
            if respect_display_set and not objeto.visible:
                continue
            yield numero, objeto

    def _proyectar(
        self, record: RequirementRecord, attributes: Sequence[str], max_attribute_chars: int
    ) -> RequirementRecord:
        """Devuelve el objeto con solo los atributos pedidos, ya truncados."""
        disponibles = {
            "Object Heading": record.heading,
            "Object Text": record.text,
            **record.attributes,
        }
        seleccionados = {
            nombre: truncar(disponibles.get(nombre, ""), max_attribute_chars)
            for nombre in attributes
            if nombre not in ("Object Heading", "Object Text")
        }
        return replace(
            record,
            heading=truncar(record.heading, max_attribute_chars)
            if "Object Heading" in attributes
            else "",
            text=truncar(record.text, max_attribute_chars) if "Object Text" in attributes else "",
            attributes=seleccionados,
        )

    def fetch_page(
        self,
        module_path: str,
        attributes: Sequence[str],
        *,
        cursor: int | None = None,
        page_size: int = 25,
        max_attribute_chars: int = 20_000,
        include_deleted: bool = False,
        include_table_internals: bool = False,
        respect_display_set: bool = False,
    ) -> RequirementPage:
        self._comprobar_modulo(module_path)
        self.validate_attributes(module_path, attributes).raise_if_invalid()

        if self.fail_after_pages is not None and self.pages_served >= self.fail_after_pages:
            # Simula un corte de DOORS a mitad del recorrido (DXL Execution Timeout,
            # sesion caida, modulo bloqueado por otro usuario...).
            raise DxlExecutionError("fallo simulado de DOORS a mitad del recorrido")

        recogidos: list[RequirementRecord] = []
        ultimo: int | None = None
        agotado = True
        for numero, objeto in self._ordenados(
            include_deleted=include_deleted,
            include_table_internals=include_table_internals,
            respect_display_set=respect_display_set,
        ):
            # El cursor es el ultimo Absolute Number visitado: se continua *despues* de el,
            # sin volver a mirar los anteriores (ADR-006).
            if cursor is not None and numero <= cursor:
                continue
            if len(recogidos) >= page_size:
                agotado = False
                break
            self.objects_visited += 1
            recogidos.append(self._proyectar(objeto.record, attributes, max_attribute_chars))
            ultimo = numero

        self.pages_served += 1
        return RequirementPage(
            records=tuple(recogidos),
            next_cursor=None if agotado else ultimo,
        )

    def get_requirement(
        self,
        module_path: str,
        absolute_number: int,
        attributes: Sequence[str],
        *,
        max_attribute_chars: int = 20_000,
    ) -> RequirementRecord | None:
        self._comprobar_modulo(module_path)
        self.validate_attributes(module_path, attributes).raise_if_invalid()
        objeto = self._objetos.get(absolute_number)
        if objeto is None or objeto.deleted:
            return None
        return self._proyectar(objeto.record, attributes, max_attribute_chars)

    def search(
        self,
        module_path: str,
        query: str,
        attributes: Sequence[str],
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        cursor: int | None = None,
        page_size: int = 25,
        max_attribute_chars: int = 20_000,
        respect_display_set: bool = False,
    ) -> SearchPage:
        self._comprobar_modulo(module_path)
        self.validate_attributes(module_path, attributes).raise_if_invalid()

        banderas = 0 if case_sensitive else re.IGNORECASE
        patron = re.compile(query if regex else re.escape(query), banderas)

        hits: list[SearchHit] = []
        ultimo: int | None = None
        agotado = True
        for numero, objeto in self._ordenados(
            include_deleted=False,
            include_table_internals=False,
            respect_display_set=respect_display_set,
        ):
            if cursor is not None and numero <= cursor:
                continue
            if len(hits) >= page_size:
                agotado = False
                break
            proyectado = self._proyectar(objeto.record, attributes, max_attribute_chars)
            valores = {
                "Object Heading": proyectado.heading,
                "Object Text": proyectado.text,
                **proyectado.attributes,
            }
            for nombre in attributes:
                coincidencia = patron.search(valores.get(nombre, ""))
                if coincidencia:
                    hits.append(
                        SearchHit(
                            record=proyectado,
                            matched_attribute=nombre,
                            match_start=coincidencia.start(),
                            match_text=coincidencia.group(0),
                        )
                    )
                    break  # una coincidencia por objeto basta para localizarlo
            ultimo = numero

        return SearchPage(hits=tuple(hits), next_cursor=None if agotado else ultimo)

    def get_links(
        self, module_path: str, absolute_number: int, *, direction: str = "both"
    ) -> list[LinkRecord]:
        """La fuente falsa no simula trazabilidad: devuelve una lista vacia.

        Los enlaces se sincronizan en el hito H8 (Graph-RAG); cuando llegue ese hito, aqui
        hara falta un grafo simulado para poder probarlo sin DOORS.
        """
        self._comprobar_modulo(module_path)
        return []
