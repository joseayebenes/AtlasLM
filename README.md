# AtlasLM — esqueleto de desarrollo

Base de conocimiento de ingeniería (documentos, requisitos de DOORS, código y grafo)
configurable por fichero, importable como librería y orquestada con Google ADK. Este
repositorio es el **esqueleto de la fase 0 del plan** (`legacy/PLAN.md`): motor
incremental, configuración validada, sistema de plugins, lector de DOORS desde base de
datos, analizador Ada, datos de ejemplo, servidor falso y pruebas. Los scripts anteriores
están en `legacy/` y se migran módulo a módulo en la fase 1. Ya están migrados
`pdf_struct.py` (ahora `atlas.docs`) y `doc_rag.py` (ahora `atlas.index`).

## Puesta en marcha en un Mac (Apple Silicon)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[docs,code,mcp,dev]"      # añade ",agents" cuando empieces con ADK
python examples/make_samples.py             # datos de ejemplo (PDFs, Ada, doors.sqlite)
python -m pytest -q                         # 50 pruebas sin modelo (+1 de agentes si Ollama está en marcha)
```

### Tres niveles de modelo

| Nivel | Para qué | Configuración |
|---|---|---|
| **Servidor falso** (`tests/fakes/fake_api.py`) | pruebas automáticas y desarrollo del motor: determinista, instantáneo, con fallos inyectables (409, 401, respuestas cortadas, prosa, modelo que razona) | `atlas.test.toml` |
| **Modelo local** (Ollama o LM Studio) | probar la calidad real de análisis, búsqueda y agentes sin cupo ni red | `atlas.dev.toml` |
| **Gateway corporativo** | producción | `atlas.toml` |

Los tres usan la misma API compatible con OpenAI: cambiar de nivel es cambiar `base_url`.

### Ollama o LM Studio

Cualquiera de los dos vale; el código no distingue. Recomendación:

- **Ollama** para empezar: `brew install ollama`, luego `ollama pull gemma4:12b` y
  `ollama pull embeddinggemma`. API en `http://localhost:11434/v1`. Es el que usa
  `atlas.dev.toml`. Arranca el servicio con `OLLAMA_CONTEXT_LENGTH=16384 ollama serve`,
  porque el contexto por defecto es pequeño y recorta las secciones largas sin avisar.
- **LM Studio** si quieres interfaz gráfica y algo más de velocidad en Apple Silicon (motor
  MLX). Activa el servidor local (por defecto `http://localhost:1234/v1`) y pon esa URL y
  el nombre exacto del modelo cargado en `atlas.dev.toml`.

Tamaño del modelo según la memoria del Mac (valores aproximados de descarga en Ollama):

| Memoria | Chat | Embeddings |
|---|---|---|
| 16 GB | `gemma4:e4b` (~10 GB) o `gemma4:12b` (~8 GB cuantizado) | `embeddinggemma` |
| 24-32 GB | `gemma4:26b` (MoE, 4B activos: rápido para su calidad) | `embeddinggemma` |

Comprueba la conexión antes de nada: `python -m atlas.cli check atlas.dev.toml`. Si el
modelo razona antes de contestar (gemma4 lo hace por defecto), `check` lo indica
(`OK: 'ok' (reasoning model: …)`). Ese razonamiento cuenta dentro de `max_tokens`. Si el
modelo agota el presupuesto razonando sin llegar a contestar, `check` falla y lo explica.

`atlas.dev.toml` desactiva el razonamiento con `[llm] reasoning_effort = "none"` (`check` y
el análisis de documentos lo envían en cada petición). Con razonamiento, `gemma4:12b`
tardaba entre 1 y 8 minutos en cada sección del manual de ejemplo. Sin él tarda unos 15 s,
y el manual entero, 48 s, con los tres requisitos literales. Esa versión de Ollama tampoco
acepta `response_format` con esquema JSON (responde 501): el análisis sigue sin él y saca
el JSON de la respuesta.

### Agentes ADK con Ollama

```bash
pip install -e ".[agents]"                       # google-adk y litellm
OLLAMA_CONTEXT_LENGTH=16384 ollama serve         # en otra terminal
make test-ollama                                 # o: python -m pytest -q -m ollama
```

`tests/test_agents_ollama.py` monta un `LlmAgent` de ADK con LiteLLM sobre la URL y el
modelo de `atlas.dev.toml` (`[llm] base_url`, `[agents] model`). Le da dos herramientas
sobre las muestras: `get_requirement`, que usa el adaptador de DOORS, y `trace`, que busca
las etiquetas `Req:` en el Ada. Comprueba que el agente usa las dos, que responde con el
requisito y los ficheros correctos y que no se inventa ficheros. Se omite sola si falta el
extra `[agents]`, si Ollama no responde o si el modelo no está descargado. Para excluirla:
`python -m pytest -m "not ollama"`.

