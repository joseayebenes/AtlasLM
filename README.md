# AtlasLM

Base de conocimiento de ingeniería: documentos, requisitos de DOORS, código Ada y el grafo
que los une, configurable con un fichero, importable como librería y al servicio de
agentes (Google ADK) y de editores (OpenCode, VS Code) por MCP.

- **Procesa una vez.** Un motor incremental y recuperable analiza cada documento, módulo de
  requisitos y fichero de código solo cuando cambia, y sigue donde se quedó tras un corte.
- **Determinista primero.** Estructura, grafo, trazabilidad y búsqueda funcionan sin
  modelo; el modelo resume, responde y juzga.
- **Trazabilidad.** De cualquier requisito, en una llamada: su texto oficial, los documentos
  que lo citan (y si dicen otra cifra), el código que lo implementa y las pruebas que lo
  cubren. Y a la inversa, el impacto de un cambio.

Versión 0.2.0: las fases 0 a 6 del plan (`legacy/PLAN.md`) y, desde la 0.1.0, agentes que
escriben y prueban código, el código en el índice, Python, OpenRouter para comparar modelos y
preguntas amplias. Los cambios de cada versión están en el [CHANGELOG](CHANGELOG.md).

## Documentación

| Documento | Para qué |
|---|---|
| [Instalación](docs/instalacion.md) | instalar en un puesto nuevo, con o sin red; preparar la entrega |
| [Configuración](docs/configuracion.md) | cada sección y clave de `atlas.toml` |
| [Seguridad](docs/seguridad.md) | qué sale del puesto, secretos, qué puede hacer cada pieza |
| [OpenRouter](docs/openrouter.md) | probar y comparar modelos de chat con las muestras |
| [atlas.pipeline](atlas/pipeline/README.md) | el motor incremental, el paralelismo y los perfiles de tiempo |
| [atlas.llm](atlas/llm/README.md) | la conexión con los modelos: proxy, TLS, límites, JSON |
| [atlas.docs](atlas/docs/README.md) | documentos: extracción, secciones, análisis, Markdown |
| [atlas.index](atlas/index/README.md) | índice, búsqueda híbrida y respuestas citadas |
| [atlas.doors](atlas/doors/README.md) | requisitos: la base de intercambio y sus adaptadores |
| [atlas.code](atlas/code/README.md) | análisis de Ada y herramientas de ficheros para agentes |
| [atlas.graph](atlas/graph/README.md) | el grafo unificado y las consultas de trazabilidad |
| [atlas.mcp](atlas/mcp/README.md) | el servidor MCP y su configuración en el editor |
| [atlas.agents](atlas/agents/README.md) | agentes en Markdown, verificación, informes, evaluación |
| [tools/doors_export](tools/doors_export/README.md) | el exportador de DOORS a la base de intercambio |
| [Plan del proyecto](legacy/PLAN.md) | objetivos, arquitectura, fases y su estado |

## Puesta en marcha para desarrollar

