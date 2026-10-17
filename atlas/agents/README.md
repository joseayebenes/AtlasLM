# atlas.agents — agentes ADK

Agentes de Google ADK definidos en ficheros Markdown, y lo que se construye con ellos:
paquetes de contexto, verificación de requisitos, informes y evaluación. Necesita el extra
`[agents]` (google-adk 2.11), salvo `build_context` y la lectura de las definiciones.

| Fichero | Qué tiene |
|---|---|
| `definitions.py` | `AgentDef` y `load_definitions`: los agentes de `builtin/` y de `[agents] dirs`, con los retoques de `[agents.<nombre>]` (sin ADK) |
| `model.py` | `AtlasLlm`: un modelo de ADK sobre el cliente de `atlas.llm` (proxy, TLS, límites, reintentos) |
| `runtime.py` | `build_agent` y `run_agent`: el agente de un fichero como `LlmAgent`, con sus herramientas y como mucho `max_steps` llamadas al modelo |
| `context.py` | `build_context`: un paquete en Markdown sobre un requisito, un elemento de código o un paquete; `similar_requirements`: requisitos parecidos que ya tienen código |
| `verify.py` | la verificación de los requisitos con la documentación |
| `link.py` | las propuestas de enlaces: el código que puede implementar los requisitos sin código |
| `comply.py` | el cumplimiento del código con un requisito, obligación a obligación |
| `reports.py` | informes de impacto y de cobertura: el grafo encuentra, un agente redacta |
| `tools.py` | `build_context`, `context_at`, `similar_requirements`, `verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` como plugins `tool` |
| `evaluate.py` | `atlas eval`: preguntas a los agentes, búsquedas y consultas del grafo con respuesta conocida, puntuadas sin modelo |
| `develop.py` | `atlas develop`: un agente escribe el código encargo a encargo y los tests lo juzgan; lo que rompe algo se deshace |

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
top_p: 0.95                # opcional: el muestreo de gemma 4 (temperatura 1,0, top_p 0,95, top_k 64)
top_k: 64                  # opcional: no es de la API de OpenAI, va en extra_body
keep_tool_results: 10      # opcional: resultados grandes de herramientas que se conservan enteros
stall_steps: 15            # opcional: llamadas sin cambiar el código que paran una ejecución
---
You trace requirements of an engineering project...
```

Vienen de serie, en `builtin/`, seis agentes:
- `assistant`, con herramientas de documentos, DOORS y código, todas de lectura; para una
  pregunta amplia (un resumen, todos los requisitos de algo) lista los documentos y módulos
  y llama a `ask_docs` con `whole`, que los lee enteros;
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
`write_file` avisa cuando el contenido no cambia. Un modelo que razona puede gastar todo
`max_tokens` pensando y no contestar nada; ADK tomaría esa vuelta vacía como la respuesta
final, así que se pregunta otra vez con el doble de `max_tokens`, como mucho dos veces. Cada
vuelta va con streaming si el servidor lo admite (`[llm] stream`, ver `atlas/llm`): los trozos
se juntan antes de que ADK vea la respuesta, así que las llamadas a herramientas son las
mismas. Una vuelta sin un trozo en `[llm.network] idle_timeout` (120 s) o que pasa de
`turn_timeout` (600 s) en total se corta, conexión incluida, y se pregunta otra vez, como mucho
dos veces; si el agente se cancela, su petición se corta en el acto. Antes la vuelta colgada
se abandonaba en un hilo y seguía viva en el gateway.

El razonamiento de cada vuelta vuelve al modelo en las siguientes, en el mensaje del asistente
que lleva las llamadas a herramientas (`[llm] reasoning_field`, `"reasoning"` por defecto; `""`
lo apaga). Lo pide la ficha de gemma 4: el razonamiento de los turnos con llamadas a
herramientas se queda en la conversación, y su plantilla de chat lo vuelve a poner si llega en
`reasoning` o `reasoning_content`. Antes se tiraba: en cada vuelta, el modelo tenía que deducir
otra vez qué estaba haciendo. `atlas probe` dice si un servidor lo aprovecha.

Con `keep_tool_results` (en el fichero del agente), la conversación no crece sin fin: una
lectura de un fichero que se ha vuelto a leer después se quita (su texto ya no es el del
fichero, y el modelo lo copiaba en `edit_file`), y también los resultados grandes de antes de
los últimos `keep_tool_results`, de cinco en cinco. En su lugar queda una nota con sus campos
cortos (`ok`, `file`, `passed`...). JetBrains vio que esto acierta como resumir con el modelo
y cuesta la mitad (*The Complexity Trap*, 2025). En la tanda 4, las vueltas de un arreglo
llevaban 55 000 tokens de entrada, casi todo lecturas viejas. Por defecto, 0: todo se queda.

Con `[llm] context_tokens`, además, cada vuelta tiene que caber en el contexto del modelo:
antes de enviarla se estiman sus tokens (caracteres / 4, con las herramientas) y, si pasan de
`context_tokens − max_tokens`, se dejan fuera resultados viejos de cinco en cinco, como con un
`keep_tool_results` menor, hasta que quepa. El registro lo dice: «turn of 12 300 tokens over a
context of 8000 (6000 for the input): 10 old tool results left out». Los cinco últimos se quedan
siempre.

`run_agent` devuelve también los tokens de entrada y salida de todas las vueltas y, cuando el
servidor lo dice (OpenRouter, `usage.cost`), su coste. Cada llamada de un agente a una herramienta queda en el
registro de uso del workspace (origen `agent`, con el nombre del agente; `[mcp] usage_log` y
`atlas usage`).

```bash
atlas agent assistant "¿Qué prueba verifica SRS-0004?" atlas.dev.toml
ATLAS_CONFIG=atlas.dev.toml adk web examples/adk     # las llamadas paso a paso, en el navegador
```

## Flujos fijos

| Pieza | Qué hace | Modelo |
|---|---|---|
| `build_context` | todo lo de un requisito, un elemento de código o un paquete en un Markdown de como mucho 4000 tokens: requisitos con su texto, firmas, llamadores, pruebas, documentos y código. Es una herramienta de `atlas serve` y la salida de `atlas trace` (con 20 000 tokens) | no |
| `context_at` | `build_context` del código de una línea de un fichero (`path`, `line`): lo que el editor tiene abierto. También vale `build_context("src/tracks.adb:12")` | no |
| `similar_requirements` | hasta `k` (3) requisitos parecidos a uno que ya tienen código, como ejemplo de cómo se implementa y se etiqueta uno en el proyecto: su texto, por qué se parece, el subprograma que lo implementa con su firma, la línea de su etiqueta `Req:` y, del primero, las 20 primeras líneas de su cuerpo. Primero los enlazados en DOORS (en los dos sentidos, y su padre) y los que declaran las mismas secciones; después, si hay índice, los que encuentra la búsqueda por su texto (ID y palabras, sin vectores). `build_context` de un requisito los trae en su último bloque, el primero que se recorta | no |
| `atlas verify` | cada requisito con documentación contra los documentos: veredicto e incongruencias (valor, condición, contradicción, alcance, sin definir) con las palabras de cada texto y su fuente. También es la herramienta `verify_requirement` | `requirement_verifier` |
| `atlas comply` | si el código que implementa cada requisito hace todo lo que pide, obligación a obligación: cumplida, no cumplida, en parte o imposible de saber leyendo el código. También es la herramienta `check_compliance` | `compliance_checker` |
| `atlas impact` | informe de lo que afecta un cambio de código o de un requisito | `impact_analyst` |
| `atlas coverage` | informe de los huecos de trazabilidad de un módulo | `coverage_auditor` |
| `atlas link` | para cada requisito sin código, el código que puede implementarlo, con confianza (alta, media, baja), motivo y la etiqueta `Req:` que habría que añadir y dónde. No toca el código. También es la herramienta `link_requirement` | `req_linker` |
| `ask_atlas` | el asistente como herramienta de `atlas serve`: una llamada para el cliente. Su `status` dice, sin modelo, si responde: `answered` (cada ID y cifra de la respuesta está en lo que devolvieron sus herramientas), `partial` (las frases que no, en `uncited`) o `not_found` | `assistant` |

En la verificación y en los informes, el grafo encuentra los datos sin modelo, y el agente
solo juzga o redacta. Los informes van a `<workspace>/reports/` y guardan, tras el texto
del modelo, los datos de los que sale. La verificación guarda sus juicios en caché:
verificar otra vez solo pregunta por lo que ha cambiado, y verifica varios requisitos a la
vez (`[pipeline] workers`).

## Desarrollo guiado por los tests

`atlas develop PLAN` encarga el código a un agente, tarea a tarea, y deja que los tests digan
qué ha salido bien. El plan es un TOML:

```toml
agent = "implementer"                    # el agente de los encargos
fix_agent = "fixer"                      # el que arregla un test que falla (por defecto, agent)
check = ["src/*.ad?"]                    # check_code sobre estos ficheros tras cada encargo
tests = ["tests/python/test_stp_*.py"]   # los tests de aceptación, con run_test, en este orden
progress = 'Step\s+(\d+)'                # opcional: hasta dónde llegó un test que falla
build_fixes = 2                          # vueltas para arreglar lo que dice check, tras un encargo
advance_fixes = 2                        # vueltas para que avancen los tests de advances
fix_rounds = 12                          # rondas para arreglar tests, tras los encargos
stuck_rounds = 2                         # rondas sin avanzar antes de dejar un test a una persona
restarts = 1                             # veces que una ejecución parada vuelve a empezar, limpia
attempts = 1                             # intentos de cada encargo a la vez, cada uno en una copia
planner = "planner"                      # opcional: un agente que lee y dice cómo hacer cada encargo
task_calls = 0                           # presupuesto de cada intento: llamadas (0 = sin límite)
task_seconds = 0                         # ... segundos
task_cost = 0.0                          # ... coste, el que dice el servidor del modelo

