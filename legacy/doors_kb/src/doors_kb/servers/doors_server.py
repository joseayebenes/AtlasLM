"""Servidor MCP de acceso directo a DOORS Classic (hito H1).

Expone el catalogo de herramientas de la seccion 8 y aplica las reglas de seguridad de la
seccion 3.5 (RF-040..RF-044). Se ejecuta sobre stdio, en la maquina donde esta DOORS.

Tres reglas gobiernan todo el archivo:

* **Solo lectura.** No existe ninguna tool que ejecute DXL arbitrario (RF-040, ADR-003), y
  todas se anotan como ``read_only_hint`` (RF-042). Un agente no puede modificar requisitos.
* **Respuestas acotadas.** Todo lo que sale pasa por ``response.serializar``, que impone el
  limite duro configurable y avisa cuando recorta (RF-043, RF-044).
* **Los logs van a stderr.** El transporte es stdio y stdout esta reservado al protocolo:
  un ``print`` de mas corrompe la sesion MCP (RNF-009).

Arranque:

    python -m doors_kb.servers.doors_server
"""

from __future__ import annotations

import functools
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..config import Settings
from ..errors import DoorsKbError
from ..sources.doors.client import DoorsComClient
from . import response

logger = logging.getLogger("doors_kb.servers.doors")

