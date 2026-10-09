"""Comando ``doors-sync``: sincroniza un modulo de DOORS hacia la copia local.

Es la forma habitual de ejecutar el hito H3. Con ``--source fake`` hace el mismo recorrido
contra la fuente simulada, lo que permite comprobar la instalacion y el esquema de la base
sin tener DOORS delante.

Ejemplos::

    doors-sync --module "/Proyecto/Requisitos/Requisitos del sistema" --page-size 25
    doors-sync --source fake --module "/Demo/Reqs" --db /tmp/demo.sqlite3
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import Settings
from ..db import SqliteRepository
from ..errors import DoorsKbError
from ..models import AttributeDefinition
from ..sources.base import RequirementsSource
from ..sources.fake import FakeDoorsSource
from ..sync import SyncService

logger = logging.getLogger("doors_kb.cli.sync")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doors-sync",
        description="Sincroniza un modulo de IBM DOORS Classic hacia la copia local SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "La configuracion se toma de las variables de entorno (ver README) y estos\n"
            "argumentos la sobreescriben para una ejecucion concreta."
        ),
    )
    parser.add_argument("--module", help="Ruta completa del modulo (fullName) en DOORS.")
    parser.add_argument("--db", help="Ruta del fichero SQLite de la copia local.")
    parser.add_argument(
        "--attributes",
        help=(
            "Atributos a sincronizar, separados por comas. Definen tambien el hash de "
            "contenido, asi que conviene fijarlos y no cambiarlos a la ligera (riesgo R-005)."
        ),
    )
    parser.add_argument("--page-size", type=int, help="Objetos por pagina DXL.")
    parser.add_argument(
        "--max-attribute-chars", type=int, help="Limite de caracteres por atributo."
    )
    parser.add_argument(
        "--source",
        choices=("doors", "fake"),
        default="doors",
        help="Origen de datos. 'fake' usa un modulo simulado y no necesita DOORS.",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help=(
            "Reconstruye el indice lexical desde la copia local y termina, sin consultar "
            "DOORS. Los indices son derivados reconstruibles (RNF-016)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Muestra el detalle de cada pagina."
    )
    return parser


def _ajustes_desde(argumentos: argparse.Namespace) -> Settings:
    """Combina el entorno con los argumentos de esta ejecucion."""
    base = Settings.from_env()
    cambios: dict[str, object] = {}
    if argumentos.module:
        cambios["module_path"] = argumentos.module
    if argumentos.db:
        cambios["db_path"] = argumentos.db
    if argumentos.attributes:
        cambios["sync_attributes"] = tuple(
            parte.strip() for parte in argumentos.attributes.split(",") if parte.strip()
        )
    if argumentos.page_size:
        cambios["sync_page_size"] = argumentos.page_size
    if argumentos.max_attribute_chars:
        cambios["max_attribute_chars"] = argumentos.max_attribute_chars
    return Settings(**{**base.__dict__, **cambios})


def _fuente_simulada(ajustes: Settings) -> FakeDoorsSource:
    """Modulo de ejemplo para ``--source fake``.

    Su esquema de atributos es **fijo** a proposito: si definiera sobre la marcha cualquier
    atributo que se le pidiera, un nombre mal escrito pasaria la validacion y el modo de
    prueba dejaria de comportarse como DOORS justo en el caso que interesa detectar
    (RF-012, CA-007).
    """
    origen = FakeDoorsSource(module_path=ajustes.module_path)
    for nombre, tipo in (("Estado", "Enumeration"), ("Criticidad", "String")):
        origen.definir_atributo(AttributeDefinition(nombre, tipo))
    for numero in range(1, 26):
        origen.anadir(
            numero,
            heading=f"Requisito de ejemplo {numero}",
            text=f"El sistema debera cumplir la condicion numero {numero}.",
            attributes={"Estado": "Aprobado", "Criticidad": "media"},
        )
    return origen


def _crear_fuente(ajustes: Settings, tipo: str) -> RequirementsSource:
    """Crea la fuente y, si es DOORS, abre la sesion Automation.

    La sesion se abre aqui y no dentro del servicio: iniciar sesion es una accion del
    usuario (hay que autenticarse a mano), no un detalle de la sincronizacion (RF-003).
    """
    if tipo == "fake":
        return _fuente_simulada(ajustes)

    from ..sources.doors.client import DoorsComClient

    cliente = DoorsComClient(ajustes)
    print(
        "Abriendo la sesion Automation de DOORS. Si aparece la ventana de inicio de "
        "sesion, autenticate en ELLA (no en una ventana anterior).",
        file=sys.stderr,
    )
    cliente.start_session()
    return cliente


def main(argv: list[str] | None = None) -> int:
    argumentos = construir_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if argumentos.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        ajustes = _ajustes_desde(argumentos)
        modulo = ajustes.resolve_module_path()

        if argumentos.rebuild_index:
            # No se abre ninguna sesion de DOORS: el indice se rehace desde la copia local.
            with SqliteRepository(ajustes.db_path) as repositorio:
                indexados = repositorio.rebuild_fts_index(modulo)
            print(json.dumps({"module_path": modulo, "indexed": indexados}, indent=2))
            return 0

        fuente = _crear_fuente(ajustes, argumentos.source)

        with SqliteRepository(ajustes.db_path) as repositorio:
            stats = SyncService(fuente, repositorio, ajustes).sync_module(modulo)

        print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return 0

    except DoorsKbError as exc:
        # Los errores previstos se muestran como mensaje, no como traza: quien ejecuta esto
        # necesita saber que hacer, no donde se lanzo la excepcion.
        print(f"ERROR: {exc}", file=sys.stderr)
        estadisticas = getattr(exc, "stats", None)
        if estadisticas is not None:
            print(json.dumps(estadisticas.to_dict(), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
