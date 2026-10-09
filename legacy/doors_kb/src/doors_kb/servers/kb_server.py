"""Servidor MCP sobre la copia local (RF-078, hito H7).

Es el companero del servidor directo, y estan separados a proposito: este **no necesita
Windows ni DOORS**. Responde desde SQLite, con la latencia de una consulta local, y es donde
un agente deberia hacer sus busquedas masivas.

A cambio tiene una limitacion que el servidor directo no tiene: refleja la ultima
sincronizacion, no el estado vivo de DOORS. Por eso **todas** las respuestas llevan la
frescura de la copia local (riesgo R-007): un indice desactualizado que responde sin avisar
es peor que uno que no responde, porque el agente no tiene forma de saber que le estan
contestando con datos viejos.

Arranque:

    python -m doors_kb.servers.kb_server
"""

from __future__ import annotations

import functools
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..config import Settings
from ..db.repository import SqliteRepository
from ..embeddings.provider import EmbeddingProvider, OpenAICompatibleProvider
from ..errors import DoorsKbError
from ..search import buscar_hibrida, buscar_lexical, buscar_vectorial
from . import response

logger = logging.getLogger("doors_kb.servers.kb")

SOLO_LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def configurar_logging(nivel: int = logging.INFO) -> None:
    """Logs a stderr: stdout es del protocolo MCP (RNF-009)."""
    logging.basicConfig(
        stream=sys.stderr,
        level=nivel,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def crear_servidor(
    settings: Settings | None = None,
    repositorio: SqliteRepository | None = None,
    provider: EmbeddingProvider | None = None,
):
    """Construye el servidor MCP local.

    El repositorio y el proveedor se pueden inyectar para montarlo en un test con una base
    en memoria y embeddings falsos, sin red.
    """
    ajustes = settings or Settings.from_env()
    repo = repositorio or SqliteRepository(ajustes.db_path)

    def proveedor_de_embeddings() -> EmbeddingProvider:
        """Crea el proveedor solo cuando hace falta.

        Las busquedas lexicales no necesitan embeddings: exigir la configuracion del
        proveedor para arrancar dejaria inutilizable el servidor entero por una funcion que
        quiza no se use.
        """
        if provider is not None:
            return provider
        return OpenAICompatibleProvider(
            ajustes.embeddings_base_url,
            ajustes.embeddings_api_key,
            ajustes.embeddings_model,
            timeout=ajustes.embeddings_timeout_seconds,
            batch_size=ajustes.embeddings_batch_size,
        )

    servidor = MCPServer(
        name="doors-kb",
        instructions=(
            "Base de conocimiento local de requisitos de DOORS. Responde desde una copia "
            "SQLite sincronizada, sin consultar DOORS, asi que es rapida pero refleja la "
            "ultima sincronizacion: cada respuesta incluye 'freshness'. Usa kb_search para "
            "identificadores y terminos exactos, kb_semantic_search para preguntas "
            "conceptuales y kb_hybrid_search cuando no sepas cual encaja mejor. Si el dato "
            "tiene que estar al dia, verificalo despues en el servidor 'doors'."
        ),
        version="0.4.0",
    )

    def frescura(module_path: str) -> dict[str, object]:
        """Estado de la copia local para el modulo consultado (riesgo R-007)."""
        datos = repo.module_freshness(module_path)
        if datos is None:
            return {
                "module_path": module_path,
                "synchronized": False,
                "warning": (
                    "Este modulo no se ha sincronizado nunca en esta copia local. "
                    "Ejecuta doors-sync o consulta el servidor 'doors'."
                ),
            }
        datos["synchronized"] = True
        if datos.get("last_full_sync_at") is None:
            datos["warning"] = (
                "La copia local nunca ha completado una sincronizacion entera de este "
                "modulo: puede faltar informacion."
            )
        return datos

    def responder(module_path: str, datos: dict[str, Any]) -> str:
        """Serializa aplicando el limite duro y adjuntando siempre la frescura."""
        return response.serializar(
            {**datos, "freshness": frescura(module_path)}, ajustes.hard_max_response_chars
        )

    def con_errores(funcion):
        """Traduce los errores del proyecto a respuestas legibles para el agente."""

        @functools.wraps(funcion)
        def envoltorio(*args, **kwargs) -> str:
            try:
                return funcion(*args, **kwargs)
            except DoorsKbError as exc:
                logger.warning("%s: %s", type(exc).__name__, exc)
                return response.serializar(
                    {"error": type(exc).__name__, "message": str(exc)},
                    ajustes.hard_max_response_chars,
                )

        return envoltorio

    # -----------------------------------------------------------------------------------

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_status(module_path: str | None = None) -> str:
        """Informa de si la copia local esta al dia y cuanta informacion contiene.

        Conviene consultarla antes de fiarse de una busqueda: dice cuando se sincronizo el
        modulo por ultima vez, cuantos requisitos hay y cuantos estan indexados.
        """
        ruta = ajustes.resolve_module_path(module_path)
        return responder(
            ruta,
            {
                "status": {
                    "module_path": ruta,
                    "requirements": repo.count_requirements(ruta),
                    "indexed_for_text_search": repo.count_indexed(ruta),
                    "last_sync_run": repo.last_sync_run(ruta),
                    "last_embedding_run": repo.last_embedding_run(ruta),
                }
            },
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_search(
        query: str,
        module_path: str | None = None,
        limit: int = 25,
        column: str | None = None,
        advanced_syntax: bool = False,
    ) -> str:
        """Busqueda textual en la copia local. Rapida y exacta.

        Es la mejor opcion para identificadores, codigos y terminos tecnicos literales.
        Con 'column' se restringe a un campo ('identifier', 'heading', 'text', 'attributes').
        Con 'advanced_syntax' se admite la sintaxis de FTS5 (OR, NEAR, prefijos con *);
        por defecto la consulta se trata como texto literal.
        """
        ruta = ajustes.resolve_module_path(module_path)
        resultados = buscar_lexical(
            repo,
            query,
            module_path=ruta,
            limit=limit,
            modo="advanced" if advanced_syntax else "literal",
            columna=column,
        )
        return responder(
            ruta, {"query": query, "records": [r.to_dict() for r in resultados]}
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_semantic_search(
        query: str, module_path: str | None = None, limit: int = 25, filters: dict | None = None
    ) -> str:
        """Busqueda por significado sobre los embeddings de la copia local.

        Util cuando no conoces las palabras exactas del requisito. 'filters' acepta pares
        atributo-valor ({"Criticidad": "alta"}) que se aplican antes de puntuar.
        """
        ruta = ajustes.resolve_module_path(module_path)
        resultados = buscar_vectorial(
            repo,
            query,
            proveedor_de_embeddings(),
            module_path=ruta,
            filtros_atributos=filters,
            limit=limit,
        )
        return responder(
            ruta, {"query": query, "records": [r.to_dict() for r in resultados]}
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_hybrid_search(
        query: str,
        module_path: str | None = None,
        limit: int = 25,
        filters: dict | None = None,
        lexical_weight: float = 1.0,
        semantic_weight: float = 1.0,
    ) -> str:
        """Combina la busqueda textual y la semantica en un solo ranking.

        Es la opcion por defecto razonable cuando no sabes si la consulta encaja mejor con
        terminos exactos o con significado. Cada resultado indica por que aparece
        ('matched_by') y en que posicion quedo en cada ranking.
        """
        ruta = ajustes.resolve_module_path(module_path)
        resultados = buscar_hibrida(
            repo,
            query,
            proveedor_de_embeddings(),
            module_path=ruta,
            filtros_atributos=filters,
            limit=limit,
            peso_lexical=lexical_weight,
            peso_vectorial=semantic_weight,
        )
        return responder(
            ruta, {"query": query, "records": [r.to_dict() for r in resultados]}
        )

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_get_requirement(absolute_number: int, module_path: str | None = None) -> str:
        """Obtiene un requisito completo de la copia local, con todos sus atributos."""
        ruta = ajustes.resolve_module_path(module_path)
        registro = repo.get_requirement(ruta, absolute_number)
        if registro is None:
            return responder(
                ruta,
                {
                    "absolute_number": absolute_number,
                    "record": None,
                    "message": (
                        "No esta en la copia local. Puede que no exista, que este borrado "
                        "o que la copia no incluya este modulo."
                    ),
                },
            )
        return responder(ruta, {"record": registro})

    @servidor.tool(annotations=SOLO_LECTURA)
    @con_errores
    def kb_list_requirements(
        module_path: str | None = None, cursor: int | None = None, limit: int = 50
    ) -> str:
        """Recorre los requisitos de un modulo desde la copia local.

        Avanza con 'cursor', igual que el servidor directo: pasa el ultimo
        'absolute_number' recibido para continuar.
        """
        ruta = ajustes.resolve_module_path(module_path)
        registros = repo.list_requirements(ruta, limit=limit, cursor=cursor)
        siguiente = registros[-1]["absolute_number"] if len(registros) == limit else None
        return responder(ruta, {"records": registros, "next_cursor": siguiente})

    return servidor


def main() -> None:
    """Punto de entrada del servidor local sobre stdio."""
    configurar_logging()
    logger.info("Iniciando servidor MCP 'doors-kb' (copia local, solo lectura) sobre stdio")
    crear_servidor().run(transport="stdio")


if __name__ == "__main__":
    main()