[[tasks]]
id = "ping-bye"
text = "In Controller.Execute, for SRS-0113 and SRS-0114: PING is answered OK PONG ..."
advances = ["test_stp_0101.py"]          # opcional: los tests que este encargo hace avanzar
```

Las rutas del plan (`check`, `tests`, `advances`) son relativas a la carpeta de `atlas.toml`,
como las de `[sources.code]`, se lance atlas desde donde se lance.

Sale de lo que fue mal cuando `gemma-4-26b` y `devstral` escribieron un servidor sin ayuda
(experimento de 2026-10-13, en `atlas-socket-demo`): código que compilaba y no funcionaba,
tests cambiados para que pasaran, el mismo fallo intentado seis veces del mismo modo y
cambios que rompían lo que ya iba.

1. Cada encargo va al agente; después, `check_code` sobre los ficheros de `check` y los tests.
   Si `check_code` da errores, vuelven al agente, como mucho `build_fixes` veces.
2. Un encargo que deja errores, o que hace que un test falle antes que antes (uno que pasaba
   falla; con `progress`, se para en un paso anterior), **se deshace**: los ficheros vuelven a
   como estaban. Se intenta otra vez diciéndole lo que rompió; si lo vuelve a romper, se queda
   deshecho. El código no empeora nunca.
   Un encargo puede decir qué tests hace avanzar (`advances`: rutas, nombres o patrones de
   tests del plan). Si alguno sigue fallando donde fallaba, el encargo **vuelve al agente** con
   lo que escribió el test y el código que cita, como mucho `advance_fixes` veces, igual que
   los errores de compilación; una de esas vueltas que rompe algo se deshace ella sola. Si aun
   así no avanzan, el encargo se conserva (compila y no rompe nada) y el informe lo dice
   (`not_advanced`, «Did not advance»). Sin esto, el encargo de `HELLO` dejó con gemma una
   excepción que ningún test podía pasar, y nada lo dijo hasta ocho encargos después.
3. Después, las **rondas**: el agente recibe un test que falla, lo que escribió y el código que
   cita, y los intentos anteriores sobre él. Una ronda que rompe algo se deshace. Un test que
   no avanza en `stuck_rounds` rondas se deja **para una persona**: el agente no insiste en lo
   que no sabe arreglar. Mientras un test avanza, sigue con él; si no, pasa al siguiente.
4. Una ejecución que **se para sin acabar** vuelve a empezar con una conversación nueva, como
   mucho `restarts` veces: el mismo encargo, el diff de lo que cambió (que se queda) y lo que
   dice `check_code` ahora. Se para al agotar `max_steps` o, si el agente tiene `stall_steps`,
   al llevar esas llamadas sin que el código avance: un cambio tras el que el fichero compila o
   tiene menos errores que antes, o un test que pasa. Reescribir un fichero con los mismos
   errores no es avanzar (en la prueba del 2026-10-15, un intento reescribió `server.adb` 17
   veces con los mismos 2 errores). No cuenta en un agente que solo lee, como el planificador:
   parecería atascado siempre. En la tanda 4, las 6
   ejecuciones que agotaron sus 40 llamadas se llevaron el 41 % del tiempo, casi todo en
   bucles de leer y editar mal: una conversación larga de ediciones fallidas no mejora con más
   vueltas.
5. Con `planner`, cada encargo pasa antes por ese agente, que solo lee (sus herramientas no
   cambian nada) y escribe cómo hacerlo; el encargo llega al agente con ese plan. Es el
   arquitecto y el editor de Aider: el modelo no tiene que pensar el cambio y acertar con las
   herramientas en las mismas vueltas. Con el mismo modelo en los dos papeles, GPT-4o-mini
   pasó del 55,6 al 60,2 % en su banco de pruebas.
6. Con `attempts` mayor que 1, cada encargo (y su reintento) y cada ronda van a esos intentos
   **a la vez**, cada uno en su copia de la carpeta de trabajo, la de `atlas.toml`
   (`<workspace>/attempts/<n>`: las raíces del código, los scripts del proyecto y su propia
   carpeta de compilación), con sus arreglos de compilación y sus vueltas de `advances`. De los
   que no rompen nada gana el que deja más tests pasando, y luego el que llega más lejos en los
   demás; sus ficheros pasan a la carpeta de trabajo y las copias se borran. Un modelo pequeño
   acierta una de tres más a menudo que tres seguidas (Agentless, S*), y el cupo da para ello:
   una vuelta de `gemma-4-26b` tarda
   unos 30 s, dos peticiones por minuto de las 28 del gateway. Las raíces del código tienen que
   estar dentro de la carpeta de trabajo, y los tests no pueden usar rutas absolutas a ella.
7. Un **informe** tras cada paso, en `<workspace>/reports/develop-<plan>.json` y `.md`: qué hizo
   cada encargo y cada ronda, su tiempo, tokens y coste, sus ediciones (y cuántas fallaron) y
   sus compilaciones (y cuántas con errores), los tests que pasan y los que necesitan a una
   persona, con su salida. El tiempo de las ejecuciones (`seconds`, sumado) y el de pared de la
   tanda (`wall_seconds`: con los tests, y con los intentos a la vez contados una vez). Al final, los errores del compilador de toda la tanda, los más
   frecuentes primero, y si llevaban pista (`[sources.code] hints`): los que no, son las
   lecciones que añadir antes de la siguiente. Lo que cada encargo y cada ronda dejó en el
   código es un **parche**, `develop-<plan>/<id>.patch` (`round-<n>.patch`), enlazado desde su
   paso, y todos juntos en `develop-<plan>/all.patch`: para revisarlos y para llevarlos a otra
   copia (`git apply`, o `patch -p1`).

Una tanda **interrumpida** (Ctrl+C, o un fallo del propio atlas) deja los ficheros del paso en
curso como estaban antes de él, y el paso queda `interrupted`; ningún encargo queda a medias.
Ctrl+C mata también la prueba en marcha con lo que arrancó. `atlas develop PLAN atlas.toml
--resume` sigue desde el informe: da por hechos los encargos terminados (uno conservado, o
deshecho tras su reintento), repite los que tienen `error` o quedaron `interrupted`, y las
rondas siguen por donde iban, con lo que ya se intentó en cada test. El estado es el que dicen
los tests ahora, no el informe; los pasos anteriores se conservan, y el registro de llamadas
sigue en el mismo fichero. Con `attempts` mayor que 1, Ctrl+C espera a que acaben las
ejecuciones en marcha. Las tandas duran horas: antes, un corte obligaba a empezar de cero.

Antes de la primera ejecución se comprueba el plan: que sus agentes existen y pueden hacer su
parte (alguna herramienta que cambia el código; `check_code` si `[sources.code]` tiene
compilador; `run_test` el `fix_agent`) y que los tests están protegidos. Antes, un agente que
no existía dejaba cada encargo con un error y la tanda los recorría todos sin llamar al modelo.
`atlas develop PLAN atlas.toml --check` hace además, sin modelo y sin cambiar nada, lo que
haría la tanda al empezar: `check_code` sobre los ficheros de `check` y los tests una vez, y
avisa de los encargos de más de 1200 caracteres (un modelo pequeño va mejor con encargos
pequeños).

Cada intento de un encargo (el encargo, su reintento o una ronda), con sus arreglos de
compilación, sus vueltas de `advances` y sus reinicios, tiene un **presupuesto**: `task_calls`,
`task_seconds` y `task_cost` (0 = sin límite). Al llegar a él no se lanza ninguna ejecución más
de ese intento; lo hecho se conserva o se deshace como siempre, y el paso lo dice
(`budget_exceeded`). En la tanda 4, un arreglo de compilación gastó 40 llamadas y 37 minutos
sin arreglar nada. Una ejecución que falla por el servidor del modelo queda como `error`, no
como encargo conservado, y el resumen final las cuenta.

Deshacer devuelve los ficheros de las raíces del código que un agente puede cambiar (ni
excluidos ni protegidos); no se hace ningún commit. Los tests deben estar protegidos
(`[sources.code] protected`), para que el agente no pueda cambiarlos.

Lo medido con `gemma-4-26b` (OpenRouter, razonamiento `low`, temperatura 1,0) en
`atlas-socket-demo`, el 2026-10-13 y 14, con los tests generados por `atlas tests` y
protegidos, `run_program`, `code_at` y el registro del servidor con la traza de cada excepción:
- **Desde `src` vacío**, los 18 encargos pequeños y 2 rondas del `fixer` dieron un servidor que
  pasa los 5 procedimientos de prueba (los 10 tests, Ada y Python), sin tocar nadie el código:
  23 ejecuciones del agente, 463 llamadas, 5 h y media de agente y 0,61 $. Ningún encargo se
  deshizo; 3 necesitaron arreglos de compilación y 4 agotaron `max_steps`. En el experimento
  anterior, sin estas piezas, ninguna configuración llegó a un servidor que funcionara.
- **Sobre el código de `devstral`** que se caía en `controller.adb:103` (seis rondas de devstral
  sin arreglarlo), el `fixer` lo dejó en 5 de 5 en 2 rondas (51 min, 0,065 $): fue a la línea
  que citaba la traza y probó con `run_program`.
- **Lo que no recoge la regla de deshacer:** el encargo de `HELLO` dejó un `CONSTRAINT_ERROR`
  que un `when others => null` se tragaba (el cliente no recibía respuesta). Se conservó porque
  los tests ya fallaban en ese paso; los ocho encargos siguientes no pudieron hacer avanzar
  ningún test. Lo arregló la segunda ronda, cuando el registro del servidor ya escribía también
  las excepciones tratadas.
- **Las vueltas de gemma son lentas**: de 20 s a 10 min. Tres no contestaron en 600 s y se
  preguntaron otra vez (hoy, `[llm.network] idle_timeout` y `turn_timeout`).
- **Con `advances`** en 11 de los 18 encargos y la traza de excepciones desde el principio
  (tanda 4, otra vez desde `src` vacío): los 5 tests pasaron al acabar los encargos, **sin
  ninguna ronda**, en 28 ejecuciones, 4 h 52 min de agente y 0,72 $. El encargo de los
  argumentos dejó `PING` y `BYE` sin comprobar los suyos: volvió al agente con la salida de
  `test_stp_0103` y en una vuelta de 4 minutos el test pasó. El primer intento de `GET` dejó un
  `begin` sin cerrar que dos arreglos no resolvieron: se deshizo y el reintento se conservó.
  Un arreglo de compilación gastó sus 40 llamadas (37 min, 0,17 $) sin arreglar nada, y el
  agente dejó en `src` un fichero suyo de prueba.
- **Con las mejoras del 2026-10-15** (razonamiento devuelto, subprogramas enteros, sintaxis y
  compilación al editar, pistas, biblioteca y manuales, contexto corto, reinicios, planificador
  y dos intentos a la vez; prueba corta con los 4 primeros encargos desde `src` vacío): fallaron
  6 de 56 ediciones (11 %; el 53 % en la tanda 4). El servidor salió en 23 minutos de agente
  repartidos entre dos intentos a la vez (65 en serie en la tanda 4) y el LF en 10, por 0,23 $
  en total: más que antes por encargo, por los dos intentos, el planificador y el razonamiento
  devuelto. Los dos errores sin pista más repetidos, escapes de C (`'\r'`, `'LF'`) y
  `(others => ())`, son ya dos pistas de serie. La prueba destapó dos fallos, ya corregidos:
  el corte por atasco cortaba también al planificador, que solo lee, y no contaba como atasco
  reescribir un fichero con los mismos errores (un intento lo hizo 17 veces).

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

## Fuentes locales

Todo lo que una herramienta devuelve a un agente va al modelo. Con fuentes locales
(`model = "none"`, ver [configuración](../../docs/configuracion.md#fuentes-locales-model--none)),
las herramientas de un agente reciben su propio contexto (`model_context`, `for_model`):
las del grafo ven el grafo sin el texto de esas fuentes (`KnowledgeGraph.for_model`: los
IDs y los enlaces se quedan; el texto de los requisitos, los títulos, lo que dicen las
secciones, los comentarios y el código fuente, no), y las de documentos omiten sus pasajes
y dicen cuáles en `local_only`. `verify`, `comply`, `link`, `impact` y `coverage` usan el
mismo grafo; un requisito de un módulo local, o cuyo código es local, queda `local_only`
sin preguntar al modelo. Si el código es local, un agente con herramientas que lo leen o lo
compilan (`read_file`, `grep`, `check_code`...) no arranca.

## Evaluación

`atlas eval` ejecuta los conjuntos de `[agents.eval] sets` (YAML) y los puntúa sin modelo.
Un conjunto mezcla cuatro tipos de caso, según la clave que lleva la pregunta:

```yaml
- id: trace-srs-0004                 # un agente: necesita el modelo y el extra [agents]
  question: Which system requirement does SRS-0004 satisfy?
  expect: [SSS-0005]                 # textos que la respuesta debe contener
  tools: [trace]                     # opcional: herramientas que debe llamar
