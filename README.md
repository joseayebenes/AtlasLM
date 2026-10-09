# AtlasLM — esqueleto de desarrollo

Base de conocimiento de ingeniería (documentos, requisitos de DOORS, código y grafo)
configurable por fichero, importable como librería y orquestada con Google ADK. Este
repositorio es el **esqueleto de la fase 0 del plan** (`legacy/PLAN.md`): motor
incremental, configuración validada, sistema de plugins, lector de DOORS desde base de
datos, analizador Ada, datos de ejemplo, servidor falso y pruebas. Los scripts anteriores
están en `legacy/` y se migran módulo a módulo en la fase 1. Ya están migrados
`pdf_struct.py` (ahora `atlas.docs`), `doc_rag.py` (`atlas.index`), `ada_graph.py`
(`atlas.code` y `atlas.graph`) y las consultas de `ada_graph_mcp.py` (`atlas.graph`).
Falta el servidor MCP (`atlas.mcp`).

## Puesta en marcha en un Mac (Apple Silicon)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[docs,code,mcp,dev]"      # añade ",agents" cuando empieces con ADK
python examples/make_samples.py             # datos de ejemplo (PDFs, Ada, requisitos)
python -m pytest -q                         # 90 pruebas sin modelo (+2 de agentes si Ollama está en marcha)
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
python -m atlas.cli graph    atlas.dev.toml   # informe del grafo de código
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
python -m atlas.cli run atlas.test.toml           # primera vez: DOORS, Ada, grafo, documentos e índice
python -m atlas.cli run atlas.test.toml           # segunda vez: todo "al día", 0 peticiones
python -m atlas.cli graph atlas.test.toml
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
├── doors/          requisitos: base de intercambio (exchange.py) y adaptadores "exchange" y "database"
├── code/           analizadores de código: "ada" (era ada_graph.py), etiquetas de requisito,
│                   herramientas de ficheros para agentes de código
├── docs/           documentos (era pdf_struct.py): extractores, estructura, análisis y Markdown
├── index/          índice (era doc_rag.py): registros, embeddings con caché, FTS5, búsqueda
│                   híbrida y respuesta citada; sus tablas van en el mismo atlas.db
├── graph/          grafo de código en atlas.db, consultas (eran ada_graph*.py), GraphML y JSON
├── agents/ mcp/    pendientes (fases 1 y 5 del plan)
└── cli.py          run | status | sections | graph | search | ask | plugins | check
examples/make_samples.py    genera examples/samples/ (PDFs con y sin marcadores, Ada, requirements.sqlite)
tools/doors_export/         exportador de DOORS a la base de intercambio (descarga + publicación)
tests/                      pytest; tests/fakes/fake_api.py es el servidor OpenAI falso y
                            test_agents_ollama.py, la prueba de agentes con Ollama
