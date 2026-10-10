# AtlasLM — esqueleto de desarrollo

Base de conocimiento de ingeniería (documentos, requisitos de DOORS, código y grafo)
configurable por fichero, importable como librería y orquestada con Google ADK. Este
repositorio es el **esqueleto de la fase 0 del plan** (`legacy/PLAN.md`): motor
incremental, configuración validada, sistema de plugins, lector de DOORS desde base de
datos, analizador Ada, datos de ejemplo, servidor falso y pruebas. Los scripts anteriores
están en `legacy/` y se migran módulo a módulo en la fase 1. Ya están migrados
`pdf_struct.py` (ahora `atlas.docs`), `doc_rag.py` (`atlas.index`), `ada_graph.py`
(`atlas.code` y `atlas.graph`) y los dos servidores MCP, `ada_graph_mcp.py` y el modo `mcp`
de `doc_rag.py`, que ahora son uno solo (`atlas.mcp`, `atlas serve`).

## Puesta en marcha en un Mac (Apple Silicon)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[docs,code,mcp,dev]"      # añade ",agents" cuando empieces con ADK
python examples/make_samples.py             # datos de ejemplo (PDFs, Ada, requisitos)
python -m pytest -q                         # 118 pruebas sin modelo (+5 de agentes si Ollama está en marcha)
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
pip install -e ".[agents]"                       # google-adk
OLLAMA_CONTEXT_LENGTH=16384 ollama serve         # en otra terminal
make test-ollama                                 # o: python -m pytest -q -m ollama
```

`tests/test_agents_ollama.py` prueba los agentes contra la URL y el modelo de
`atlas.dev.toml` (`[llm] base_url`, `[agents] model`), con `AtlasLlm` (sección
[Agentes](#agentes)). Son cinco pruebas:
- un agente con dos herramientas propias sobre las muestras (`get_requirement` y una `trace`
  que busca las etiquetas `Req:`): usa las dos, responde con el requisito y los ficheros
  correctos y no se inventa ficheros;
- un cambio preciso en el Ada con las herramientas de ficheros;
- un agente con las herramientas de `atlas serve` por MCP;
- el asistente de serie, como lo ejecuta `atlas agent assistant`;
- la verificación de un requisito con la documentación.

Se omiten solas si falta el extra `[agents]`, si Ollama no responde o si el modelo no está
descargado. Para excluirlas: `python -m pytest -m "not ollama"`.

Con `gemma4:12b`, conviene desactivar el razonamiento en los agentes
(`reasoning_effort: none`, como hacen los agentes de serie). Con razonamiento, 7 de 10
respuestas fueron correctas y tardaron 40-80 s; las otras salieron vacías o con ficheros
inventados. Sin razonamiento, acertó 5 de 5 en 8-14 s.

La tercera prueba da al agente solo las herramientas de `atlas serve`, por MCP, como
OpenCode o VS Code. Antes, procesa un PDF y el Ada de ejemplo con los embeddings de Ollama.
Tarda unos dos minutos con el modelo en frío. Se hicieron cuatro preguntas, tres veces cada
una: trazabilidad, impacto, documentos y una sección.
- Sin razonamiento, el agente acertó 12 de 12, con una mediana de 14 s.
- Con razonamiento, acertó 9 de 12, con una mediana de 58 s. En las tres de trazabilidad no
  llamó a `trace` y se inventó los subprogramas.
- Tras la fase 4 (26 herramientas), sin razonamiento: 12 de 12, con una mediana de 18 s.
  `trace` ya da los enlaces de DOORS, el código y las pruebas, así que basta con esa
  llamada; la prueba ya no exige también `get_requirement`.

## Comandos

```bash
python -m atlas.cli run      atlas.dev.toml   # procesamiento incremental y reanudable
python -m atlas.cli status   atlas.dev.toml   # libro de registro y datos
python -m atlas.cli sections atlas.dev.toml   # secciones de cada documento, sin llamar al modelo
python -m atlas.cli graph    atlas.dev.toml   # informe del grafo (código, DOORS, documentos) y de sus huecos
python -m atlas.cli trace SRS-0004 atlas.dev.toml   # todo sobre un requisito: DOORS, documentos, código, pruebas
python -m atlas.cli matrix SRS_TMS atlas.dev.toml   # matriz de trazabilidad de un módulo, en CSV y Markdown
python -m atlas.cli search "track deletion latency" atlas.dev.toml   # búsqueda en los documentos
python -m atlas.cli ask "¿Cuánto tarda en borrarse una traza?" atlas.dev.toml   # respuesta con citas
python -m atlas.cli plugins  atlas.dev.toml   # analizadores, adaptadores y extractores disponibles
python -m atlas.cli agents   atlas.dev.toml   # agentes (ficheros Markdown): de serie y del proyecto
python -m atlas.cli agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml   # pregunta a un agente
python -m atlas.cli verify SRS_TMS atlas.dev.toml    # cada requisito contra la documentación, con el modelo
python -m atlas.cli impact Logger.Log atlas.dev.toml # informe de lo que afecta un cambio
python -m atlas.cli coverage SRS_TMS atlas.dev.toml  # informe de los huecos de trazabilidad
python -m atlas.cli eval     atlas.dev.toml   # evaluación de los agentes con preguntas de respuesta conocida
python -m atlas.cli check    atlas.dev.toml   # conexión con el modelo
python -m atlas.cli serve    atlas.dev.toml   # servidor MCP (stdio) para OpenCode, VS Code...
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

