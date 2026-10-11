# atlas.agents — agentes ADK

Agentes de Google ADK definidos en ficheros Markdown, y lo que se construye con ellos:
paquetes de contexto, verificación de requisitos, informes y evaluación. Necesita el extra
`[agents]` (google-adk 2.11), salvo `build_context` y la lectura de las definiciones.

| Fichero | Qué tiene |
|---|---|
| `definitions.py` | `AgentDef` y `load_definitions`: los agentes de `builtin/` y de `[agents] dirs`, con los retoques de `[agents.<nombre>]` (sin ADK) |
| `model.py` | `AtlasLlm`: un modelo de ADK sobre el cliente de `atlas.llm` (proxy, TLS, límites, reintentos) |
| `runtime.py` | `build_agent` y `run_agent`: el agente de un fichero como `LlmAgent`, con sus herramientas y como mucho `max_steps` llamadas al modelo |
| `context.py` | `build_context`: un paquete en Markdown sobre un requisito, un elemento de código o un paquete |
| `verify.py` | la verificación de los requisitos con la documentación |
| `reports.py` | informes de impacto y de cobertura: el grafo encuentra, un agente redacta |
| `tools.py` | `build_context`, `verify_requirement` y `ask_atlas` como plugins `tool` |
| `evaluate.py` | `atlas eval`: preguntas a los agentes, búsquedas y consultas del grafo con respuesta conocida, puntuadas sin modelo |

## Agentes en ficheros Markdown

Como los subagentes de Claude Code: cada fichero es un agente, con una cabecera YAML y, en
el cuerpo, sus instrucciones.

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

Vienen de serie, en `builtin/`, cuatro agentes:
- `assistant`, con herramientas de documentos, DOORS y código, todas de lectura;
- `requirement_verifier`, `impact_analyst` y `coverage_auditor`, sin herramientas: redactan
  el paso final de un flujo fijo.

Los del proyecto van en las carpetas de `[agents] dirs` (`agents/` junto a `atlas.toml`, por
defecto). Un fichero con el nombre de uno de serie lo sustituye. `[agents.<nombre>]` en el
TOML retoca un campo sin tocar el fichero (`max_steps = 10`). `atlas agents` los lista.

Un agente se ejecuta con ADK sobre `AtlasLlm`, que usa el cliente de `atlas.llm`: el mismo
proxy, TLS, límite de peticiones y reintentos que el resto, sin LiteLLM. Sus herramientas
son los plugins `tool` con esos nombres, las mismas funciones que ofrece `atlas serve`.

```bash
atlas agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml
ATLAS_CONFIG=atlas.dev.toml adk web examples/adk     # las llamadas paso a paso, en el navegador
```

## Flujos fijos

| Pieza | Qué hace | Modelo |
|---|---|---|
| `build_context` | todo lo de un requisito, un elemento de código o un paquete en un Markdown de como mucho 4000 tokens: requisitos con su texto, firmas, llamadores, pruebas, documentos y código. Es una herramienta de `atlas serve` | no |
| `atlas verify` | cada requisito con documentación contra los documentos: veredicto e incongruencias (valor, condición, contradicción, alcance, sin definir) con las palabras de cada texto y su fuente. También es la herramienta `verify_requirement` | `requirement_verifier` |
| `atlas impact` | informe de lo que afecta un cambio de código o de un requisito | `impact_analyst` |
| `atlas coverage` | informe de los huecos de trazabilidad de un módulo | `coverage_auditor` |
| `ask_atlas` | el asistente como herramienta de `atlas serve`: una llamada para el cliente | `assistant` |

En la verificación y en los informes, el grafo encuentra los datos sin modelo, y el agente
solo juzga o redacta. Los informes van a `<workspace>/reports/` y guardan, tras el texto
del modelo, los datos de los que sale. La verificación guarda sus juicios en caché:
verificar otra vez solo pregunta por lo que ha cambiado, y verifica varios requisitos a la
vez (`[pipeline] workers`).

Con `gemma4:12b`, `atlas verify SRS-0001` encuentra que DOORS dice 499 ms y la
especificación 301 ms, y cita las dos fuentes (35 s). `atlas impact Logger.Log` tarda
37 s y `atlas coverage SRS_TMS`, 46 s.

## Evaluación

`atlas eval` ejecuta los conjuntos de `[agents.eval] sets` (YAML) y los puntúa sin modelo.
Un conjunto mezcla tres tipos de caso, según la clave que lleva la pregunta:

```yaml
- id: trace-srs-0004                 # un agente: necesita el modelo y el extra [agents]
  question: Which system requirement does SRS-0004 satisfy?
  expect: [SSS-0005]                 # textos que la respuesta debe contener
  tools: [trace]                     # opcional: herramientas que debe llamar
- id: logs-30-days                   # la búsqueda del índice
  search: how long does the HMI retain its log files
  expect: [30 days]
  kinds: [requirement]               # opcional, como document: dónde buscar
- id: graph-impact-log               # una consulta del grafo, sin modelo
  impact: Logger.Log                 # trace, trace_code, impact, find_symbol, search_code_docs
  expect: [tracks.delete_track, SRS-0004]
```