- id: logs-30-days                   # la búsqueda del índice
  search: how long does the HMI retain its log files
  expect: [30 days]
  kinds: [requirement]               # opcional, como document: dónde buscar
  tags: [paraphrase]                 # opcional, en todos los tipos: las cifras por tag
- id: graph-impact-log               # una consulta del grafo, sin modelo
  impact: Logger.Log                 # trace, trace_code, impact, find_symbol, search_code_docs
  expect: [tracks.delete_track, SRS-0004]
- id: ask-days-of-logs               # una respuesta de los documentos (atlas ask): el modelo, sin ADK
  ask: How many days must the HMI keep its logs?
  expect: ["30"]
  cited: true                        # opcional: cada texto esperado, en una fuente citada
  document: hmi_manual.md            # opcional, como scope (auto, passages, whole) y section
```

- **Agentes.** Un caso está bien si la respuesta contiene los textos esperados (IDs,
  identificadores, valores) y el agente ha llamado a las herramientas esperadas sin llegar a
  `max_steps`. Cada resultado guarda sus tokens y, si el servidor lo dice (OpenRouter), su
  coste; el informe, la suma. `examples/eval/assistant.yaml` tiene 24 casos sobre las
  muestras (y cuatro de `ask`). Con
  `gemma4:12b` sin razonamiento, el asistente acertó 23 de 24 (96 %), con una mediana de 9
  a 11 s por pregunta. El fallo dice bien qué falta por definir en las alarmas, pero contesta
  en español y sin el literal «TBD» que pide el caso.
- **Búsqueda.** Cada texto esperado se encuentra en la posición del primer resultado que lo
  contiene (en su ID, documento, título de sección o texto), y el caso, en la del último.
  `section:<documento>#<id de sección>` solo casa con un resultado de esa sección: los IDs
  de sección se repiten en cada documento y son cortos (`004`), así que no se buscan en el
  texto. De
  ahí salen el acierto@k (los casos con todo lo esperado entre los k primeros, para k = 1, 3,
  5 y 10) y el rango recíproco medio (MRR). Cada búsqueda se hace dos veces: híbrida (IDs,
  vectores y BM25, como `atlas search`) y léxica (IDs y BM25, sin ninguna petición), para ver
  qué aportan los vectores.