Para medir el motor con un proyecto grande, `make large` genera uno sintético en
`examples/large/`: 20 PDFs (260 páginas), 660 ficheros Ada y 5500 requisitos. Después lo
procesa tres veces con el servidor falso. Resultados:
- la primera ejecución tarda 17,5 s;
- la segunda, sin cambios, 0,1 s y sin ninguna petición;
- tras cambiar un plazo en una página de un PDF, 1,5 s y una sola petición al modelo.

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
│                   híbrida y respuesta citada; sus tablas van en el mismo atlas.db;
│                   herramientas de documentos para agentes y MCP
├── graph/          grafo de código, DOORS y documentos en atlas.db; consultas (eran
│                   ada_graph*.py) y trazabilidad; GraphML, JSON y Mermaid; herramientas
│                   del grafo para agentes y MCP
├── mcp/            servidor MCP con las herramientas de documentos, grafo y ficheros
├── agents/         agentes en ficheros Markdown (builtin/), AtlasLlm para ADK, build_context,
│                   verificación de requisitos, informes y evaluación
└── cli.py          run | status | sections | graph | trace | matrix | verify | impact | coverage |
                    search | ask | plugins | agents | agent | eval | check | serve
examples/make_samples.py    genera examples/samples/ (PDFs con y sin marcadores, Ada, requirements.sqlite)
examples/large_project.py   proyecto grande sintético y medición del motor (make large)
examples/eval/              conjuntos de evaluación de los agentes (atlas eval)
examples/adk/atlas_agent/   un agente de atlas para adk web y adk run
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

Los requisitos de DOORS también se indexan (paso `index.requirements`): cada módulo es un
documento más del índice, con el nombre del módulo. Cada requisito es un registro de tipo
`requirement` con los encabezados que tiene encima como ruta, y cada objeto de información,
un fragmento. Así, `search` encuentra un requisito aunque no aparezca en ningún documento, y
su cita queda como `SRS_TMS › 1.1 Deletion (requirement)`. `get_requirement` sigue dando el
texto oficial aparte, y en sus documentos no repite el de DOORS.

`[index] kinds` elige qué se indexa. Una búsqueda combina tres listas con Reciprocal Rank
Fusion:

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

El paso `graph` construye con eso el grafo de código, que forma parte del grafo unificado
(sección siguiente).

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

Los ficheros que casan con `[sources.code] test_patterns` (`["**/tests/**"]`) son pruebas:
sus subprogramas son nodos `test`. Un elemento que cita un requisito lo implementa (arista
`implements`); una prueba que lo cita lo verifica (`verifies`).

Como librería, las consultas son las del antiguo `ada_graph_mcp.py`, más las de
trazabilidad:

```python
from atlas.config import load_config
from atlas.graph import KnowledgeGraph
g = KnowledgeGraph.from_config(load_config("atlas.dev.toml"))
g.impact("Delete_Track", depth=3)          # lo que depende de él, y los requisitos y pruebas afectados
g.trace("SRS-0004")                       # DOORS, documentos, código y pruebas del requisito
g.read_source("tracks.update")            # el código, con números de línea
```

## Grafo y trazabilidad