- **Agentes.** Un caso está bien si la respuesta contiene los textos esperados (IDs,
  identificadores, valores) y el agente ha llamado a las herramientas esperadas sin llegar a
  `max_steps`. `examples/eval/assistant.yaml` tiene 24 casos sobre las muestras. Con
  `gemma4:12b` sin razonamiento, el asistente acertó 23 de 24 (96 %), con una mediana de 9
  a 11 s por pregunta. El fallo dice bien qué falta por definir en las alarmas, pero contesta
  en español y sin el literal «TBD» que pide el caso.
- **Búsqueda.** Cada texto esperado se encuentra en la posición del primer resultado que lo
  contiene (en su ID, documento, título de sección o texto), y el caso, en la del último. De
  ahí salen el acierto@k (los casos con todo lo esperado entre los k primeros, para k = 1, 3,
  5 y 10) y el rango recíproco medio (MRR). Cada búsqueda se hace dos veces: híbrida (IDs,
  vectores y BM25, como `atlas search`) y léxica (IDs y BM25, sin ninguna petición), para ver
  qué aportan los vectores.
- **Grafo.** Un caso está bien si el resultado de la consulta contiene lo esperado. El grafo
  no ordena ni varía: o está o no está.

`examples/eval/retrieval.yaml` tiene 17 búsquedas (IDs exactos, palabras del texto,
paráfrasis, preguntas en español, secciones) y 10 consultas del grafo sobre las muestras. Con
`embeddinggemma`:

| Búsqueda | acierto@1 | @3 | @5 | @10 | MRR |
|---|---|---|---|---|---|
| híbrida | 76 % | 94 % | 100 % | 100 % | 0,86 |
| léxica, sin modelo | 65 % | 82 % | 94 % | 100 % | 0,76 |

El grafo acierta 10 de 10. Los vectores se notan en las paráfrasis y en las preguntas en
español: «how long does the HMI retain its log files» sale primera en la híbrida y tercera en
la léxica. Las muestras son sintéticas y repetitivas, así que este conjunto sirve de
referencia y para detectar regresiones; la cifra que vale es la de un conjunto escrito sobre
el proyecto real.

```toml
[agents.eval]
sets = ["examples/eval/assistant.yaml", "examples/eval/retrieval.yaml"]
runs = 3                   # veces que se hace cada pregunta a un agente: los modelos no responden siempre igual
```

Los resultados van a `<workspace>/reports/eval-<conjunto>.json`, en `agents` y `retrieval`.
Un conjunto con solo búsquedas y consultas del grafo no necesita ADK ni el modelo de chat;
la búsqueda híbrida sí necesita el de embeddings.

## Con Ollama

```bash
pip install -e ".[agents]"                       # google-adk
OLLAMA_CONTEXT_LENGTH=16384 ollama serve         # en otra terminal
make test-ollama                                 # o: python -m pytest -q -m ollama
```

`tests/test_agents_ollama.py` prueba los agentes contra la URL y el modelo de
`atlas.dev.toml` (`[llm] base_url`, `[agents] model`). Son cinco pruebas:
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
OpenCode o VS Code. Se hicieron cuatro preguntas, tres veces cada una: trazabilidad,
impacto, documentos y una sección.
- Sin razonamiento, el agente acertó 12 de 12, con una mediana de 14 s.
- Con razonamiento, acertó 9 de 12, con una mediana de 58 s. En las tres de trazabilidad no
  llamó a `trace` y se inventó los subprogramas.
- Con las 26 herramientas de la fase 4, sin razonamiento: 12 de 12, con una mediana de
  18 s. `trace` ya da los enlaces de DOORS, el código y las pruebas, así que basta con esa
  llamada.

## Como librería

```python
from atlas.agents.context import build_context
from atlas.agents.definitions import load_definitions
from atlas.agents.runtime import run_agent
from atlas.graph import KnowledgeGraph

print(sorted(load_definitions(cfg)))                                  # los agentes, sin ADK
print(build_context(KnowledgeGraph.from_config(cfg), "SRS-0004")["markdown"])   # sin modelo
res = run_agent("assistant", "Which tests verify SRS-0004?", cfg)     # con ADK y el modelo
print(res["answer"], [c["name"] for c in res["calls"]])
```

## Pruebas

`tests/test_agents.py`: ficheros Markdown y sus retoques, `AtlasLlm` con un cliente de pega,
`max_steps`, `build_context`, la verificación, los informes, `ask_atlas`, la evaluación de los
agentes y la app de `adk web`. `tests/test_eval.py`: la evaluación de la búsqueda y del grafo.
Con Ollama, `tests/test_agents_ollama.py`.
