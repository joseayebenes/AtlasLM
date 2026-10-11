"""Modelo SQLite de la copia local de requisitos de DOORS.

Este es el primero de los dos archivos del script de descarga: define **donde** se guardan
los requisitos y con que reglas. El segundo (``descargar_requisitos.py``) define **como** se
traen desde DOORS. Ninguno de los dos importa el paquete ``doors_kb``: son autonomos y se
pueden copiar a la maquina Windows donde este DOORS sin llevarse el resto del proyecto.

Decisiones que conviene tener presentes al leer el esquema:

* La identidad logica de un requisito es ``(module_path, absolute_number)``. El Absolute
  Number por si solo no vale: se repite entre modulos distintos.
* Los atributos del proyecto se guardan normalizados en su propia tabla, de modo que anadir
  un atributo en DOORS no obliga a migrar el esquema SQL.
* Los borrados son **logicos** (``is_deleted``), nunca fisicos: un objeto que desaparece de
  DOORS puede reaparecer, y su historia local sigue siendo util.
* Cada requisito guarda un hash SHA-256 de su contenido. Es lo que permite que la segunda
  descarga y las siguientes sean incrementales en lugar de reescribir el modulo entero.

DOORS es siempre la fuente de verdad: esta base es un derivado reconstruible y nunca se
escribe nada de vuelta.

Ejecutado directamente, imprime un resumen de la base:

    python scripts/modelo_sqlite.py doors.sqlite3
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Nombres de los atributos de DOORS que ademas se copian a columnas propias de
# ``requirements``. Estan duplicados a proposito: siguen tambien en la tabla de atributos,
# pero tenerlos como columna hace legible cualquier consulta SQL a mano.
ATRIBUTO_HEADING = "Object Heading"
ATRIBUTO_TEXT = "Object Text"

ESQUEMA = """
PRAGMA foreign_keys = ON;

-- Modulos descargados y su frescura.
CREATE TABLE IF NOT EXISTS modules (
    module_path        TEXT PRIMARY KEY,
    last_sync_at       TEXT,
    last_full_sync_at  TEXT
);

-- Datos principales de cada objeto de DOORS y su hash de contenido.
CREATE TABLE IF NOT EXISTS requirements (
    id                    INTEGER PRIMARY KEY,
    module_path           TEXT    NOT NULL REFERENCES modules(module_path) ON DELETE CASCADE,
    absolute_number       INTEGER NOT NULL,
    identifier            TEXT    NOT NULL DEFAULT '',
    outline_number        TEXT    NOT NULL DEFAULT '',
    heading               TEXT    NOT NULL DEFAULT '',
    text                  TEXT    NOT NULL DEFAULT '',
    content_hash          TEXT    NOT NULL,
    is_deleted            INTEGER NOT NULL DEFAULT 0,
    first_seen_at         TEXT    NOT NULL,
    last_seen_at          TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    deleted_at            TEXT,
    UNIQUE (module_path, absolute_number)
);

CREATE INDEX IF NOT EXISTS idx_requirements_modulo_activo
    ON requirements (module_path, is_deleted);

