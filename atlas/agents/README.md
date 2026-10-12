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
| `link.py` | las propuestas de enlaces: el código que puede implementar los requisitos sin código |
| `comply.py` | el cumplimiento del código con un requisito, obligación a obligación |
| `reports.py` | informes de impacto y de cobertura: el grafo encuentra, un agente redacta |
| `tools.py` | `build_context`, `verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` como plugins `tool` |
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

Vienen de serie, en `builtin/`, seis agentes:
- `assistant`, con herramientas de documentos, DOORS y código, todas de lectura;
- `requirement_verifier`, `req_linker`, `compliance_checker`, `impact_analyst` y
  `coverage_auditor`, sin herramientas: juzgan o redactan el paso final de un flujo fijo.

Los del proyecto van en las carpetas de `[agents] dirs` (`agents/` junto a `atlas.toml`, por
defecto). Un fichero con el nombre de uno de serie lo sustituye. `[agents.<nombre>]` en el
TOML retoca un campo sin tocar el fichero (`max_steps = 10`). `atlas agents` los lista.

Un agente se ejecuta con ADK sobre `AtlasLlm`, que usa el cliente de `atlas.llm`: el mismo
proxy, TLS, límite de peticiones y reintentos que el resto, sin LiteLLM. Sus herramientas
son los plugins `tool` con esos nombres, las mismas funciones que ofrece `atlas serve`.
La misma llamada con los mismos argumentos, la tercera vez seguida, no se ejecuta: el modelo
recibe un error que le pide hacer otra cosa. `gemma-4-26b` llegó a escribir 17 veces seguidas
el mismo fichero tras un error de compilación que no entendía, hasta agotar `max_steps`; y
`write_file` avisa cuando el contenido no cambia.

```bash
atlas agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml
ATLAS_CONFIG=atlas.dev.toml adk web examples/adk     # las llamadas paso a paso, en el navegador
```

## Flujos fijos

| Pieza | Qué hace | Modelo |
|---|---|---|
| `build_context` | todo lo de un requisito, un elemento de código o un paquete en un Markdown de como mucho 4000 tokens: requisitos con su texto, firmas, llamadores, pruebas, documentos y código. Es una herramienta de `atlas serve` | no |
| `atlas verify` | cada requisito con documentación contra los documentos: veredicto e incongruencias (valor, condición, contradicción, alcance, sin definir) con las palabras de cada texto y su fuente. También es la herramienta `verify_requirement` | `requirement_verifier` |
| `atlas comply` | si el código que implementa cada requisito hace todo lo que pide, obligación a obligación: cumplida, no cumplida, en parte o imposible de saber leyendo el código. También es la herramienta `check_compliance` | `compliance_checker` |
| `atlas impact` | informe de lo que afecta un cambio de código o de un requisito | `impact_analyst` |
| `atlas coverage` | informe de los huecos de trazabilidad de un módulo | `coverage_auditor` |
| `atlas link` | para cada requisito sin código, el código que puede implementarlo, con confianza (alta, media, baja), motivo y la etiqueta `Req:` que habría que añadir y dónde. No toca el código. También es la herramienta `link_requirement` | `req_linker` |
| `ask_atlas` | el asistente como herramienta de `atlas serve`: una llamada para el cliente | `assistant` |

En la verificación y en los informes, el grafo encuentra los datos sin modelo, y el agente
solo juzga o redacta. Los informes van a `<workspace>/reports/` y guardan, tras el texto
del modelo, los datos de los que sale. La verificación guarda sus juicios en caché:
verificar otra vez solo pregunta por lo que ha cambiado, y verifica varios requisitos a la
vez (`[pipeline] workers`).

## Cumplimiento del código

`atlas comply ID|MÓDULO` (o `*`) juzga, para cada requisito con código, si ese código hace
todo lo que el requisito pide. Sin modelo reúne:
- el texto oficial, o el del documento que lo declara si no está en DOORS;
- el código que lo cita con `Req:`, con su comentario y su fuente;
- lo que ese código llama, con sus comentarios;
- las pruebas que lo verifican o lo cubren.

El agente `compliance_checker` divide el requisito en obligaciones (cada «shall», condición,
valor, límite y unidad) y marca cada una:
- `met`: el código la cumple, y cita las líneas que lo hacen;
- `not_met`: no la cumple;
- `partial`: la cumple solo en parte;
- `cannot_tell`: no se puede saber leyendo el código (tiempos, rendimiento, código que no se
  ve).

El veredicto no lo decide el modelo: sale de las obligaciones. Es `does_not_comply` si alguna
no se cumple o se cumple en parte, `cannot_tell` si queda alguna sin decidir y `complies` si
se cumplen todas. Un requisito sin código queda como `no_code` sin preguntar, y los juicios
se guardan en caché. El informe (`reports/comply-<objetivo>.md`) da cada obligación con su
marca y su prueba.