Con `gemma4:12b`, conviene desactivar el razonamiento en los agentes
(`reasoning_effort="none"`, como hace la prueba). Con razonamiento, 7 de 10 respuestas
fueron correctas y tardaron 40-80 s; las otras salieron vacías o con ficheros inventados.
Sin razonamiento, acertó 5 de 5 en 8-14 s. Al instalar `[agents]`, litellm baja `openai`
a la versión 2.x; el resto del proyecto funciona igual.

## Comandos

```bash
python -m atlas.cli run      atlas.dev.toml   # procesamiento incremental y reanudable
python -m atlas.cli status   atlas.dev.toml   # libro de registro y datos
python -m atlas.cli sections atlas.dev.toml   # secciones de cada documento, sin llamar al modelo
python -m atlas.cli search "track deletion latency" atlas.dev.toml   # búsqueda en los documentos
python -m atlas.cli ask "¿Cuánto tarda en borrarse una traza?" atlas.dev.toml   # respuesta con citas
python -m atlas.cli plugins  atlas.dev.toml   # analizadores, adaptadores y extractores disponibles
python -m atlas.cli check    atlas.dev.toml   # conexión con el modelo
```

Toda la configuración está en el TOML; la línea de comandos solo elige acción y fichero.
Con `pip install -e .` el comando `atlas` queda disponible directamente.

Prueba el motor sin modelo:

```bash
python tests/fakes/fake_api.py --port 8770 &      # en otra terminal
python -m atlas.cli run atlas.test.toml           # primera vez: DOORS, Ada, documentos e índice
python -m atlas.cli run atlas.test.toml           # segunda vez: todo "al día", 0 peticiones
python -m atlas.cli search "alarm latency" atlas.test.toml
# cambia un .adb, un requisito en examples/samples/doors.sqlite o hmi_manual.md y vuelve a
# ejecutar: solo se reprocesa esa unidad (y del documento, solo las secciones cambiadas y
# los textos nuevos del índice)
```

## Estructura

```
atlas/
├── config.py       carga y validación de atlas.toml, ${VAR}, huellas por sección
├── plugins.py      interfaces (CodeAnalyzer, RequirementsAdapter, DocumentExtractor, Tool) y registro
├── pipeline/       motor incremental: Step, Ledger (SQLite), Runner, cuarentena, poda, bloqueo
├── store.py        atlas.db: requisitos, enlaces, unidades de código, documentos
├── llm/            connection.py: clientes OpenAI, proxy, TLS, límites, reintentos;
│                   structured.py: respuestas JSON con reparación
├── doors/          adaptador "database": consultas y convención configurables, diff
├── code/           analizador "ada" (envuelve legacy/ada_graph.py)
├── docs/           documentos (era pdf_struct.py): extractores, estructura, análisis y Markdown
├── index/          índice (era doc_rag.py): registros, embeddings con caché, FTS5, búsqueda
│                   híbrida y respuesta citada; sus tablas van en el mismo atlas.db
├── graph/ agents/ mcp/    pendientes de migración (fases 1, 4 y 5 del plan)
└── cli.py          run | status | sections | search | ask | plugins | check
examples/make_samples.py    genera examples/samples/ (PDFs con y sin marcadores, Ada, doors.sqlite)
tests/                      pytest; tests/fakes/fake_api.py es el servidor OpenAI falso y
                            test_agents_ollama.py, la prueba de agentes con Ollama
legacy/                     scripts actuales + README y PLAN del proyecto
```

## Idioma

El código está en inglés: nombres, comentarios, docstrings, mensajes de la línea de
comandos y los ficheros que genera (los Markdown de cada documento; el contenido de los
resúmenes sigue `lang`). La documentación está en español: este README, `legacy/PLAN.md`,
`legacy/README.md` y los comentarios de los TOML de ejemplo. `legacy/` conserva el español
hasta que se migre; por eso `atlas/code` sigue llamando a `ada_graph.analizar`.

## Añadir o quitar piezas

- **Otro lenguaje**: una clase `CodeAnalyzer` registrada con `register("code_analyzer", "cpp", Clase)`
  en un módulo propio; actívalo con `[sources.code] languages = ["ada", "cpp"]` y declara el
  módulo en `[plugins] modules` (o como entry point `atlas.plugins` en su `pyproject`).
- **Otra fuente de requisitos**: una clase `RequirementsAdapter` (`modules`, `snapshot`,
  `requirements`, `links`) y `adapter = "nombre"` en `[sources.doors]`.
