"""Protocolo comun de las fuentes de requisitos.

Esta es **la** frontera del sistema (ADR-008, RNF-013, RNF-014): el servicio de
sincronizacion y los servidores MCP hablan con este protocolo y nunca con COM ni con DXL.
Gracias a ello, toda la logica critica (paginacion, clasificacion de cambios, reglas de
borrado seguro) se prueba sin instalar DOORS.

Nota sobre el cursor (RF-058, ADR-006): el recorrido de un modulo avanza con el **ultimo
Absolute Number visitado**, nunca con un desplazamiento. Un offset obliga a la fuente a
recorrer otra vez todos los objetos anteriores en cada pagina, con coste cuadratico, que
es exactamente lo que agotaba el watchdog interno de DXL en modulos grandes.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from ..models import (
    AttributeDefinition,
    AttributeValidation,
    LinkRecord,
    RequirementPage,
    RequirementRecord,
    SearchPage,
)

# Direcciones aceptadas al pedir trazabilidad (RF-035).
LINK_DIRECTIONS = ("outgoing", "incoming", "both")


@runtime_checkable
class RequirementsSource(Protocol):
    """Origen de requisitos de solo lectura."""

    def list_object_attributes(self, module_path: str) -> list[AttributeDefinition]:
        """Lista los atributos de objeto definidos en el modulo (RF-010, RF-011)."""
        ...

    def validate_attributes(
        self, module_path: str, names: Sequence[str]
    ) -> AttributeValidation:
        """Comprueba que los nombres existen como atributos de objeto (RF-012, RF-013)."""
        ...

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
        """Devuelve una pagina de requisitos a partir del cursor (RF-020, RF-057, RF-058).

        ``cursor=None`` empieza por el primer objeto del modulo. La pagina devuelta lleva
        ``next_cursor=None`` cuando se ha alcanzado el final, que es la unica senal valida
        para que el sincronizador marque ausentes como eliminados (RF-061).

        Los objetos borrados y las filas internas de tablas nativas se excluyen por defecto
        (RF-023, RF-024). ``respect_display_set`` limita el recorrido a la vista visible del
        modulo (RF-022); es una opcion de consulta que el sincronizador nunca usa (ADR-012).
        """
        ...

    def get_requirement(
        self,
        module_path: str,
        absolute_number: int,
        attributes: Sequence[str],
        *,
        max_attribute_chars: int = 20_000,
    ) -> RequirementRecord | None:
        """Obtiene un objeto por su Absolute Number (RF-021). ``None`` si no existe."""
        ...

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
        """Busca texto literal o expresion regular dentro del modulo (RF-030..RF-034)."""
        ...

    def get_links(
        self, module_path: str, absolute_number: int, *, direction: str = "both"
    ) -> list[LinkRecord]:
        """Devuelve la trazabilidad entrante, saliente o ambas de un objeto (RF-035)."""
        ...


def sugerir_nombres(desconocido: str, disponibles: Iterable[str]) -> tuple[str, ...]:
    """Propone nombres de atributo parecidos a uno que no existe (RF-013).

    La causa habitual de un atributo "inexistente" es una diferencia de mayusculas o un
    espacio de mas ("Object text" en vez de "Object Text"), asi que la comparacion se hace
    sin distinguir mayusculas. Se usa aqui, y no en cada implementacion, para que la fuente
    falsa y DOORS den exactamente el mismo mensaje de error.

    El umbral es alto a proposito: los atributos de DOORS comparten prefijos largos
    ("Object Heading", "Object Text", "Object Identifier"), asi que un umbral laxo
    propondria los tres para cualquier error y la sugerencia dejaria de ayudar.
    """
    catalogo = list(disponibles)
    indice = {n.casefold(): n for n in catalogo}
    aproximados = difflib.get_close_matches(desconocido.casefold(), list(indice), n=3, cutoff=0.75)
    return tuple(indice[a] for a in aproximados)


def validar_nombres(
    module_path: str, names: Sequence[str], definiciones: Sequence[AttributeDefinition]
) -> AttributeValidation:
    """Construye el informe de validacion a partir del esquema real del modulo.

    Se comprueba contra las definiciones del modulo en lugar de intentar leer el atributo y
    ver si viene vacio: en DXL, ``objectAttributeText`` con ``noError`` devuelve cadena
    vacia tanto si el atributo no existe como si existe y esta vacio (seccion 10). Validar
    contra el esquema es lo que convierte ese fallo silencioso en un error explicito.
    """
    por_nombre = {d.name: d for d in definiciones}
    validos: dict[str, AttributeDefinition] = {}
    desconocidos: dict[str, tuple[str, ...]] = {}
    for nombre in names:
        definicion = por_nombre.get(nombre)
        if definicion is None:
            desconocidos[nombre] = sugerir_nombres(nombre, por_nombre)
        else:
            validos[nombre] = definicion
    return AttributeValidation(module_path=module_path, valid=validos, unknown=desconocidos)


def truncar(valor: str, max_chars: int) -> str:
    """Recorta un valor de atributo dejando constancia visible del recorte (RNF-011).

    El marcador importa: sin el, el agente no puede distinguir un texto corto de uno
    truncado y podria concluir que un requisito dice menos de lo que dice.
    """
    if max_chars <= 0 or len(valor) <= max_chars:
        return valor
    marca = "... [truncado]"
    if max_chars <= len(marca):
        return valor[:max_chars]
    return valor[: max_chars - len(marca)] + marca