-- Atributos arbitrarios del proyecto, sin una columna SQL por atributo.
CREATE TABLE IF NOT EXISTS requirement_attributes (
    requirement_id  INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    value_text      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (requirement_id, name)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_attributes_nombre
    ON requirement_attributes (name);

-- Auditoria de cada descarga: es lo que permite explicar por que la copia local esta como
-- esta, y distinguir una descarga completa de una que se interrumpio a mitad.
CREATE TABLE IF NOT EXISTS sync_runs (
    id                 INTEGER PRIMARY KEY,
    module_path        TEXT    NOT NULL,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    status             TEXT    NOT NULL,  -- running | success | failed
    pages              INTEGER NOT NULL DEFAULT 0,
    requirements_seen  INTEGER NOT NULL DEFAULT 0,
    inserted           INTEGER NOT NULL DEFAULT 0,
    updated            INTEGER NOT NULL DEFAULT 0,
    unchanged          INTEGER NOT NULL DEFAULT 0,
    deleted            INTEGER NOT NULL DEFAULT 0,
    completed_module   INTEGER NOT NULL DEFAULT 0,
    error              TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_modulo
    ON sync_runs (module_path, started_at DESC);
"""


def ahora() -> str:
    """Marca de tiempo UTC en ISO-8601, que es como se guardan todas las fechas."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalizar(texto: str) -> str:
    """Normaliza saltos de linea antes de que un texto entre en el hash.

    DOORS devuelve CRLF y, segun la version del cliente, a veces CR sueltos. Esa diferencia
    no es un cambio de requisito: sin normalizar, el mismo objeto se clasificaria como
    ``updated`` en cada descarga y el trabajo incremental no serviria de nada.
    """
    return texto.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Requisito:
    """Un objeto de DOORS tal y como se descarga.

    ``attributes`` lleva todos los atributos pedidos al modulo; ``heading`` y ``text`` son
    solo un atajo de lectura hacia dos de ellos.
    """

    module_path: str
    absolute_number: int
    identifier: str = ""
    outline_number: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def heading(self) -> str:
        return self.attributes.get(ATRIBUTO_HEADING, "")

    @property
    def text(self) -> str:
        return self.attributes.get(ATRIBUTO_TEXT, "")

    def payload_canonico(self) -> str:
        """Representacion canonica de los campos que definen el contenido.

        Deliberadamente **no** incluye ``module_path`` ni ``absolute_number``: esos son la
        identidad, no el contenido. ``sort_keys`` garantiza que el hash no dependa del orden
        en que DXL devuelva los atributos, que no esta garantizado.
        """
        return json.dumps(
            {
                "identifier": _normalizar(self.identifier),
                "outline_number": _normalizar(self.outline_number),
                "attributes": {k: _normalizar(v) for k, v in self.attributes.items()},
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def hash_contenido(self) -> str:
        """SHA-256 del contenido: la base de la deteccion incremental."""
        return hashlib.sha256(self.payload_canonico().encode("utf-8")).hexdigest()


@dataclass
class Estadisticas:
    """Contadores de una descarga, que acaban en la tabla ``sync_runs``."""

    module_path: str = ""
    pages: int = 0
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    completed_module: bool = False
    error: str | None = None

    def registrar(self, cambio: str) -> None:
        self.seen += 1
        if cambio == "inserted":
            self.inserted += 1
        elif cambio == "updated":
            self.updated += 1
        else:
            self.unchanged += 1

    def resumen(self) -> str:
        estado = "completo" if self.completed_module else "PARCIAL"
        return (
            f"{self.module_path}: {self.seen} requisitos ({estado}), "
            f"{self.inserted} nuevos, {self.updated} modificados, "
            f"{self.unchanged} sin cambios, {self.deleted} marcados como borrados"
        )


class BaseLocal:
    """Copia local en SQLite. Es la unica clase que escribe en la base.

    Se usa como gestor de contexto:

        with BaseLocal("doors.sqlite3") as base:
            base.guardar(requisito)
    """

    def __init__(self, ruta: str) -> None:
        self.ruta = ruta
        self.conexion = sqlite3.connect(ruta)
        self.conexion.row_factory = sqlite3.Row
        # WAL permite leer la base (por ejemplo, para comprobar el avance) mientras la
        # descarga sigue escribiendo.
        self.conexion.execute("PRAGMA journal_mode = WAL")
        self.conexion.execute("PRAGMA foreign_keys = ON")
        self.conexion.executescript(ESQUEMA)
        self.conexion.commit()

    def __enter__(self) -> BaseLocal:
        return self

    def __exit__(self, *_excepcion: object) -> None:
        self.cerrar()

    def cerrar(self) -> None:
        self.conexion.close()

    @contextmanager
    def transaccion(self) -> Iterator[sqlite3.Connection]:
        """Agrupa las escrituras de una pagina en una sola transaccion.

        Una pagina por transaccion es el equilibrio buscado: si la descarga se interrumpe,
        lo ya confirmado sigue siendo coherente y no hay que empezar de cero.
        """
        try:
            yield self.conexion
            self.conexion.commit()
        except BaseException:
            self.conexion.rollback()
            raise

    # -----------------------------------------------------------------------------------
    # Escritura
    # -----------------------------------------------------------------------------------

    def registrar_modulo(self, module_path: str) -> None:
        self.conexion.execute(
            "INSERT OR IGNORE INTO modules (module_path) VALUES (?)", (module_path,)
        )

    def guardar(self, requisito: Requisito) -> str:
        """Inserta o actualiza un requisito y devuelve 'inserted', 'updated' o 'unchanged'.

        Un requisito que reaparece despues de haber sido marcado como borrado se reactiva y
        cuenta como ``updated``: su contenido puede no haber cambiado, pero su estado si.
        """
        cursor = self.conexion.execute(
            "SELECT id, content_hash, is_deleted FROM requirements "
            "WHERE module_path = ? AND absolute_number = ?",
            (requisito.module_path, requisito.absolute_number),
        )
        fila = cursor.fetchone()
        hash_nuevo = requisito.hash_contenido()
        momento = ahora()

        if fila is None:
            cursor = self.conexion.execute(
                "INSERT INTO requirements ("
                "  module_path, absolute_number, identifier, outline_number, heading, text,"
                "  content_hash, is_deleted, first_seen_at, last_seen_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    requisito.module_path,
                    requisito.absolute_number,
                    requisito.identifier,
                    requisito.outline_number,
                    requisito.heading,
                    requisito.text,
                    hash_nuevo,
                    momento,
                    momento,
                    momento,
                ),
            )
            self._guardar_atributos(int(cursor.lastrowid or 0), requisito.attributes)
            return "inserted"

        if fila["content_hash"] == hash_nuevo and not fila["is_deleted"]:
            # Sin cambios: solo se anota que se ha vuelto a ver. No se tocan los atributos,
            # que es de donde sale casi toda la ganancia de la descarga incremental.
            self.conexion.execute(
                "UPDATE requirements SET last_seen_at = ? WHERE id = ?",
                (momento, fila["id"]),
            )
            return "unchanged"

        self.conexion.execute(
            "UPDATE requirements SET identifier = ?, outline_number = ?, heading = ?, "
            "text = ?, content_hash = ?, is_deleted = 0, deleted_at = NULL, "
            "last_seen_at = ?, updated_at = ? WHERE id = ?",
            (
                requisito.identifier,
                requisito.outline_number,
                requisito.heading,
                requisito.text,
                hash_nuevo,
                momento,
                momento,
                fila["id"],
            ),
        )
        self._guardar_atributos(int(fila["id"]), requisito.attributes)
        return "updated"

    def _guardar_atributos(self, requirement_id: int, atributos: dict[str, str]) -> None:
        """Reescribe los atributos del requisito.

        Se borran y se vuelven a insertar en lugar de hacer un UPSERT por atributo: asi un
        atributo que desaparece del modulo tambien desaparece de la copia local.
        """
        self.conexion.execute(
            "DELETE FROM requirement_attributes WHERE requirement_id = ?", (requirement_id,)
        )
        self.conexion.executemany(
            "INSERT INTO requirement_attributes (requirement_id, name, value_text) "
            "VALUES (?, ?, ?)",
            [(requirement_id, nombre, valor) for nombre, valor in atributos.items()],
        )

    def marcar_ausentes(self, module_path: str, vistos: Iterable[int]) -> int:
        """Marca como borrados los requisitos que ya no estan en DOORS.

        **Solo debe llamarse cuando el modulo se ha recorrido entero.** Una descarga
        interrumpida ha visto una parte del modulo, y marcar "lo que no aparecio" borraria
        logicamente requisitos que si existen. Quien llama es responsable de comprobarlo;
        por eso el parametro se llama ``vistos`` y no admite una lista parcial por error.
        """
        presentes = sorted(set(vistos))
        marcador = ",".join("?" * len(presentes))
        condicion = f"AND absolute_number NOT IN ({marcador})" if presentes else ""
        momento = ahora()
        cursor = self.conexion.execute(
            f"UPDATE requirements SET is_deleted = 1, deleted_at = ?, updated_at = ? "
            f"WHERE module_path = ? AND is_deleted = 0 {condicion}",
            (momento, momento, module_path, *presentes),
        )
        return cursor.rowcount

    def anotar_frescura(self, module_path: str, *, completo: bool) -> None:
        """Actualiza la fecha de ultima descarga del modulo."""
        momento = ahora()
        if completo:
            self.conexion.execute(
                "UPDATE modules SET last_sync_at = ?, last_full_sync_at = ? "
                "WHERE module_path = ?",
                (momento, momento, module_path),
            )
        else:
            self.conexion.execute(
                "UPDATE modules SET last_sync_at = ? WHERE module_path = ?",
                (momento, module_path),
            )

    # -----------------------------------------------------------------------------------
    # Historial
    # -----------------------------------------------------------------------------------

    def iniciar_run(self, module_path: str) -> int:
        cursor = self.conexion.execute(
            "INSERT INTO sync_runs (module_path, started_at, status) VALUES (?, ?, 'running')",
            (module_path, ahora()),
        )
        self.conexion.commit()
        return int(cursor.lastrowid or 0)

    def cerrar_run(self, run_id: int, stats: Estadisticas) -> None:
        """Cierra el historial, tanto si la descarga acabo bien como si fallo."""
        self.conexion.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, pages = ?, "
            "requirements_seen = ?, inserted = ?, updated = ?, unchanged = ?, deleted = ?, "
            "completed_module = ?, error = ? WHERE id = ?",
            (
                ahora(),
                "failed" if stats.error else "success",
                stats.pages,
                stats.seen,
                stats.inserted,
                stats.updated,
                stats.unchanged,
                stats.deleted,
                1 if stats.completed_module else 0,
                stats.error,
                run_id,
            ),
        )
        self.conexion.commit()

    # -----------------------------------------------------------------------------------
    # Lectura
    # -----------------------------------------------------------------------------------

    def resumen(self) -> list[sqlite3.Row]:
        """Una fila por modulo con sus contadores y su frescura."""
        return list(
            self.conexion.execute(
                "SELECT m.module_path, m.last_sync_at, m.last_full_sync_at,"
                "  COUNT(r.id) FILTER (WHERE r.is_deleted = 0) AS vivos,"
                "  COUNT(r.id) FILTER (WHERE r.is_deleted = 1) AS borrados "
                "FROM modules m LEFT JOIN requirements r ON r.module_path = m.module_path "
                "GROUP BY m.module_path ORDER BY m.module_path"
            )
        )


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Resumen de la copia local de requisitos.")
    parser.add_argument("base", nargs="?", default="doors.sqlite3", help="ruta del fichero SQLite")
    argumentos = parser.parse_args()

    with BaseLocal(argumentos.base) as base:
        filas = base.resumen()
        if not filas:
            print(f"{argumentos.base}: sin modulos descargados todavia.")
            return 0
        print(f"{argumentos.base}:")
        for fila in filas:
            print(
                f"  {fila['module_path']}: {fila['vivos']} requisitos vivos, "
                f"{fila['borrados']} borrados; ultima descarga completa: "
                f"{fila['last_full_sync_at'] or 'nunca'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
