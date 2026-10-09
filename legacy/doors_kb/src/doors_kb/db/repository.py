"""Repositorio SQLite de la copia local de requisitos.

Cubre RF-050 a RF-064 y RNF-012. Es la unica capa que sabe de SQL: el servicio de
sincronizacion le pide operaciones de dominio ("haz upsert de este requisito", "marca los
ausentes"), no le pasa consultas.

Dos reglas de esta capa merecen atencion porque son sutiles y silenciosas si se rompen:

1. **Los borrados son logicos.** ``mark_missing_as_deleted`` pone ``is_deleted = 1``; nunca
   borra filas. Un requisito puede reaparecer, y su historia local sigue siendo util.
2. **Reactivar es un cambio.** Un requisito marcado como borrado que vuelve con el *mismo*
   hash se clasifica como ``updated``, no como ``unchanged`` (RF-056). Es el caso que un
   ``WHERE content_hash != ?`` ingenuo pierde, dejando el registro invisible para siempre.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..models import ChangeType, RequirementRecord, SyncStats

_ESQUEMA = Path(__file__).with_name("schema.sql")


def _ahora() -> str:
    """Marca temporal UTC en ISO 8601.

    En UTC a proposito: la maquina de DOORS y la que consulta la copia local pueden estar
    en husos distintos, y una marca sin huso no es comparable.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


class _Resultado:
    """Resultado ya materializado de una consulta.

    Las filas se leen dentro del cerrojo, no despues: devolver el cursor de sqlite3 dejaria
    la lectura fuera de la seccion critica y otro hilo podria intercalar una escritura entre
    la consulta y su recorrido.
    """

    __slots__ = ("_filas", "lastrowid", "rowcount")

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        try:
            self._filas = cursor.fetchall()
        except sqlite3.ProgrammingError:
            # Las sentencias que no devuelven filas (DDL, algunos PRAGMA) no son leibles.
            self._filas = []
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    def fetchall(self) -> list[sqlite3.Row]:
        return self._filas

    def fetchone(self) -> sqlite3.Row | None:
        return self._filas[0] if self._filas else None