- **Otro formato de documento**: una clase `DocumentExtractor` por extensión.
- **Quitar algo**: `[plugins] disabled = ["code_analyzer:ada"]`, o no activarlo en la fuente.

## Documentos

Cada `[[sources.docs]]` es un grupo de documentos (PDF, Markdown, Word...) con su forma de
procesarlos:

| Clave | Valores | Para qué |
|---|---|---|
| `paths`, `patterns` | carpetas o ficheros; `["*.pdf"]` | qué documentos (las carpetas se recorren enteras) |
| `analysis` | `llm` (por defecto), `regex`, `none` | `llm`: el modelo saca resumen, requisitos, glosario, entidades y pendientes por sección. `regex`: solo requisitos, sin modelo. `none`: solo la estructura |
| `req_regex` | `(?i)\bReq\s*:\s*(\S+)` | en `regex`, marca cada requisito; el grupo 1 es su ID y el texto sigue hasta el final de la frase |
| `structure` | `auto`, `bookmarks`, `headings`, `size` | de dónde salen las secciones: marcadores del PDF, títulos numerados del texto o bloques de tamaño fijo |
| `heading_regex` | grupo 1 = número, grupo 2 = título | si los títulos tienen un formato que no se reconoce |
| `max_chars`, `min_chars` | 12000, 300 | tamaño máximo de sección; las más cortas se unen a la anterior |
| `lang` | `"Spanish"` | idioma de los resúmenes (vacío: el del documento) |

Cada documento se escribe en `<workspace>/docs/<fuente>/<nombre>_<extensión>/`: `index.md`,
`sections/`, `requirements.md` (con la verificación de cada requisito contra el texto),
`glossary.md`, `entities.md`, `open_issues.md` y `structure.json` (todo junto, para el
índice y el grafo). Los análisis de sección se guardan en `<workspace>/docs/.cache`: un
documento modificado solo vuelve al modelo con las secciones cuyo texto ha cambiado. Si
falla alguna, el documento se escribe igual y queda en error para reintentarla en la
siguiente ejecución.

Antes de procesar documentos nuevos, `atlas sections` muestra qué secciones saldrían, de
dónde sale la estructura y por qué se aceptan o rechazan las líneas que parecen títulos.

Como librería, sin motor ni CLI:

```python
from atlas.docs import DocsOptions, process_document
data = process_document("spec.pdf", "out/spec", DocsOptions(analysis="regex"))
```

## Búsqueda

El paso `index` de `atlas run` indexa cada documento procesado a varias granularidades:

- resumen global;
- resúmenes de sección;
- fragmentos de unos `chunk_chars` caracteres con solape, cada uno con la ruta y el
  resumen de su sección;
- requisitos;
- definiciones;
- pendientes.

`[index] kinds` elige cuáles. Una búsqueda combina tres listas con Reciprocal Rank Fusion:

- los IDs exactos que aparecen en la consulta;
- la similitud de los vectores (`[embeddings]`);
- BM25 sobre el mismo texto (FTS5 de SQLite).

`atlas ask` reescribe antes la pregunta en consultas de búsqueda. Después pasa al modelo
los mejores resultados, con los fragmentos vecinos, y le pide responder solo con esas
fuentes y citándolas como `[n]`.

Todo va en `atlas.db`: registros, vectores, índice de texto y una caché de vectores por
modelo y texto, que evita embeber dos veces el mismo texto. Un documento reanalizado solo
embebe sus textos nuevos. Cambiar `[embeddings] model` reindexa todo sin volver al LLM, y
la búsqueda nunca mezcla vectores de dos modelos.

Con Ollama (`atlas.dev.toml`), indexar los tres documentos de ejemplo con `embeddinggemma`
lleva segundos. Una respuesta de `gemma4:12b` tarda de 50 s a 2 min, según la longitud del
contexto y de la respuesta.

Como librería:

```python
from atlas.config import load_config
from atlas.index import Index
idx = Index.from_config(load_config("atlas.dev.toml"))
hits = idx.search("alarm latency", k=8, kinds=["requirement"])
res = idx.answer("When does the HMI raise an alarm?")      # {"answer": ..., "sources": [...]}
```

## DOORS desde una base de datos

`[sources.doors.database]` lleva las consultas SQL (módulos, requisitos, enlaces e
instantánea) con el parámetro `:module`, y `[sources.doors.convention]` el nombre de las
columnas, el patrón de los IDs, la normalización, las columnas de enlace y la expresión
regular de referencias dentro del texto. Ver `atlas.dev.toml` (SQLite de ejemplo) y
`atlas.toml` (ODBC hacia SQL Server). Nada de esto está en el código.