- **Grafo.** Un caso está bien si el resultado de la consulta contiene lo esperado. El grafo
  no ordena ni varía: o está o no está.
- **Respuestas (`ask`).** La pregunta va a `Index.answer`, como `atlas ask` y la herramienta
  `ask_docs`, con su `document`, `scope` y `section` si los tiene (`section` solo si la
  versión de `Index.answer` lo admite; si no, el caso falla con ese error). Está bien si la
  respuesta contiene cada texto esperado y, con `cited: true`, si cada uno está además en el
  texto que el modelo leyó de una fuente que la respuesta cita (`[n]`): el valor correcto
  sacado de la fuente equivocada no vale. Guarda las peticiones al modelo, el `scope` con el
  que se leyó, los segundos y las fuentes citadas (y `status` y `unsupported` cuando la
  respuesta los trae). Es lo más caro y lo que más cambia entre versiones.

`tags` agrupa los casos por tipo: el informe y la salida de `atlas eval` dan las cifras de
la búsqueda (acierto@k y MRR, híbrida y léxica) y del grafo de cada tag, además de las del
conjunto. Un caso puede tener varios: `[paraphrase, spanish]`.

`examples/eval/retrieval.yaml` tiene 20 búsquedas y 10 consultas del grafo sobre las
muestras, con sus tags: `id` (IDs exactos), `words` (palabras del texto), `paraphrase`,
`spanish`, `section` y `code`; en el grafo, `trace`, `impact` y `code`. Con
`embeddinggemma`:

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
runs = 3                   # veces que se hace cada pregunta (agentes y ask): los modelos no responden siempre igual
```

Los resultados van a `<workspace>/reports/eval-<conjunto>.json`, en `retrieval`, `answers` y
`agents`, con una cabecera `meta` que dice qué se midió: el modelo de los agentes y el de
`[llm]`, su `reasoning_effort`, el `provider` de `[llm.extra_body]`, el modelo de
embeddings, la versión de atlas, la fecha, `runs` y el hash del YAML. Ese fichero se sobrescribe; una
copia queda en `eval-<conjunto>-<versión>-<fecha>.json`. `atlas eval --compare` compara cada
caso con el del último informe de otra versión de atlas: el rango anterior y el nuevo de
cada búsqueda (híbrida y léxica), si cada consulta o pregunta pasaba y pasa, y al final las
regresiones (una búsqueda que baja o deja de encontrarse en cualquiera de los dos modos, o
algo que pasaba y ya no). Con alguna regresión termina con código 1. El mismo día y la misma
versión, la copia se sobrescribe. Con `atlas.test.toml` y el servidor falso (sus vectores
no saben de significado: las cifras solo enseñan la forma), y un informe anterior de otra
versión:

```text
retrieval (written to workspace-test/reports/eval-retrieval.json):
  search, hybrid: hit@1 50%, hit@3 65%, hit@5 75%, hit@10 85%, MRR 0.60 (20 cases)
  search, lexical: hit@1 60%, hit@3 60%, hit@5 80%, hit@10 90%, MRR 0.66 (20 cases)
    id (3): hybrid hit@3 67% MRR 0.73  lexical hit@3 67% MRR 0.75
    paraphrase (6): hybrid hit@3 17% MRR 0.18  lexical hit@3 33% MRR 0.40
    ...
  graph: 10 of 10 right (100%)
    trace: 4 of 4
    ...
  compared with atlas 0.2.0 of 2026-10-16T18:02:11:
    same       search-id-srs-0102  hybrid 1 lexical 1 -> hybrid 1 lexical 1
    REGRESSION search-code-hours  hybrid 4 lexical 6 -> hybrid - lexical 6
    ...
  regressions: search-code-hours