Es una ayuda para la inspección del código, no una prueba de que funciona: lo que leer el
código no puede decidir se deja a las pruebas, y un modelo pequeño puede dar por buena una
obligación que no lo es.

En un experimento con `gemma4:12b`, 6 casos de respuesta conocida, 3 veces cada uno:
- encontró las 3 veces un error de límite (`Speed < 300.0` donde el requisito dice «300
  inclusive»);
- vio las 3 veces que poner a 0 el Id sin poner a 0 el azimut cumple solo en parte;
- marcó «within 320 ms» como imposible de saber leyendo el código;
- en «keep logs for 30 days» dio por cumplido «keep logs» con un `Put_Line` a la salida
  estándar, y dejó los 30 días como imposibles de saber: un juicio generoso.

Con `atlas comply` sobre un `Classify` de cuatro ramas (prueba
`test_compliance_checker_finds_a_wrong_boundary`), 3 de 3 ejecuciones: `does_not_comply`
con `Speed < 300.0` y `complies` con `Speed <= 300.0`, unos 40 s cada una.

## Propuestas de enlaces

`atlas link MÓDULO` (o un ID, o `*`) recorre los requisitos sin código y, para cada uno,
busca candidatos sin modelo:
- el código que mencionan las secciones que declaran el requisito;
- el código de los requisitos enlazados con él (enlaces de DOORS y referencias en el texto);
- lo que encuentra la búsqueda en el código indexado (`code_doc`) con su texto, o con el del
  documento que lo declara si no está en DOORS.

Como mucho seis candidatos, cada uno con lo que lo trajo, su comentario, los requisitos que
ya cita y el principio de su código. El agente `req_linker` elige los que implementan el
requisito, con confianza y motivo, o ninguno. Lo que elija fuera de los candidatos se
descarta, y un requisito sin candidatos no se pregunta. Los juicios se guardan en caché.

El informe (`reports/link-<objetivo>.md` y `.json`) dice, para cada propuesta, la etiqueta
que hay que añadir (`-- Req: SRS-0150`) y dónde: el comentario de la declaración en la
especificación, o el del cuerpo si no la hay. **No se escribe nada en el código**: lo decide
una persona, y el siguiente `atlas run` lee la etiqueta. Es deliberado: un enlace inventado
cuenta en la matriz igual que uno revisado.

Con `gemma4:12b`, en cuatro ejecuciones:
- «The software shall remove a track from the track database when the operator deletes it»:
  propone `tracks.delete_track` con confianza alta («pone a 0 el ID de la traza»).
- «The HMI shall show the speed of each track in knots»: no propone nada.
- Los 36 requisitos de `SRS_TMS` sin código, cuyos textos sintéticos no tienen relación con
  el código de las muestras: ninguna propuesta. Para SRS-0005 el candidato era
  `tracks.delete_track`, que implementa SRS-0004, al que refina; el agente lo descartó porque
  no procesa el evento que pide.

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

`examples/eval/retrieval.yaml` tiene 20 búsquedas (IDs exactos, palabras del texto,
paráfrasis, preguntas en español, secciones y código) y 10 consultas del grafo sobre las
muestras. Con `embeddinggemma`:

| Búsqueda | acierto@1 | @3 | @5 | @10 | MRR |
|---|---|---|---|---|---|
| híbrida | 75 % | 90 % | 100 % | 100 % | 0,84 |
| léxica, sin modelo | 55 % | 70 % | 90 % | 100 % | 0,68 |

El grafo acierta 10 de 10. Los vectores se notan en las paráfrasis, en las preguntas en
español y en el código: «how long does the HMI retain its log files» sale primera en la
híbrida y cuarta en la léxica, y «type for the hours of the day» (`tracks.hour_range`),
primera y cuarta. Las muestras son sintéticas y repetitivas, así que este conjunto sirve de
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
`atlas.dev.toml` (`[llm] base_url`, `[agents] model`). Son siete pruebas:
- un agente con dos herramientas propias sobre las muestras (`get_requirement` y una `trace`
  que busca las etiquetas `Req:`): usa las dos, responde con el requisito y los ficheros
  correctos y no se inventa ficheros;
- un cambio preciso en el Ada con las herramientas de ficheros;
- un agente con las herramientas de `atlas serve` por MCP;
- el asistente de serie, como lo ejecuta `atlas agent assistant`;
- la verificación de un requisito con la documentación;
- las propuestas de enlaces del `req_linker`, una que debe proponer y otra que no;
- el cumplimiento de un `Classify` con un error de límite y sin él.

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