class _ConexionSerializada:
    """Envoltorio de la conexion que serializa el acceso entre hilos.

    Hace falta porque el servidor MCP local ejecuta cada tool en un hilo de trabajo del
    SDK, mientras que una conexion de sqlite3 esta atada por defecto al hilo que la creo.
    Las alternativas serian una conexion por hilo -que con una base ``:memory:`` daria una
    base distinta a cada hilo- o abrir y cerrar en cada consulta, mas lento y sin ganar
    nada: las consultas de este proyecto son cortas y un cerrojo reentrante basta.

    El cerrojo es reentrante para que ``transaction()`` pueda retenerlo durante toda la
    transaccion sin bloquearse a si mismo, que es lo que impide que otro hilo lea la base a
    medio escribir.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, parametros: Sequence[object] = ()) -> _Resultado:
        with self._lock:
            return _Resultado(self._conn.execute(sql, parametros))

    def executemany(self, sql: str, secuencia) -> _Resultado:
        with self._lock:
            return _Resultado(self._conn.executemany(sql, secuencia))

    def executescript(self, sql: str) -> _Resultado:
        with self._lock:
            return _Resultado(self._conn.executescript(sql))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteRepository:
    """Acceso a la copia local. Se puede usar como gestor de contexto.

    Es seguro de usar desde varios hilos: el acceso a SQLite se serializa internamente
    (ver :class:`_ConexionSerializada`).
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: _ConexionSerializada | None = None
        self._lock = threading.RLock()

    # -----------------------------------------------------------------------------------
    # Ciclo de vida
    # -----------------------------------------------------------------------------------

    def connect(self) -> _ConexionSerializada:
        """Abre la conexion, crea el esquema si hace falta y fija los PRAGMA (RNF-012)."""
        if self._conn is not None:
            return self._conn

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None desactiva el manejo implicito de transacciones de sqlite3:
        # las abrimos nosotros por pagina, que es lo que exige la sincronizacion (RNF-012).
        # check_same_thread=False permite usar la conexion desde los hilos de trabajo del
        # servidor MCP; la exclusion mutua la pone _ConexionSerializada.
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL permite leer la copia local (por ejemplo desde el MCP) mientras una
        # sincronizacion escribe, sin bloqueos mutuos.
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_ESQUEMA.read_text(encoding="utf-8"))
        self._conn = _ConexionSerializada(conn, self._lock)
        return self._conn

    @property
    def conn(self) -> _ConexionSerializada:
        return self.connect()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SqliteRepository:
        self.connect()
        return self

    def __exit__(self, *_excepcion) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[_ConexionSerializada]:
        """Agrupa escrituras en una transaccion.

        El sincronizador envuelve **cada pagina** en una de estas: si el recorrido se corta,
        las paginas ya confirmadas se conservan y la pagina a medias se deshace entera.

        El cerrojo se retiene durante toda la transaccion: sin eso, otro hilo podria leer la
        base a medio escribir o intercalar su propio BEGIN.
        """
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # -----------------------------------------------------------------------------------
    # Modulos
    # -----------------------------------------------------------------------------------

    def ensure_module(self, module_path: str) -> None:
        """Registra el modulo si es la primera vez que se sincroniza."""
        self.conn.execute(
            "INSERT INTO modules (module_path) VALUES (?) ON CONFLICT(module_path) DO NOTHING",
            (module_path,),
        )

    def touch_module(self, module_path: str, *, full: bool) -> None:
        """Actualiza la frescura del modulo tras una sincronizacion correcta.

        ``full`` distingue un recorrido completo del modulo de una sincronizacion parcial:
        solo el primero garantiza que la copia local refleja el modulo entero, y es el dato
        que el agente necesita para saber si puede fiarse de la copia (riesgo R-007).
        """
        momento = _ahora()
        self.ensure_module(module_path)
        if full:
            self.conn.execute(
                "UPDATE modules SET last_sync_at = ?, last_full_sync_at = ? WHERE module_path = ?",
                (momento, momento, module_path),
            )
        else:
            self.conn.execute(
                "UPDATE modules SET last_sync_at = ? WHERE module_path = ?",
                (momento, module_path),
            )

    def module_freshness(self, module_path: str) -> dict[str, object] | None:
        """Devuelve cuando se sincronizo el modulo por ultima vez y cuantos objetos tiene."""
        fila = self.conn.execute(
            "SELECT module_path, last_sync_at, last_full_sync_at FROM modules"
            " WHERE module_path = ?",
            (module_path,),
        ).fetchone()
        if fila is None:
            return None
        activos = self.conn.execute(
            "SELECT COUNT(*) FROM requirements WHERE module_path = ? AND is_deleted = 0",
            (module_path,),
        ).fetchone()[0]
        borrados = self.conn.execute(
            "SELECT COUNT(*) FROM requirements WHERE module_path = ? AND is_deleted = 1",
            (module_path,),
        ).fetchone()[0]
        return {
            "module_path": fila["module_path"],
            "last_sync_at": fila["last_sync_at"],
            "last_full_sync_at": fila["last_full_sync_at"],
            "active_requirements": activos,
            "deleted_requirements": borrados,
        }

    # -----------------------------------------------------------------------------------
    # Requisitos
    # -----------------------------------------------------------------------------------

    def upsert_requirement(self, record: RequirementRecord) -> ChangeType:
        """Inserta o actualiza un requisito y devuelve como ha cambiado (RF-055).

        La clasificacion es la senal que, en el hito H5, decidira que embeddings hay que
        regenerar: solo ``inserted`` y ``updated`` (RF-074).
        """
        conn = self.conn
        self.ensure_module(record.module_path)
        nuevo_hash = record.content_hash()
        momento = _ahora()

        fila = conn.execute(
            "SELECT id, content_hash, is_deleted FROM requirements "
            "WHERE module_path = ? AND absolute_number = ?",
            (record.module_path, record.absolute_number),
        ).fetchone()

        if fila is None:
            cursor = conn.execute(
                "INSERT INTO requirements ("
                " module_path, absolute_number, identifier, outline_number, heading, text,"
                " content_hash, is_deleted, source_last_modified,"
                " first_seen_at, last_seen_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    record.module_path,
                    record.absolute_number,
                    record.identifier,
                    record.outline_number,
                    record.heading,
                    record.text,
                    nuevo_hash,
                    record.source_last_modified,
                    momento,
                    momento,
                    momento,
                ),
            )
            self._guardar_atributos(int(cursor.lastrowid), record)
            self._indexar(record)
            return ChangeType.INSERTED

        requirement_id = int(fila["id"])
        estaba_borrado = bool(fila["is_deleted"])
        contenido_igual = fila["content_hash"] == nuevo_hash

        if contenido_igual and not estaba_borrado:
            # Nada que escribir salvo la constancia de que lo hemos vuelto a ver y, si la
            # hay, la fecha de origen: es metadato fuera del hash (RF-063), asi que puede
            # refrescarse sin que el requisito cuente como modificado.
            conn.execute(
                "UPDATE requirements SET last_seen_at = ?, source_last_modified = ? WHERE id = ?",
                (momento, record.source_last_modified, requirement_id),
            )
            return ChangeType.UNCHANGED

        # Reaparicion con contenido identico: el hash coincide, pero el registro estaba
        # marcado como borrado y hay que reactivarlo. Clasificarlo como 'unchanged' lo
        # dejaria invisible en todas las busquedas locales (RF-056).
        conn.execute(
            "UPDATE requirements SET"
            " identifier = ?, outline_number = ?, heading = ?, text = ?, content_hash = ?,"
            " is_deleted = 0, deleted_at = NULL, source_last_modified = ?,"
            " last_seen_at = ?, updated_at = ?"
            " WHERE id = ?",
            (
                record.identifier,
                record.outline_number,
                record.heading,
                record.text,
                nuevo_hash,
                record.source_last_modified,
                momento,
                momento,
                requirement_id,
            ),
        )
        self._guardar_atributos(requirement_id, record)
        self._indexar(record)
        return ChangeType.UPDATED

    def _guardar_atributos(self, requirement_id: int, record: RequirementRecord) -> None:
        """Reescribe los atributos del requisito.

        Se borran y se vuelven a insertar en lugar de hacer un diff: un atributo que
        desaparece del perfil de sincronizacion tiene que desaparecer tambien de la copia,
        y el volumen por requisito es de unas pocas filas.
        """
        conn = self.conn
        conn.execute(
            "DELETE FROM requirement_attributes WHERE requirement_id = ?", (requirement_id,)
        )
        if record.attributes:
            conn.executemany(
                "INSERT INTO requirement_attributes (requirement_id, name, value_text) "
                "VALUES (?, ?, ?)",
                [(requirement_id, nombre, valor) for nombre, valor in record.attributes.items()],
            )

    # -----------------------------------------------------------------------------------
    # Indice lexical FTS5 (RF-070)
    # -----------------------------------------------------------------------------------

    def _indexar(self, record: RequirementRecord) -> None:
        """Refresca la entrada del requisito en el indice lexical.

        Se llama solo desde los caminos ``inserted`` y ``updated`` de ``upsert_requirement``:
        la clasificacion por hash que ya hace el repositorio evita reindexar lo que no ha
        cambiado, sin necesidad de una pasada extra sobre la base.
        """
        self._desindexar(record.module_path, record.absolute_number)
        self.conn.execute(
            "INSERT INTO requirements_fts"
            " (module_path, absolute_number, identifier, heading, text, attributes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.module_path,
                record.absolute_number,
                record.identifier,
                record.heading,
                record.text,
                # Los atributos se indexan como un unico campo "nombre: valor" para poder
                # buscarlos sin una columna FTS por atributo, igual que la tabla
                # requirement_attributes evita una columna SQL por atributo (RF-053).
                " ".join(f"{n}: {v}" for n, v in sorted(record.attributes.items())),
            ),
        )

    def _desindexar(self, module_path: str, absolute_number: int) -> None:
        """Retira un requisito del indice lexical."""
        self.conn.execute(
            "DELETE FROM requirements_fts WHERE module_path = ? AND absolute_number = ?",
            (module_path, absolute_number),
        )

    def rebuild_fts_index(self, module_path: str | None = None) -> int:
        """Reconstruye el indice lexical desde la copia local (RNF-016).

        La base local y sus indices son derivados reconstruibles (RF-080): si el indice se
        corrompe o cambia el tokenizador, se rehace sin volver a consultar DOORS. Devuelve
        el numero de requisitos indexados.
        """
        conn = self.conn
        if module_path is None:
            conn.execute("DELETE FROM requirements_fts")
            filas = conn.execute(
                "SELECT * FROM requirements WHERE is_deleted = 0"
            ).fetchall()
        else:
            conn.execute("DELETE FROM requirements_fts WHERE module_path = ?", (module_path,))
            filas = conn.execute(
                "SELECT * FROM requirements WHERE module_path = ? AND is_deleted = 0",
                (module_path,),
            ).fetchall()

        for fila in filas:
            datos = self._fila_a_dict(fila)
            conn.execute(
                "INSERT INTO requirements_fts"
                " (module_path, absolute_number, identifier, heading, text, attributes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datos["module_path"],
                    datos["absolute_number"],
                    datos["identifier"],
                    datos["heading"],
                    datos["text"],
                    " ".join(f"{n}: {v}" for n, v in sorted(datos["attributes"].items())),
                ),
            )
        return len(filas)

    def count_indexed(self, module_path: str | None = None) -> int:
        """Numero de requisitos presentes en el indice lexical."""
        if module_path is None:
            consulta, parametros = "SELECT COUNT(*) FROM requirements_fts", ()
        else:
            consulta = "SELECT COUNT(*) FROM requirements_fts WHERE module_path = ?"
            parametros = (module_path,)
        return int(self.conn.execute(consulta, parametros).fetchone()[0])

    def mark_missing_as_deleted(self, module_path: str, seen: Iterable[int]) -> int:
        """Marca como borrados los requisitos que no aparecieron en el recorrido (RF-061).

        **Solo debe llamarse tras recorrer el modulo entero.** Si se llamara tras una
        sincronizacion interrumpida, marcaria como eliminados requisitos que simplemente no
        dio tiempo a visitar. Quien impone esa condicion es ``SyncService``; aqui se
        documenta para que nadie la llame por error desde otro sitio.
        """
        vistos = set(seen)
        conn = self.conn
        pendientes = conn.execute(
            "SELECT absolute_number FROM requirements WHERE module_path = ? AND is_deleted = 0",
            (module_path,),
        ).fetchall()
        desaparecidos = [
            f["absolute_number"] for f in pendientes if f["absolute_number"] not in vistos
        ]
        if not desaparecidos:
            return 0
        momento = _ahora()
        conn.executemany(
            "UPDATE requirements SET is_deleted = 1, deleted_at = ?, updated_at = ? "
            "WHERE module_path = ? AND absolute_number = ?",
            [(momento, momento, module_path, numero) for numero in desaparecidos],
        )
        # Un requisito borrado sale del indice: seguir devolviendolo en las busquedas
        # locales seria peor que no tenerlo indexado.
        for numero in desaparecidos:
            self._desindexar(module_path, numero)
        return len(desaparecidos)

    def get_requirement(
        self, module_path: str, absolute_number: int, *, include_deleted: bool = False
    ) -> dict[str, object] | None:
        """Lee un requisito de la copia local con sus atributos."""
        condicion = "" if include_deleted else " AND is_deleted = 0"
        fila = self.conn.execute(
            f"SELECT * FROM requirements WHERE module_path = ? AND absolute_number = ?{condicion}",
            (module_path, absolute_number),
        ).fetchone()
        return None if fila is None else self._fila_a_dict(fila)

    def list_requirements(
        self, module_path: str, *, limit: int = 50, cursor: int | None = None
    ) -> list[dict[str, object]]:
        """Lista requisitos vivos del modulo, con el mismo modelo de cursor que la fuente."""
        filas = self.conn.execute(
            "SELECT * FROM requirements"
            " WHERE module_path = ? AND is_deleted = 0 AND absolute_number > ?"
            " ORDER BY absolute_number LIMIT ?",
            (module_path, cursor if cursor is not None else -1, limit),
        ).fetchall()
        return [self._fila_a_dict(f) for f in filas]

    def count_requirements(self, module_path: str, *, include_deleted: bool = False) -> int:
        condicion = "" if include_deleted else " AND is_deleted = 0"
        return int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM requirements WHERE module_path = ?{condicion}",
                (module_path,),
            ).fetchone()[0]
        )

    def _fila_a_dict(self, fila: sqlite3.Row) -> dict[str, object]:
        atributos = self.conn.execute(
            "SELECT name, value_text FROM requirement_attributes WHERE requirement_id = ?",
            (fila["id"],),
        ).fetchall()
        return {
            "module_path": fila["module_path"],
            "absolute_number": fila["absolute_number"],
            "identifier": fila["identifier"],
            "outline_number": fila["outline_number"],
            "heading": fila["heading"],
            "text": fila["text"],
            "attributes": {a["name"]: a["value_text"] for a in atributos},
            "content_hash": fila["content_hash"],
            "is_deleted": bool(fila["is_deleted"]),
            "source_last_modified": fila["source_last_modified"],
            "last_seen_at": fila["last_seen_at"],
            "updated_at": fila["updated_at"],
        }

    # -----------------------------------------------------------------------------------
    # Embeddings (RF-071 a RF-074)
    # -----------------------------------------------------------------------------------

    def embeddings_pendientes(
        self, module_path: str, model: str
    ) -> list[dict[str, object]]:
        """Requisitos vivos que necesitan embedding con este modelo (RF-074).

        Devuelve los que no tienen embedding con ese modelo o cuyo ``embedding_text_hash``
        guardado ya no coincide. Los ``unchanged`` no aparecen, que es justo lo que evita
        pagar de nuevo por ellos en cada pasada.

        La comparacion se hace contra el hash del **texto de embedding**, no contra el del
        requisito: el perfil de atributos configurado forma parte de ese texto y puede
        cambiar sin que el requisito cambie.
        """
        filas = self.conn.execute(
            "SELECT r.*, e.embedding_text_hash AS hash_guardado"
            "  FROM requirements r"
            "  LEFT JOIN requirement_embeddings e"
            "    ON e.requirement_id = r.id AND e.model = ?"
            " WHERE r.module_path = ? AND r.is_deleted = 0"
            " ORDER BY r.absolute_number",
            (model, module_path),
        ).fetchall()
        pendientes = []
        for fila in filas:
            datos = self._fila_a_dict(fila)
            datos["id"] = int(fila["id"])
            datos["embedding_text_hash_guardado"] = fila["hash_guardado"]
            pendientes.append(datos)
        return pendientes

    def guardar_embedding(
        self,
        requirement_id: int,
        *,
        model: str,
        content_hash: str,
        embedding_text_hash: str,
        vector: Sequence[float],
    ) -> None:
        """Guarda o reemplaza el embedding de un requisito para un modelo."""
        self.conn.execute(
            "INSERT INTO requirement_embeddings"
            " (requirement_id, model, dim, content_hash, embedding_text_hash, vector,"
            "  created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(requirement_id, model) DO UPDATE SET"
            "  dim = excluded.dim, content_hash = excluded.content_hash,"
            "  embedding_text_hash = excluded.embedding_text_hash,"
            "  vector = excluded.vector, created_at = excluded.created_at",
            (
                requirement_id,
                model,
                len(vector),
                content_hash,
                embedding_text_hash,
                empaquetar_vector(vector),
                _ahora(),
            ),
        )

    def borrar_embeddings_de_borrados(self, module_path: str, model: str) -> int:
        """Retira los embeddings de requisitos marcados como eliminados.

        Igual que en el indice lexical: devolver en una busqueda semantica algo que ya no
        esta en DOORS seria peor que no tenerlo indexado.
        """
        cursor = self.conn.execute(
            "DELETE FROM requirement_embeddings"
            " WHERE model = ? AND requirement_id IN ("
            "   SELECT id FROM requirements WHERE module_path = ? AND is_deleted = 1)",
            (model, module_path),
        )
        return cursor.rowcount or 0

    def cargar_embeddings(
        self,
        model: str,
        *,
        module_path: str | None = None,
        filtros_atributos: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Carga los vectores candidatos, ya filtrados, para la busqueda vectorial.

        Los filtros estructurados se aplican **aqui**, en SQL, y no despues de puntuar
        (RF-077): filtrar un top-k ya calculado devolveria menos resultados de los pedidos,
        o ninguno, aunque existieran coincidencias validas mas abajo en el ranking.
        """
        sql = [
            "SELECT r.module_path, r.absolute_number, r.identifier, r.heading, r.text,",
            "       e.vector, e.dim",
            "  FROM requirement_embeddings e",
            "  JOIN requirements r ON r.id = e.requirement_id",
            " WHERE e.model = ? AND r.is_deleted = 0",
        ]
        parametros: list[object] = [model]
        if module_path is not None:
            sql.append("   AND r.module_path = ?")
            parametros.append(module_path)
        for nombre, valor in (filtros_atributos or {}).items():
            sql.append(
                "   AND EXISTS (SELECT 1 FROM requirement_attributes a"
                "               WHERE a.requirement_id = r.id AND a.name = ?"
                "                 AND a.value_text = ?)"
            )
            parametros += [nombre, valor]

        filas = self.conn.execute("\n".join(sql), parametros).fetchall()
        return [
            {
                "module_path": f["module_path"],
                "absolute_number": int(f["absolute_number"]),
                "identifier": f["identifier"],
                "heading": f["heading"],
                "text": f["text"],
                "vector": desempaquetar_vector(f["vector"]),
            }
            for f in filas
        ]

    def count_embeddings(self, module_path: str, model: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM requirement_embeddings e"
                " JOIN requirements r ON r.id = e.requirement_id"
                " WHERE e.model = ? AND r.module_path = ?",
                (model, module_path),
            ).fetchone()[0]
        )

    def start_embedding_run(self, module_path: str, model: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO embedding_runs (module_path, model, started_at, status)"
            " VALUES (?, ?, ?, 'running')",
            (module_path, model, _ahora()),
        )
        return int(cursor.lastrowid)

    def finish_embedding_run(
        self, run_id: int, status: str, stats: dict[str, object]
    ) -> None:
        self.conn.execute(
            "UPDATE embedding_runs SET finished_at = ?, status = ?, candidates = ?,"
            " generated = ?, skipped = ?, removed = ?, error = ? WHERE id = ?",
            (
                _ahora(),
                status,
                int(stats.get("candidates", 0)),
                int(stats.get("generated", 0)),
                int(stats.get("skipped", 0)),
                int(stats.get("removed", 0)),
                stats.get("error"),
                run_id,
            ),
        )

    def last_embedding_run(self, module_path: str) -> dict[str, object] | None:
        fila = self.conn.execute(
            "SELECT * FROM embedding_runs WHERE module_path = ? ORDER BY id DESC LIMIT 1",
            (module_path,),
        ).fetchone()
        return None if fila is None else dict(fila)

    # -----------------------------------------------------------------------------------
    # Historial de sincronizaciones (RF-062)
    # -----------------------------------------------------------------------------------

    def start_sync_run(self, module_path: str) -> int:
        """Abre una sincronizacion en estado 'running' y devuelve su identificador."""
        self.ensure_module(module_path)
        cursor = self.conn.execute(
            "INSERT INTO sync_runs (module_path, started_at, status) VALUES (?, ?, 'running')",
            (module_path, _ahora()),
        )
        return int(cursor.lastrowid)

    def finish_sync_run(self, run_id: int, status: str, stats: SyncStats) -> None:
        """Cierra la sincronizacion con su resultado y sus contadores.

        Se llama tanto en el camino correcto como en el de error: una sincronizacion que
        falla y no deja rastro es indistinguible de una que nunca se lanzo.
        """
        self.conn.execute(
            "UPDATE sync_runs SET"
            " finished_at = ?, status = ?, pages = ?, requirements_seen = ?,"
            " inserted = ?, updated = ?, unchanged = ?, deleted = ?,"
            " completed_module = ?, error = ?"
            " WHERE id = ?",
            (
                _ahora(),
                status,
                stats.pages,
                stats.seen,
                stats.inserted,
                stats.updated,
                stats.unchanged,
                stats.deleted,
                int(stats.completed_module),
                stats.error,
                run_id,
            ),
        )

    def last_sync_run(self, module_path: str) -> dict[str, object] | None:
        fila = self.conn.execute(
            "SELECT * FROM sync_runs WHERE module_path = ? ORDER BY id DESC LIMIT 1",
            (module_path,),
        ).fetchone()
        return None if fila is None else dict(fila)


# ---------------------------------------------------------------------------------------
# Serializacion de vectores
# ---------------------------------------------------------------------------------------


def empaquetar_vector(vector: Sequence[float]) -> bytes:
    """Serializa un vector como float32 little-endian.

    El formato se fija explicitamente en lugar de usar el nativo de ``array``: un fichero
    SQLite se copia entre maquinas, y un vector escrito con otro orden de bytes se leeria
    como ruido sin dar ningun error.
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def desempaquetar_vector(datos: bytes) -> list[float]:
    """Lee un vector serializado con :func:`empaquetar_vector`."""
    return list(struct.unpack(f"<{len(datos) // 4}f", datos))
