# AtlasLM — esqueleto de desarrollo

Base de conocimiento de ingeniería (documentos, requisitos de DOORS, código y grafo)
configurable por fichero, importable como librería y orquestada con Google ADK. Este
repositorio es el **esqueleto de la fase 0 del plan** (`legacy/PLAN.md`): motor
incremental, configuración validada, sistema de plugins, lector de DOORS desde base de
datos, analizador Ada, datos de ejemplo, servidor falso y pruebas. Los scripts anteriores
están en `legacy/` y se migran módulo a módulo en la fase 1.

## Puesta en marcha en un Mac (Apple Silicon)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[docs,code,mcp,dev]"      # añade ",agents" cuando empieces con ADK
python examples/make_samples.py             # datos de ejemplo (PDFs, Ada, doors.sqlite)
python -m pytest -q                         # 17 pruebas sin modelo (+1 de agentes si Ollama está en marcha)
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
python -m atlas.cli plugins  atlas.dev.toml   # analizadores, adaptadores y extractores disponibles
python -m atlas.cli check    atlas.dev.toml   # conexión con el modelo
```

Toda la configuración está en el TOML; la línea de comandos solo elige acción y fichero.
Con `pip install -e .` el comando `atlas` queda disponible directamente.

Prueba el motor sin modelo:

```bash
python tests/fakes/fake_api.py --port 8770 &      # en otra terminal
python -m atlas.cli run atlas.test.toml           # primera vez: procesa todo
python -m atlas.cli run atlas.test.toml           # segunda vez: todo "al día", 0 peticiones
# cambia un .adb o un requisito en examples/samples/doors.sqlite y vuelve a ejecutar:
# solo se reprocesa esa unidad
```

## Estructura

```
atlas/
├── config.py       carga y validación de atlas.toml, ${VAR}, huellas por sección
├── plugins.py      interfaces (CodeAnalyzer, RequirementsAdapter, DocumentExtractor, Tool) y registro
├── pipeline/       motor incremental: Step, Ledger (SQLite), Runner, cuarentena, poda, bloqueo
├── store.py        atlas.db: requisitos, enlaces, unidades de código
├── llm/            connection.py: clientes OpenAI, proxy, TLS, límites, reintentos
├── doors/          adaptador "database": consultas y convención configurables, diff
├── code/           analizador "ada" (envuelve legacy/ada_graph.py)
├── docs/           extractores "markitdown" y "text" (la estructura y el análisis siguen en legacy/)
├── graph/ index/ agents/ mcp/    pendientes de migración (fases 1, 4 y 5 del plan)
└── cli.py          run | status | plugins | check
examples/make_samples.py    genera examples/samples/ (PDFs con y sin marcadores, Ada, doors.sqlite)
tests/                      pytest; tests/fakes/fake_api.py es el servidor OpenAI falso y
                            test_agents_ollama.py, la prueba de agentes con Ollama
legacy/                     scripts actuales + README y PLAN del proyecto
```

## Idioma

El código está en inglés: nombres, comentarios, docstrings y mensajes de la línea de
comandos. La documentación está en español: este README, `legacy/PLAN.md`,
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

## DOORS desde una base de datos

`[sources.doors.database]` lleva las consultas SQL (módulos, requisitos, enlaces e
instantánea) con el parámetro `:module`, y `[sources.doors.convention]` el nombre de las
columnas, el patrón de los IDs, la normalización, las columnas de enlace y la expresión
regular de referencias dentro del texto. Ver `atlas.dev.toml` (SQLite de ejemplo) y
`atlas.toml` (ODBC hacia SQL Server). Nada de esto está en el código.