En un Mac (para un puesto de usuario, la [guía de instalación](docs/instalacion.md)):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]" -c constraints.txt    # o: make install
python examples/make_samples.py                    # datos de ejemplo (PDFs, Ada, requisitos)
python -m pytest -q                                # 203 pruebas sin modelo (+7 de agentes con Ollama)
```

### Niveles de modelo

| Nivel | Para qué | Configuración |
|---|---|---|
| **Servidor falso** (`tests/fakes/fake_api.py`) | pruebas automáticas y desarrollo del motor: determinista, instantáneo, con fallos inyectables (409, 401, respuestas cortadas, prosa, modelo que razona, lentitud) | `atlas.test.toml` |
| **Modelo local** (Ollama o LM Studio) | probar la calidad real de análisis, búsqueda y agentes sin cupo ni red | `atlas.dev.toml` |
| **OpenRouter** | comparar modelos de chat sobre las muestras, también el del gateway, sin descargarlos ([guía](docs/openrouter.md)) | `atlas.openrouter.toml` |
| **Gateway corporativo** | producción | `atlas.toml` |

Todos usan la misma API compatible con OpenAI: cambiar de nivel es cambiar `base_url`.

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

Comprueba la conexión antes de nada: `atlas check atlas.dev.toml`. `gemma4` razona por
defecto, y ese razonamiento cuenta dentro de `max_tokens` y multiplica los tiempos:
`atlas.dev.toml` lo apaga con `[llm] reasoning_effort = "none"`. Los agentes con Ollama y
sus pruebas (`make test-ollama`) están en [atlas.agents](atlas/agents/README.md).

## Comandos

```bash
atlas init     [atlas.toml]   # una plantilla comentada de atlas.toml
atlas run      atlas.dev.toml # procesamiento incremental y reanudable
atlas status   atlas.dev.toml # libro de registro, datos y perfil de tiempos
atlas sections atlas.dev.toml # secciones de cada documento, sin llamar al modelo
atlas graph    atlas.dev.toml # informe del grafo (código, DOORS, documentos) y de sus huecos
atlas trace SRS-0004 atlas.dev.toml   # todo sobre un requisito: DOORS, documentos, código, pruebas
atlas matrix SRS_TMS atlas.dev.toml   # matriz de trazabilidad de un módulo, en CSV y Markdown
atlas search "track deletion latency" atlas.dev.toml   # búsqueda en documentos, requisitos y código
atlas ask "¿Cuánto tarda en borrarse una traza?" atlas.dev.toml   # respuesta con citas
atlas ask "Resume los requisitos de SRS_TMS" atlas.dev.toml   # una pregunta amplia lee el módulo entero
atlas search "deletion" atlas.dev.toml --kind requirement --k 5   # solo requisitos, 5 resultados
atlas plugins  atlas.dev.toml # analizadores, adaptadores y extractores disponibles
atlas agents   atlas.dev.toml # agentes (ficheros Markdown): de serie y del proyecto
atlas agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml   # pregunta a un agente
atlas verify SRS_TMS atlas.dev.toml    # cada requisito contra la documentación, con el modelo
atlas impact Logger.Log atlas.dev.toml # informe de lo que afecta un cambio
atlas coverage SRS_TMS atlas.dev.toml  # informe de los huecos de trazabilidad
atlas link SRS_TMS atlas.dev.toml      # código que puede implementar los requisitos sin código (propuestas)
atlas comply SRS_TMS atlas.dev.toml    # si el código de cada requisito hace todo lo que pide, obligación a obligación
atlas tests STP_SOCKET atlas.toml      # los procedimientos de prueba como tests, paso a paso, sin modelo
atlas develop tasks/server.toml atlas.toml   # un agente escribe el código encargo a encargo; los tests lo juzgan
atlas eval     atlas.dev.toml # evaluación del índice, el grafo y los agentes con respuestas conocidas
atlas check    atlas.dev.toml # conexión con el modelo
atlas probe    atlas.dev.toml # el modelo como agente: llama a una herramienta, usa el resultado y su razonamiento
atlas reference atlas.toml    # los manuales y la biblioteca para search_reference (también en la primera búsqueda)
atlas serve    atlas.dev.toml # servidor MCP (stdio) para OpenCode, VS Code...
atlas --version
```

Toda la configuración está en el TOML; la línea de comandos solo elige acción y fichero.
Sin instalar, `python -m atlas.cli` en vez de `atlas`.

Prueba el motor sin modelo:

```bash
python tests/fakes/fake_api.py --port 8770 &      # en otra terminal
atlas run atlas.test.toml           # primera vez: DOORS, Ada, grafo, documentos e índice
atlas run atlas.test.toml           # segunda vez: todo "al día", 0 peticiones
atlas status atlas.test.toml        # y el perfil de tiempos de cada paso
atlas graph atlas.test.toml
atlas search "alarm latency" atlas.test.toml
# cambia un .adb, un requisito en examples/samples/requirements.sqlite o hmi_manual.md y
# vuelve a ejecutar: solo se reprocesa esa unidad (y del documento, solo las secciones
# cambiadas y los textos nuevos del índice)
```

Para medir el motor con un proyecto grande, `make large` genera uno sintético en
`examples/large/`: 20 PDFs (260 páginas), 660 ficheros Ada y 5500 requisitos. Después lo
procesa tres veces con el servidor falso (ver [atlas.pipeline](atlas/pipeline/README.md)):
- la primera ejecución tarda unos 7 s;
- la segunda, sin cambios, 0,1 s y sin ninguna petición;
- tras cambiar un plazo en una página de un PDF, 0,9 s y una sola petición al modelo.

## Estructura

```
atlas/
├── config.py       carga y validación de atlas.toml, ${VAR}, secretos, huellas por sección
├── plugins.py      interfaces (CodeAnalyzer, RequirementsAdapter, DocumentExtractor, Tool) y registro
├── db.py           conexión a atlas.db, versión del esquema y migraciones
├── store.py        atlas.db: requisitos, enlaces, unidades de código, documentos
├── pipeline/       motor incremental: Step, Ledger, Runner; paralelismo y perfiles de tiempo
├── llm/            clientes OpenAI con proxy, TLS, límites y reintentos; respuestas JSON
├── doors/          requisitos: base de intercambio y adaptadores "exchange" y "database"
├── code/           analizador Ada y herramientas de ficheros para agentes de código
├── docs/           documentos: extractores, estructura, análisis y Markdown
├── index/          índice: registros, embeddings con caché, FTS5, búsqueda híbrida, respuestas
├── graph/          grafo de código, DOORS y documentos; trazabilidad; GraphML, JSON y Mermaid
├── mcp/            servidor MCP con las herramientas de documentos, grafo y ficheros
├── agents/         agentes en Markdown (builtin/), AtlasLlm, build_context, verificación,
│                   informes y evaluación
├── templates/      la plantilla de atlas init
└── cli.py          las acciones de la línea de comandos
docs/                       instalación, configuración y seguridad
examples/make_samples.py    genera examples/samples/ (PDFs con y sin marcadores, Ada, requisitos)
examples/library/           ejemplos de uso como librería (pipeline, documentos, trazabilidad, búsqueda)
examples/large_project.py   proyecto grande sintético y medición del motor (make large)
examples/eval/              conjuntos de evaluación de los agentes (atlas eval)
examples/adk/atlas_agent/   un agente de atlas para adk web y adk run
tools/doors_export/         exportador de DOORS a la base de intercambio (descarga + publicación)
tools/check_install.py      instalación limpia desde el wheelhouse (make check-install)
tools/constraints.py        genera constraints.txt
tests/                      pytest; tests/fakes/fake_api.py es el servidor OpenAI falso
legacy/                     los scripts de partida y el plan del proyecto
```

## Idioma

El código está en inglés: nombres, comentarios, docstrings, mensajes de la línea de
comandos y los ficheros que genera (los Markdown de cada documento; el contenido de los
resúmenes sigue `lang`). La documentación está en español: los README, `docs/`, el
CHANGELOG, `legacy/PLAN.md` y los comentarios de los TOML de ejemplo. `legacy/` conserva el
español mientras siga existiendo.

## Añadir o quitar piezas

Todo lo que varía de un proyecto a otro es un plugin con nombre, que se elige en
`atlas.toml`:

- **Otro lenguaje**: una clase `CodeAnalyzer` registrada con `register("code_analyzer", "cpp", Clase)`
  en un módulo propio; actívalo con `[sources.code] languages = ["ada", "cpp"]` y declara el
  módulo en `[plugins] modules` (o como entry point `atlas.plugins` en su `pyproject`).
- **Otra fuente de requisitos**: una clase `RequirementsAdapter` (`modules`, `snapshot`,
  `requirements`, `links`) y `adapter = "nombre"` en `[sources.doors]`.
- **Otro formato de documento**: una clase `DocumentExtractor` por extensión.
- **Otra herramienta para agentes y MCP**: un plugin `tool`.
- **Quitar algo**: `[plugins] disabled = ["code_analyzer:ada"]`, o no activarlo en la fuente.

Un plugin ejecuta código al cargarse: tiene la misma confianza que el propio `atlas.toml`
(ver [seguridad](docs/seguridad.md)).