# Anotacion comun: todas las herramientas de este servidor son de consulta (RF-042).
SOLO_LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def configurar_logging(nivel: int = logging.INFO) -> None:
    """Envia los logs a stderr.

    Es obligatorio, no una preferencia: con transporte stdio, cualquier escritura en stdout
    que no sea protocolo rompe la sesion MCP (RNF-009).
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=nivel,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def crear_servidor(settings: Settings | None = None, cliente: DoorsComClient | None = None):
    """Construye el servidor MCP con sus herramientas.

    Recibe la configuracion y el cliente como parametros para poder montarlo en un test con
    una sesion falsa, sin DOORS.
    """
    ajustes = settings or Settings.from_env()
    doors = cliente or DoorsComClient(ajustes)
    servidor = MCPServer(
        name="doors",
        instructions=(
            "Acceso de solo lectura a IBM DOORS Classic. Empieza por start_doors_session "
            "y espera a que el usuario complete el login en la ventana de DOORS. Usa "
            "list_object_attributes o validate_attributes antes de pedir atributos: los "
            "nombres deben coincidir exactamente. Las consultas grandes se recorren con "
            "'cursor', no con offset."
        ),
        version="0.4.0",
    )

    def responder(datos: dict[str, Any]) -> str:
        """Serializa aplicando el limite duro de respuesta (RF-043)."""
        return response.serializar(datos, ajustes.hard_max_response_chars)

    def con_errores(funcion):
        """Convierte los errores del proyecto en respuestas legibles para el agente.

        Un error propio lleva un mensaje pensado para actuar ("usa list_object_attributes
        para ver los nombres exactos"); propagar la traza de Python en su lugar no ayudaria
        a nadie. Los errores inesperados si se dejan subir.

        ``functools.wraps`` no es cosmetico aqui: el SDK de MCP deriva el esquema de
        entrada de cada tool inspeccionando la firma de la funcion, y sin ``__wrapped__``
        vería la del envoltorio (``*args, **kwargs``) en lugar de los parametros reales.
        """

        @functools.wraps(funcion)
        def envoltorio(*args, **kwargs) -> str:
            try:
                return funcion(*args, **kwargs)
            except DoorsKbError as exc:
                logger.warning("%s: %s", type(exc).__name__, exc)
                return responder({"error": type(exc).__name__, "message": str(exc)})

        return envoltorio

    # -----------------------------------------------------------------------------------
    # Sesion y configuracion
    # -----------------------------------------------------------------------------------

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def doors_configuration() -> str:
        """Muestra la configuracion efectiva: modulo por defecto, ProgID, timeouts y limites.

        Util como primer paso para comprobar que el servidor arranco con los valores que se
        esperaban antes de culpar a DOORS de un fallo.
        """
        return responder({"configuration": ajustes.describe()})

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def start_doors_session() -> str:
        """Crea la sesion Automation de DOORS y espera a que el usuario se autentique.

        Abre una ventana **nueva** de DOORS: hay que iniciar sesion en esa, no en una que ya
        estuviera abierta, porque la sesion de Automation es independiente.
        """
        return responder({"session": doors.start_session()})

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def doors_status(module_path: str | None = None) -> str:
        """Comprueba que la sesion esta viva y que el modulo se puede abrir en lectura."""
        return responder({"status": doors.status(module_path)})

    # -----------------------------------------------------------------------------------
    # Esquema
    # -----------------------------------------------------------------------------------

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def list_object_attributes(module_path: str | None = None) -> str:
        """Lista los atributos de objeto del modulo con tipo, ambito y enumeraciones.

        Conviene llamarla antes de cualquier consulta: los nombres de atributo de DOORS
        distinguen mayusculas y suelen llevar espacios ("Object Text").
        """
        ruta = ajustes.resolve_module_path(module_path)
        atributos = doors.list_object_attributes(ruta)
        return responder(
            {"module_path": ruta, "attributes": [a.to_dict() for a in atributos]}
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def validate_attributes(attributes: list[str], module_path: str | None = None) -> str:
        """Comprueba que los nombres de atributo existen y sugiere alternativas si no.

        Devuelve el informe completo en lugar de fallar, para poder corregir los nombres
        antes de lanzar una consulta cara.
        """
        ruta = ajustes.resolve_module_path(module_path)
        return responder({"validation": doors.validate_attributes(ruta, attributes).to_dict()})

    # -----------------------------------------------------------------------------------
    # Consulta de requisitos
    # -----------------------------------------------------------------------------------

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def list_requirements(
        module_path: str | None = None,
        attributes: list[str] | None = None,
        cursor: int | None = None,
        limit: int = 25,
        max_attribute_chars: int | None = None,
        include_deleted: bool = False,
        include_table_internals: bool = False,
        respect_display_set: bool = False,
    ) -> str:
        """Devuelve una pagina de requisitos del modulo.

        El recorrido avanza con 'cursor': pasa en la siguiente llamada el 'next_cursor' de
        la respuesta. Cuando 'next_cursor' es null, se llego al final del modulo.

        Con 'respect_display_set' el recorrido se limita a los objetos visibles en la vista
        actual del modulo, en lugar de recorrerlo entero.
        """
        ruta = ajustes.resolve_module_path(module_path)
        pagina = doors.fetch_page(
            ruta,
            attributes or list(ajustes.sync_attributes),
            cursor=cursor,
            page_size=limit,
            max_attribute_chars=max_attribute_chars or ajustes.max_attribute_chars,
            include_deleted=include_deleted,
            include_table_internals=include_table_internals,
            respect_display_set=respect_display_set,
        )
        return responder(
            {
                "module_path": ruta,
                "records": [r.to_dict() for r in pagina.records],
                "next_cursor": pagina.next_cursor,
            }
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def get_requirement(
        absolute_number: int,
        module_path: str | None = None,
        attributes: list[str] | None = None,
        max_attribute_chars: int | None = None,
    ) -> str:
        """Obtiene un requisito concreto por su Absolute Number."""
        ruta = ajustes.resolve_module_path(module_path)
        registro = doors.get_requirement(
            ruta,
            absolute_number,
            attributes or list(ajustes.sync_attributes),
            max_attribute_chars=max_attribute_chars or ajustes.max_attribute_chars,
        )
        if registro is None:
            return responder(
                {
                    "module_path": ruta,
                    "absolute_number": absolute_number,
                    "record": None,
                    "message": "No existe o esta borrado en DOORS.",
                }
            )
        return responder({"module_path": ruta, "record": registro.to_dict()})

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def search_requirements(
        query: str,
        module_path: str | None = None,
        attributes: list[str] | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        cursor: int | None = None,
        limit: int = 25,
        max_attribute_chars: int | None = None,
        respect_display_set: bool = False,
    ) -> str:
        """Busca texto literal o expresion regular dentro del modulo.

        La busqueda se ejecuta en DOORS: solo vuelven las coincidencias, no el modulo
        entero. Cada resultado indica en que atributo se encontro y en que posicion.
        """
        ruta = ajustes.resolve_module_path(module_path)
        pagina = doors.search(
            ruta,
            query,
            attributes or list(ajustes.sync_attributes),
            regex=regex,
            case_sensitive=case_sensitive,
            cursor=cursor,
            page_size=limit,
            max_attribute_chars=max_attribute_chars or ajustes.max_attribute_chars,
            respect_display_set=respect_display_set,
        )
        return responder(
            {
                "module_path": ruta,
                "query": query,
                "regex": regex,
                "case_sensitive": case_sensitive,
                "hits": [h.to_dict() for h in pagina.hits],
                "next_cursor": pagina.next_cursor,
            }
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def get_requirement_links(
        absolute_number: int, module_path: str | None = None, direction: str = "both"
    ) -> str:
        """Obtiene la trazabilidad de un requisito: 'outgoing', 'incoming' o 'both'.

        No incluye enlaces externos OSLC. Los enlaces entrantes exigen cargar los modulos
        origen, que puede fallar por permisos; esos fallos se reportan.
        """
        ruta = ajustes.resolve_module_path(module_path)
        enlaces = doors.get_links(ruta, absolute_number, direction=direction)
        return responder(
            {
                "module_path": ruta,
                "absolute_number": absolute_number,
                "direction": direction,
                "links": [enlace.to_dict() for enlace in enlaces],
                # Declaracion explicita del alcance de la trazabilidad (RF-037).
                "oslc_links_included": False,
            }
        )

    return servidor


def main() -> None:
    """Punto de entrada del servidor sobre stdio."""
    configurar_logging()
    logger.info("Iniciando servidor MCP 'doors' (solo lectura) sobre stdio")
    crear_servidor().run(transport="stdio")


if __name__ == "__main__":
    main()