El paso `graph` junta en un solo grafo el código, los requisitos de DOORS y los documentos.
Cada parte se construye por separado y solo cuando cambia aquello de lo que sale:

| Parte | Sale de | Nodos | Aristas |
|---|---|---|---|
| `code` | `code.parse` | unidades, subprogramas, pruebas, tipos, variables, requisitos citados | `defines`, `calls`, `converts`, `uses`, `withs`, `implements`, `verifies` |
| `doors` | `doors.requirements` | módulos (`module:SRS_TMS`), requisitos, encabezados | `contains` (módulo y jerarquía) y un enlace por cada enlace de DOORS, con su tipo (`satisfies`...) |
| `docs` | `docs.process` | documentos (`doc:hmi_manual.md`) y secciones (`doc:hmi_manual.md#003`) | `contains`, `declares` (la sección declara el requisito) y `mentions` (la sección nombra un elemento de código) |

Un requisito es el mismo nodo en las tres partes. Sus IDs se normalizan igual que los de
DOORS (`[sources.doors.convention] id_normalize`; mayúsculas por defecto), así que
`Req: srs-0004` en el código y `SRS-0004` en DOORS se encuentran. Una sección nombra un
elemento de código cuando escribe su nombre completo (`Tracks.Delete_Track`) o un nombre con
guion bajo que solo tiene un elemento (`Delete_Track`). Las palabras sueltas, como `Update`,
son demasiado comunes para contar.

El grafo completo se exporta a `<workspace>/graph/atlas.graphml` (Gephi, yEd) y
`atlas.json`. Con `[graph] communities = true`, cada elemento de código lleva su grupo
(Louvain), para colorearlo en Gephi.

Consultas de trazabilidad, sin modelo:

| Consulta | Devuelve |
|---|---|
| `trace(req)` | texto y atributos de DOORS; padre, hijos y enlaces; las secciones que lo declaran, con su texto y las cifras que difieren de DOORS; el código que lo implementa; las pruebas que lo verifican (lo citan) o lo cubren (llaman, directa o indirectamente, a ese código) |
| `trace_code(nombre)` | requisitos, llamadores, llamados, pruebas y secciones de un elemento de código; de una prueba, el código que ejercita y los requisitos que cubre |
| `impact(nombre)` | de código: lo que depende de él (lo llama, convierte a su tipo, usa su variable), y los requisitos, pruebas y secciones afectados, incluidos los requisitos que esos satisfacen en DOORS; de un requisito: los que derivan de él, su código, pruebas y secciones |
| `matrix(módulo)` | requisito → código → pruebas → documentos, con resumen y huecos |
| `gaps(módulo)` | requisitos sin código o sin pruebas; subprogramas sin requisito; IDs del código o de los documentos que no están en DOORS; cifras distintas entre DOORS y los documentos |
| `neighbors(nodo)` | el subgrafo alrededor de un nodo, en JSON o en Mermaid |
| `communities()` | grupos de código que trabajan juntos, con sus unidades y requisitos |

Un requisito de un módulo cuyos derivados de otro módulo tienen código cuenta como
implementado a través de ellos: un SSS lo está si lo están los SRS que lo satisfacen. Los
enlaces dentro de un mismo módulo (referencias, refinamientos) no cuentan como derivación.

`atlas matrix SRS_TMS` escribe la matriz en `<workspace>/graph/matrix-SRS_TMS.csv` (para una
hoja de cálculo) y `.md`, y muestra el resumen y los huecos; `atlas matrix "*"` la hace con
todos los requisitos. Con las muestras:

```
40 requirements: 4 with code, 1 with tests, 40 in documents.
without code (36): SRS-0005, SRS-0006, …
```

La comparación de cifras es el primer paso de la verificación de requisitos con la
documentación: el texto de DOORS de SRS-0004 dice 320 ms y el del PDF de ejemplo, 401 ms.
La fase 5 añade un paso con el modelo que juzga el resto de incongruencias.

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

## Servidor MCP

`atlas serve` arranca un servidor MCP por stdio con las herramientas de documentos,
requisitos y código, para OpenCode, VS Code o cualquier otro cliente MCP. Sustituye a los
dos servidores anteriores (`doc_rag.py mcp` y `ada_graph_mcp.py`). Sus herramientas son los
plugins `tool` que usarán los agentes ADK, con los mismos nombres:

| Grupo | Herramientas | Clase |
|---|---|---|
| Documentos y requisitos | `list_documents`, `search`, `get_section`, `get_requirement`, `ask_docs` | `atlas.index.DocTools` |
| Grafo y trazabilidad | `graph_stats`, `find_symbol`, `search_code_docs`, `get_node`, `callers`, `callees`, `impact`, `call_chain`, `read_source`, `unit_info`, `type_users`, `variable_users`, `trace`, `trace_code`, `matrix`, `gaps`, `list_requirements`, `untraced`, `neighbors`, `communities` | `atlas.graph.GraphTools` |
| Ficheros de código | `list_files`, `grep`, `read_file`, `edit_file`, `write_file` | `atlas.code.CodeTools` |
| Agentes | `build_context`, `verify_requirement`, `ask_atlas` | `atlas.agents.tools.AgentTools` |

Cambios respecto a los servidores anteriores:
- Tres herramientas cambian de nombre para que no se confundan:
  - `search_docs` de `doc_rag.py` es `search`;
  - `search_docs` de `ada_graph_mcp.py` (busca en los comentarios del código) es
    `search_code_docs`;
  - `requirement` (el código que cita un requisito) es `trace`, que ahora da también DOORS,
    documentos y pruebas.
- `get_requirement` devuelve también el texto oficial de DOORS, con su módulo, atributos,
  padre y enlaces. Si el ID no existe, sugiere IDs parecidos.
- `read_file` es el de `CodeTools`, que solo lee dentro de `[sources.code] roots`. El de
  `ada_graph_mcp.py` leía cualquier fichero.
- `get_section` acepta el ID que da `search` (`"004"`) y también el número o el título que
  la sección tiene en el documento (`"3"`, `"2.1"`, `"Logging"`). Si el número es el de un
  capítulo repartido en varias secciones, devuelve la lista de esas secciones.
- Todas responden con un diccionario JSON. Un problema llega como una respuesta con `error`
  (y a menudo con sugerencias), no como un fallo.

Qué herramientas ofrece el servidor:
- Con `[mcp] tools = []`, que es el valor por defecto, ofrece las de las fuentes
  configuradas:
  - con `[[sources.docs]]`, las de documentos;
  - con solo `[sources.doors]`, `get_requirement`, y `search` si `index` está entre los
    pasos;
  - con `[sources.code]`, las del grafo y `read_file`;
  - sin código pero con DOORS o documentos, las de trazabilidad (`trace`, `matrix`, `gaps`,
    `list_requirements` y `neighbors`), si `graph` está entre los pasos;
  - con el grafo, `build_context`.
- `verify_requirement` y `ask_atlas` necesitan el modelo (y `ask_atlas`, también ADK): solo
  se ofrecen si están en la lista.
- Con una lista, ofrece exactamente esas herramientas.
- `edit_file` y `write_file` solo se ofrecen si están en la lista: atlas aporta conocimiento
  y el editor edita. El servidor las marca como herramientas que escriben, y el cliente
  puede pedir confirmación antes de usarlas.
- `[plugins] disabled = ["tool:ask_docs"]` quita una herramienta, y `[mcp] enabled = false`
  apaga el servidor.

El servidor responde con lo que hay en el workspace. Puedes lanzar `atlas run` con el
servidor arrancado: el grafo y los vectores se vuelven a leer cuando cambian, sin
reiniciarlo.

Las rutas de `atlas.toml` son relativas a la carpeta desde la que se lanza atlas, así que el
cliente tiene que arrancar el servidor en la carpeta del proyecto (`cwd`). El servidor
arranca en medio segundo y escribe su registro por stderr, porque stdout es el canal del
protocolo.