```

Un conjunto con solo búsquedas y consultas del grafo no necesita ADK ni el modelo de chat;
la búsqueda híbrida sí necesita el de embeddings. Los vectores de las consultas quedan en la
caché del índice: una segunda pasada no hace ninguna petición a los embeddings.

### Un conjunto generado para el proyecto real

Para el proyecto real nadie ha escrito casos todavía. `atlas eval --generate` los saca del
`atlas.db` y del grafo del workspace, sin modelo, y da la medida de regresión desde el primer
día:

```bash
atlas run atlas.toml
atlas eval --generate examples/eval/tms.yaml --n 50 atlas.toml
```

Escribe hasta `--n` casos (50 por defecto) de cuatro tipos, por turnos:

| Tag | Caso | Espera |
|---|---|---|
| `id` | `search` con el ID exacto de un requisito al azar | el ID |
| `section` | `search` con el título de una sección (uno que no tenga otra) | `section:<documento>#<id>` |
| `trace` | `trace` de un requisito que tiene código | el requisito, su código y sus tests (10 de cada como mucho) |
| `impact` | `impact` de un subprograma que tiene llamadores | sus llamadores (10 como mucho) |

Todos llevan además el tag `generated`. La semilla es fija: con los mismos datos sale el
mismo fichero, así que dos versiones de atlas se comparan con `atlas eval --compare`. Son
solo casos de recuperación y del grafo, sin agentes: miden si la búsqueda encuentra lo que
el proyecto tiene y si el grafo lo enlaza, no si el modelo responde bien. El fichero se
reescribe entero cada vez (su primera línea dice que es generado); uno escrito a mano no se
sobrescribe nunca, y los casos a mano van en otro conjunto. Se añade a `[agents.eval] sets`
como cualquier otro:

```toml
[agents.eval]
sets = ["examples/eval/tms.yaml"]
```

Sobre las muestras, con el servidor falso, 50 casos (22 `id`, 21 `section`, 5 `trace` y 2
`impact`): los IDs salen todos primeros, las secciones entre las diez primeras, y el grafo
acierta 7 de 7. Al regenerar después de cambiar los datos, cambian los casos: para comparar
versiones, se compara con el mismo fichero.

## Con Ollama

```bash
pip install -e ".[agents]"                       # google-adk
OLLAMA_CONTEXT_LENGTH=16384 ollama serve         # en otra terminal
make test-ollama                                 # o: python -m pytest -q -m model
```

`tests/test_agents_ollama.py` prueba los agentes contra el modelo de la configuración de
`ATLAS_TEST_CONFIG`, por defecto `atlas.dev.toml`: sus `[llm]`, `[embeddings]` y
`[network]` van a las configuraciones que escriben las pruebas, y los agentes usan su
`[agents] model`. Con `make test-gateway` (`ATLAS_TEST_CONFIG=atlas.toml`) son las mismas
contra el gateway, con su proxy, su CA y su cupo (ver el [README](../../README.md#niveles-de-modelo)).
Son siete pruebas:
- un agente con dos herramientas propias sobre las muestras (`get_requirement` y una `trace`
  que busca las etiquetas `Req:`): usa las dos, responde con el requisito y los ficheros
  correctos y no se inventa ficheros;
- un cambio preciso en el Ada con las herramientas de ficheros;
- un agente con las herramientas de `atlas serve` por MCP;
- el asistente de serie, como lo ejecuta `atlas agent assistant`;
- la verificación de un requisito con la documentación;
- las propuestas de enlaces del `req_linker`, una que debe proponer y otra que no;
- el cumplimiento de un `Classify` con un error de límite y sin él.

Llevan el marcador `model` (y `ollama`, su nombre de antes, que sigue valiendo). Se omiten
solas si falta el extra `[agents]` o si `atlas check` falla sobre esa configuración (Ollama
parado, el modelo sin descargar, el gateway sin red o sin clave), y el motivo dice la salida
de `atlas check`. Para excluirlas: `python -m pytest -m "not model"`.

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

`tests/test_develop.py`: `atlas develop` con un agente de pega sobre un proyecto Python (encargos
que se deshacen, arreglos de compilación, rondas y lo que se deja a una persona).
`tests/test_agents.py`: ficheros Markdown y sus retoques, `AtlasLlm` con un cliente de pega,
`max_steps`, `build_context`, la verificación, los informes, `ask_atlas`, la evaluación de los
agentes y la app de `adk web`. `tests/test_eval.py`: la evaluación de la búsqueda y del grafo.
Con Ollama, `tests/test_agents_ollama.py`.
