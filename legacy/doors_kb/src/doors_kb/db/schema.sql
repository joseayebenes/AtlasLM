-- Esquema de la copia local de requisitos (seccion 6 de la especificacion).
--
-- Decisiones que conviene tener presentes al leerlo:
--
--  * La identidad logica de un requisito es (module_path, absolute_number) (RF-052). El
--    Absolute Number por si solo no vale: se repite entre modulos distintos.
--  * Los atributos del proyecto se guardan normalizados en su propia tabla (RF-053), de
--    modo que anadir un atributo en DOORS no obliga a migrar el esquema SQL.
--  * Los borrados son logicos (is_deleted), nunca fisicos (RF-061): un objeto que
--    desaparece de DOORS puede reaparecer, y su historia local sigue siendo util.
--  * El esquema deja sitio a la tabla FTS5 del hito H4 sin migraciones destructivas.

PRAGMA foreign_keys = ON;

-- Modulos sincronizados y su frescura (RF-051).
CREATE TABLE IF NOT EXISTS modules (
    module_path        TEXT PRIMARY KEY,
    last_sync_at       TEXT,
    last_full_sync_at  TEXT
);

-- Datos principales de cada objeto de DOORS y su hash de contenido (RF-051, RF-054).
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
    source_last_modified  TEXT,
    first_seen_at         TEXT    NOT NULL,
    last_seen_at          TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    deleted_at            TEXT,
    UNIQUE (module_path, absolute_number)
);

-- Consulta habitual: los requisitos vivos de un modulo.
CREATE INDEX IF NOT EXISTS idx_requirements_modulo_activo
    ON requirements (module_path, is_deleted);

-- Util para detectar duplicados de contenido y, en H5, para localizar los embeddings
-- que se quedan obsoletos cuando cambia el hash.
CREATE INDEX IF NOT EXISTS idx_requirements_hash
    ON requirements (content_hash);

-- Atributos arbitrarios del proyecto, sin una columna SQL por atributo (RF-053).
CREATE TABLE IF NOT EXISTS requirement_attributes (
    requirement_id  INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    value_text      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (requirement_id, name)
) WITHOUT ROWID;

-- Trazabilidad entre objetos (RF-051). Se rellena en el hito H8 (Graph-RAG); la tabla
-- existe desde ahora para no partir el esquema mas adelante.
CREATE TABLE IF NOT EXISTS links (
    id                      INTEGER PRIMARY KEY,
    source_module           TEXT    NOT NULL,
    source_absolute_number  INTEGER NOT NULL,
    target_module           TEXT    NOT NULL,
    target_absolute_number  INTEGER NOT NULL,
    link_module             TEXT    NOT NULL DEFAULT '',
    UNIQUE (source_module, source_absolute_number,
            target_module, target_absolute_number, link_module)
);

CREATE INDEX IF NOT EXISTS idx_links_origen
    ON links (source_module, source_absolute_number);
CREATE INDEX IF NOT EXISTS idx_links_destino
    ON links (target_module, target_absolute_number);

-- Auditoria de cada sincronizacion (RF-062). Es la memoria del sistema: permite explicar
-- por que la copia local esta como esta, y detectar sincronizaciones que fallaron.
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

-- ---------------------------------------------------------------------------------------
-- Indice lexical FTS5 (RF-070, hito H4)
-- ---------------------------------------------------------------------------------------
--
-- Es una tabla FTS5 autonoma, no una de contenido externo (content=...): las columnas de
-- una FTS5 con contenido externo deben corresponderse con columnas de la tabla de origen, y
-- aqui los atributos del proyecto viven normalizados en requirement_attributes, donde no
-- hay tal correspondencia. Cuesta duplicar el texto en disco; a cambio la actualizacion es
-- explicita y comprobable (ADR-010).
--
-- remove_diacritics 2 permite que "conexion" encuentre "conexión" y al reves, que es
-- imprescindible con requisitos escritos en espanol. No se usa el stemmer porter: es de
-- ingles y estropearia justo lo que ADR-007 quiere preservar, los identificadores y el
-- vocabulario tecnico exacto.
CREATE VIRTUAL TABLE IF NOT EXISTS requirements_fts USING fts5(
    module_path      UNINDEXED,
    absolute_number  UNINDEXED,
    identifier,
    heading,
    text,
    attributes,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- ---------------------------------------------------------------------------------------
-- Embeddings (RF-071 a RF-074, hito H5)
-- ---------------------------------------------------------------------------------------
--
-- Se guardan DOS hashes por embedding y no es redundancia:
--
--   * content_hash        -- el del requisito. Dice si el requisito cambio (RF-073).
--   * embedding_text_hash -- el del texto que se embebio. Ese texto incluye un perfil
--                            configurable de atributos (RF-072), asi que puede cambiar sin
--                            que cambie el requisito. Sin este segundo hash, cambiar el
--                            perfil dejaria el indice obsoleto en silencio.
--
-- La clave primaria incluye el modelo: convivir con dos modelos a la vez es lo que permite
-- migrar de proveedor sin quedarse sin busqueda semantica mientras se reindexa (R-006).
CREATE TABLE IF NOT EXISTS requirement_embeddings (
    requirement_id       INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    model                TEXT    NOT NULL,
    dim                  INTEGER NOT NULL,
    content_hash         TEXT    NOT NULL,
    embedding_text_hash  TEXT    NOT NULL,
    vector               BLOB    NOT NULL,
    created_at           TEXT    NOT NULL,
    PRIMARY KEY (requirement_id, model)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_embeddings_modelo ON requirement_embeddings (model);

-- Auditoria de cada generacion, en paralelo a sync_runs.
CREATE TABLE IF NOT EXISTS embedding_runs (
    id            INTEGER PRIMARY KEY,
    module_path   TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    status        TEXT    NOT NULL,  -- running | success | failed
    candidates    INTEGER NOT NULL DEFAULT 0,
    generated     INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    removed       INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_embedding_runs_modulo
    ON embedding_runs (module_path, started_at DESC);