OpenCode (`opencode.json` en la carpeta del proyecto):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "atlas": {
      "type": "local",
      "command": ["/ruta/al/proyecto/.venv/bin/atlas", "serve", "atlas.toml"],
      "cwd": ".",
      "enabled": true
    }
  }
}
```

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "atlas": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/bin/atlas",
      "args": ["serve", "atlas.toml"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

En Windows, el ejecutable es `.venv\Scripts\atlas.exe`. Si `atlas.toml` lee secretos del
entorno (`${GW_KEY}`), pásalos con `environment` en OpenCode o con `env` o `envFile` en
VS Code.

Las herramientas también se usan sin MCP:

```python
from atlas.config import load_config
from atlas.graph import GraphTools
from atlas.index import DocTools
cfg = load_config("atlas.dev.toml")
DocTools.from_config(cfg).get_requirement("SRS-0004")    # DOORS y documentos
GraphTools.from_config(cfg).trace("SRS-0004")            # DOORS, documentos, código y pruebas
```

## Agentes

Los agentes se definen en ficheros Markdown, como los subagentes de Claude Code. Cada fichero
es un agente: una cabecera YAML y, en el cuerpo, sus instrucciones.

```markdown
---
name: tracer
description: Traza requisitos y dice qué falta.
tools: [trace, gaps, build_context]
model: gemma4:12b          # opcional: [agents] model, o si no [llm] model
max_steps: 6               # opcional: llamadas al modelo como mucho en una respuesta
reasoning_effort: none     # opcional: "none" apaga el razonamiento
temperature: 0.1           # opcional
---
You trace requirements of an engineering project...
```

Vienen de serie, en `atlas/agents/builtin/`, cuatro agentes:
- `assistant`, con herramientas de documentos, DOORS y código;
- `requirement_verifier`, `impact_analyst` y `coverage_auditor`, sin herramientas: redactan
  el paso final de un flujo fijo.

Los del proyecto van en las carpetas de `[agents] dirs` (`agents/` junto a `atlas.toml`, por
defecto). Un fichero con el nombre de uno de serie lo sustituye. `[agents.<nombre>]` en el
TOML retoca un campo sin tocar el fichero (`max_steps = 10`). `atlas agents` los lista.

Un agente se ejecuta con ADK sobre `AtlasLlm`, un modelo de ADK que usa el cliente de
`atlas.llm`: el mismo proxy, TLS, límite de peticiones y reintentos que el resto, sin
LiteLLM. Sus herramientas son los plugins `tool` con esos nombres, las mismas funciones que
ofrece `atlas serve`.

```bash
atlas agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml
ATLAS_CONFIG=atlas.dev.toml adk web examples/adk     # las llamadas paso a paso, en el navegador
```

Lo que viene además de los agentes:

| Pieza | Qué hace | Modelo |
|---|---|---|
| `build_context` | todo lo de un requisito, un elemento de código o un paquete en un Markdown de como mucho 4000 tokens: requisitos con su texto, firmas, llamadores, pruebas, documentos y código. Es una herramienta de `atlas serve` | no |
| `atlas verify` | cada requisito con documentación contra los documentos: veredicto e incongruencias (valor, condición, contradicción, alcance, sin definir) con las palabras de cada texto y su fuente. También es la herramienta `verify_requirement` | `requirement_verifier` |
| `atlas impact` | informe de lo que afecta un cambio de código o de un requisito | `impact_analyst` |
| `atlas coverage` | informe de los huecos de trazabilidad de un módulo | `coverage_auditor` |
| `ask_atlas` | el asistente como herramienta de `atlas serve`: una llamada para el cliente | `assistant` |

En la verificación y en los informes, el grafo encuentra los datos sin modelo, y el agente
solo juzga o redacta. Los informes van a `<workspace>/reports/` y
guardan, tras el texto del modelo, los datos de los que sale. La verificación guarda sus
juicios en caché: verificar otra vez solo pregunta por lo que ha cambiado.

Con `gemma4:12b`, `atlas verify SRS-0001` encuentra que DOORS dice 499 ms y la
especificación 301 ms, y cita las dos fuentes (35 s). `atlas impact Logger.Log` tarda
37 s y `atlas coverage SRS_TMS`, 46 s.

`atlas eval` hace las preguntas de los conjuntos de `[agents.eval] sets` (YAML) y las puntúa
sin modelo. Un caso está bien si la respuesta contiene los textos esperados (IDs,
identificadores, valores) y el agente ha llamado a las herramientas esperadas.
`examples/eval/assistant.yaml` tiene 24 casos sobre las muestras. Con `gemma4:12b` sin
razonamiento, el asistente acertó 23 de 24 (96 %), con una mediana de 11 s por pregunta. El
fallo dijo bien qué faltaba por definir en las alarmas, pero contestó en español y sin el
literal «TBD» que pedía el caso.

```toml
[agents.eval]
sets = ["examples/eval/assistant.yaml"]
runs = 3                   # veces que se hace cada pregunta: los modelos no responden siempre igual
```