legacy/                     scripts actuales + README y PLAN del proyecto
```

## Idioma

El código está en inglés: nombres, comentarios, docstrings, mensajes de la línea de
comandos y los ficheros que genera (los Markdown de cada documento; el contenido de los
resúmenes sigue `lang`). La documentación está en español: este README, `legacy/PLAN.md`,
`legacy/README.md` y los comentarios de los TOML de ejemplo. `legacy/` conserva el español
mientras siga existiendo.

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

## Código y grafo

El paso `code` analiza cada fichero Ada con tree-sitter y guarda en `atlas.db`:
- sus subprogramas, con sus líneas, sus llamadas propias, el comentario de cabecera (el del
  `.ads` si lo hay) y los requisitos que citan;
- sus tipos, variables, constantes y `with`.

Los requisitos se leen de la etiqueta `[sources.code] req_tag` (`"Req:"` por defecto). Para
otro formato, `req_regex`, cuyo grupo 1 es la lista de IDs.

El paso `graph` construye con eso el grafo de código y lo exporta a `<workspace>/graph/`
(`code.graphml` para Gephi o yEd, `code.json`).

Los elementos se identifican como `unidad.nombre` (`tracks.update`), así que dos paquetes
pueden tener un `Update` cada uno. Cada `Nombre (...)` de un cuerpo se clasifica según lo
que es `Nombre`:
- un subprograma: es una llamada;
- un tipo: es una conversión;
- una variable de paquete: es un uso;
- un tipo predefinido: se ignora;
- algo desconocido: es una llamada externa.

Un nombre sin paquete se busca primero en la propia unidad y luego en las demás,
prefiriendo las que se importan con `with`. Si aún hay varias, la arista va a todas, para
que el análisis de impacto no se deje ninguna; `atlas graph` las cuenta como llamadas
ambiguas.

Como librería, las consultas son las del antiguo `ada_graph_mcp.py`:

```python
from atlas.config import load_config
from atlas.graph import KnowledgeGraph
g = KnowledgeGraph.from_config(load_config("atlas.dev.toml"))
g.impact("Delete_Track", depth=3)          # quién lo llama, directa o indirectamente
g.requirement("SRS-0004")                 # qué subprogramas y tipos lo trazan
g.read_source("tracks.update")            # el código, con números de línea
```

### Herramientas para agentes de código

`atlas.code.CodeTools` tiene las herramientas habituales de un agente de código sobre
`[sources.code] roots`:

| Herramienta | Qué hace |
|---|---|
| `list_files` | ficheros por patrón glob (`*.adb` busca en todas las subcarpetas) |
| `grep` | expresión regular, con fichero, línea y líneas de contexto |
| `read_file` | un fragmento numerado |
| `edit_file` | sustituye un texto y devuelve el diff |
| `write_file` | crea un fichero o lo reescribe entero |

Protecciones:
- No tocan nada fuera de las raíces ni de lo excluido con `exclude`; tampoco con `..` ni
  con enlaces simbólicos.
- Solo editan un fichero leído antes, y no si ha cambiado desde entonces.
- Si el texto a sustituir aparece varias veces, exigen precisar más o pedir `replace_all`.
- Conservan la codificación, los finales de línea CRLF y los permisos, y escriben de forma
  atómica.
- `CodeTools(..., read_only=True)` deja solo las de lectura.

Un error es una respuesta con `error` (y, si el texto no aparece, las líneas más
parecidas) para que el agente se corrija.

Los modelos pequeños suelen fallar copiando el texto a sustituir: escapan las comillas o
cambian la sangría. `edit_file` lo intenta primero exacto y luego sin esos escapes y sin
tener en cuenta la sangría, siempre que la coincidencia sea única, y lo indica en `notes`.
Con eso, un agente ADK con `gemma4:12b` cambió un mensaje de log en los ejemplos en 5
intentos de 5, unos 20 s cada uno; sin esa tolerancia entraba en bucle.

```python
from atlas.code import CodeTools
tools = CodeTools.from_config(load_config("atlas.dev.toml"))
agent = LlmAgent(..., tools=[tools.grep, tools.read_file, tools.edit_file])   # ADK
```

También están registradas como plugins `tool` con esos nombres, para `[agents.*] tools` y
`[mcp] tools`. Lo que se edite se vuelve a analizar en el siguiente `atlas run`.

## Requisitos: la base de intercambio

atlas lee siempre los requisitos de la **base de intercambio**: una base SQLite con un
esquema fijo y versionado (`atlas/doors/exchange.py`).

| Tabla | Qué guarda |
|---|---|
| `exchange_modules` | módulos, con una instantánea que cambia con su contenido |
| `exchange_requirements` | ID, texto, título, tipo (`requirement`, `heading` o `information`), padre, orden, nivel, numeración y atributos |
| `exchange_links` | enlaces ya tipados |

Lo que cambia de un proyecto a otro es **quién la rellena**: cada relleno hace su propia
correspondencia con la fuente, y atlas no necesita saber de dónde vienen los datos.

```
DOORS ─► tools/doors_export ─┐
ReqIF, hoja de cálculo... ───┼─► requirements.sqlite ─► atlas (adapter = "exchange")
```

- **DOORS**: `tools/doors_export` descarga en bruto desde DOORS (en el PC con DOORS) y
  publica la base aplicando `mapping.toml`. Ese fichero dice qué atributo da el ID y el
  texto, qué atributos pasan, cómo se llama cada módulo y qué tipo es cada módulo de
  enlaces. Ver [su README](tools/doors_export/README.md).
- **Otro origen**: basta con escribir la base con `ExchangeWriter`, que comprueba el
  contrato: IDs únicos, tipos válidos y enlaces que salen del propio módulo.

En `atlas.toml` solo se dice dónde está la base y cómo casan los IDs con documentos y
código:

```toml
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "C:/atlas/tms/requirements.sqlite"
[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-\\d{3,6}$"
inline_reference_regex = "\\b(?:SRS|SSS)-\\d{3,6}\\b"   # "see SSS-0012" en el texto es un enlace
```

Para leer directamente otra base con consultas propias queda el adaptador `database`
(`[sources.doors.database]`, SQLite u ODBC; ver `atlas/doors/__init__.py`).
