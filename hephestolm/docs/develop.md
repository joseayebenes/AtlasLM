# hephesto develop: desarrollo guiado por los tests

`hephesto develop PLAN` encarga el código a un agente, tarea a tarea, y deja que los tests digan
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
parallel = 1                             # encargos a la vez, los que ya tienen hechos los de su after
planner = "planner"                      # opcional: un agente que lee y dice cómo hacer cada encargo
task_calls = 0                           # presupuesto de cada intento: llamadas (0 = sin límite)
task_seconds = 0                         # ... segundos
task_cost = 0.0                          # ... coste, el que dice el servidor del modelo
require_tags = false                     # un encargo cuyos requisitos no cita nada de lo cambiado vuelve una vez
requirement_chars = 1200                 # caracteres de cada requisito delante del encargo
reviewer = "reviewer"                    # opcional: un agente que lee el diff de un encargo que compila
review_fixes = 1                         # veces que el veredicto del revisor vuelve al agente (0: solo se informa)
stray = "report"                         # o "delete": los tests que el agente deja entre el código
propose = false                          # true: los agentes trabajan en una copia; el resultado, los parches
max_copy_mb = 500                        # lo más que copian propose y attempts de la carpeta de trabajo

[[tasks]]
id = "ping-bye"
text = "In Controller.Execute, for SRS-0113 and SRS-0114: PING is answered OK PONG ..."
requirements = ["SRS-0113", "SRS-0114"]  # opcional: los requisitos que implementa, del grafo
advances = ["test_stp_0101.py"]          # opcional: los tests que este encargo hace avanzar
after = ["T01"]                          # opcional: los encargos que necesita hechos (por defecto, el anterior)
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
   La copia lleva también la carpeta de compilación (`<workspace>/build`), donde se ejecutan los
   tests. Una carpeta de trabajo de más de `max_copy_mb` (sin `.git` ni el workspace) o un test
   que la nombra por su ruta absoluta se rechazan al empezar: el test probaría el código de la
   carpeta de trabajo, no el de la copia.
7. Con `reviewer` (apagado por defecto), un encargo que compila y no rompe nada pasa por ese
   agente con el encargo y el diff del cambio. Solo lee, tenga las herramientas que tenga: las
   de ficheros le contestan que son de solo lectura. Contesta en JSON, `{"verdict": "ok"}` o
   `{"verdict": "issues", "issues": [{"file", "line", "issue"}]}`, y los problemas vuelven al
   agente `review_fixes` veces (una por defecto; con 0 solo se informan); un arreglo que rompe
   algo se deshace. El
   informe los lista en «Review». Es para lo que no ven ni el compilador ni los tests (ramas
   muertas, cambios fuera del encargo); antes van las reglas sin modelo de
   `[sources.code] rules`. Falta medirlo con un banco de encargos antes de encenderlo por
   defecto. `hephestolm/examples/agents/reviewer.md` es un ejemplo.
8. Los ficheros que **crea** un encargo (o una ronda) que se conserva van en su paso
   (`created`), en la sección «New files» del informe y en la salida de `hephesto develop`. En la
   tanda 4 el agente dejó en `src` un fichero suyo de prueba, que entró en `check`, en cada
   copia de `attempts` y en el siguiente `atlas run`. Con `stray = "delete"`, antes de juzgar
   el encargo se borran los ficheros nuevos de su ejecución que casan con
   `[sources.code] test_patterns` y no con `protected`: un test del agente fuera de `tests/`. Se
   dicen en `deleted`. Nunca se borra un fichero nuevo que no sea un test, ni una carpeta. No
   hace falta limitar dónde escribe cada agente: `protected` ya se aplica a los ficheros nuevos.
9. Con `parallel` mayor que 1, los encargos cuyos `after` ya están hechos van **a la vez**,
   como mucho `parallel`, cada uno en su copia de la carpeta de trabajo
   (`<workspace>/tasks/<id>`) con toda la maquinaria de un encargo: su planificador, sus
   intentos, sus arreglos de compilación, sus vueltas de `advances` y su reintento. Un encargo
   sin `after` va después del anterior, así que un plan sin `after` sigue yendo de uno en uno,
   como hoy; para que dos vayan a la vez, `after = []` (o `after = ["T01"]` los dos). Al acabar
   uno, a la carpeta de trabajo pasan **solo los ficheros que cambió** frente a su copia, y los
   tests se pasan otra vez allí. Si otro encargo que acabó antes ya había cambiado alguno de
   esos ficheros, o si juntos rompen lo que iba, el segundo en llegar **se deshace** (`conflict`
   en su paso, con los ficheros y los encargos con los que chocó) y vuelve a hacerse solo, sobre
   el código de ahora, cuando acaban los que están en marcha; los que van después de él lo
   esperan. Si solo un encargo puede ir, va en la carpeta de trabajo, sin copia. Un `after` que
   nombra un encargo que no existe, o encargos que se esperan entre sí, son un error al leer el
   plan.

   Cada agente a la vez pide una vuelta cada unos 30 s: los `parallel` × `attempts` agentes se
   comparan con `[network] requests_per_minute` (con la duración de las vueltas medida en el
   informe, si se reanuda), y se avisa si van a llenarlo. Mientras el cupo no se comparta entre
   procesos (`atlas serve` y `atlas run` creen tener las 28 peticiones para ellos), `parallel`
   es como mucho 2, y el informe lo dice. El beneficio depende de que el plan tenga cadenas
   independientes, y no está medido todavía con el gateway.
10. Un **informe** tras cada paso, en `<workspace>/reports/develop-<plan>.json` y `.md`: qué hizo
   cada encargo y cada ronda, su tiempo, tokens y coste, sus ediciones (y cuántas fallaron) y
   sus compilaciones (y cuántas con errores), los tests que pasan y los que necesitan a una
   persona, con su salida. El tiempo de las ejecuciones (`seconds`, sumado) y el de pared de la
   tanda (`wall_seconds`: con los tests, y con los intentos a la vez contados una vez). Con
   `parallel`, `schedule`: el tiempo de los encargos uno tras otro, el de pared y la
   aceleración, en una línea del Markdown, y los avisos del cupo en `warnings`. Al final, los
   errores del compilador de toda la tanda, los más
   frecuentes primero, y si llevaban pista (`[sources.code] hints`): los que no, son las
   lecciones que añadir antes de la siguiente, y van como borradores en
   `reports/hints-<plan>.toml` (ver «Borradores de pistas»). Los avisos de instrucciones dentro
   de los datos que dieron las herramientas (ver «Datos e instrucciones») van en el paso, en
   `warnings`, y en la sección «Instructions inside the data» del Markdown. Lo que cada encargo
   y cada ronda dejó en el código es un **parche**, `develop-<plan>/<id>.patch` (`round-<n>.patch`), enlazado desde su
   paso, y todos juntos en `develop-<plan>/all.patch`: para revisarlos y para llevarlos a otra
   copia (`git apply`, o `patch -p1`).

Una tanda **interrumpida** (Ctrl+C, o un fallo del propio atlas) deja los ficheros del paso en
curso como estaban antes de él, y el paso queda `interrupted`; ningún encargo queda a medias.
Ctrl+C mata también la prueba en marcha con lo que arrancó. `hephesto develop PLAN atlas.toml
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
`hephesto develop PLAN atlas.toml --check` hace además, sin modelo y sin cambiar nada, lo que
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

## Encargos anclados a requisitos

Sin `requirements`, la trazabilidad entra en la tanda solo porque `implementer.md` pide poner
`-- Req:`, y nada lo comprueba; el encargo nombra los requisitos en texto libre y el agente no
recibe su texto oficial. Con `requirements` (IDs del grafo, así que antes `atlas run`), un
encargo recibe tres cosas sin modelo:

1. **Su texto, delante.** Por cada requisito, su texto oficial, las secciones que lo declaran
   (con el texto del documento) y el código que ya lo cita, de `trace`, como mucho
   `requirement_chars` caracteres cada uno: no el `build_context` entero, que es para un
   requisito solo y gasta 4000 tokens. El planificador, si lo hay, lo recibe igual.
2. **Sus tests, si no trae `advances`.** Los tests del plan que lo citan (`verified_by` de
   `trace`, los que lo nombran en un comentario) y los de los procedimientos de prueba
   enlazados con él por `verifies` en DOORS (STP-0101 `verifies` SRS-0113): los que citan el
   procedimiento y los que llevan su nombre, como los escribe `hephesto tests`
   (`test_stp_0101.py`). Cuando DOORS tenga la semántica de los tipos de enlace (6.1 del
   análisis), el tipo saldrá de ahí; hoy se busca el tipo `verifies` tal cual.
3. **Si quedan sin etiquetar.** Tras un encargo conservado, se analizan los ficheros cambiados
   con el analizador de su lenguaje (`[sources.code] languages`, sin el motor incremental) y el
   paso dice qué requisitos del encargo no cita ningún subprograma cambiado (`untagged`, y
   «Untagged» en el informe). Cuenta la etiqueta de la declaración en la especificación cuando
   cambió el cuerpo. Con `require_tags = true`, el encargo vuelve una vez al agente con ese aviso
   (`tag-fix`); si esa vuelta rompe algo, se deshace ella sola.

Un ID que no está en el grafo, o un workspace sin `atlas run`, es un error de configuración
antes de empezar, no a mitad de la tanda. El informe guarda en `requirements` los de cada
encargo y sus `advances` (con `advances_from = "requirements"` cuando salen del grafo).

## Borradores de pistas

Escribir una pista era leer el `.calls.jsonl`: las dos de la prueba corta del 2026-10-15
fueron un commit a mano. `hephesto hints PLAN [atlas.toml]` (y el final de cada `hephesto develop`,
en la sección «Hints to write» del informe) lee las llamadas de la última tanda del plan y
agrupa por forma (el mensaje sin sus nombres ni sus números) los errores sin pista: los del
`check` de cada cambio, los de la sintaxis de un cambio rechazado y los de `check_code`. Para
cada forma busca en la misma ejecución la primera escritura posterior al mismo fichero cuyo
`check` ya no lo trae (en un error de sintaxis, la primera que se hizo), y guarda su diff.
Escribe `reports/hints-<plan>.toml`, un `[[hint]]` por forma, las más frecuentes primero:

```toml
# 3 times, in src/server.adb (runs 4, 7)
# message: "Get_Line" not declared in "Text_IO"
# how the agent got past it (run 4, edit_file of src/server.adb):
#   -      Line := Text_IO.Get_Line (Socket);
#   +      Line := Ada.Text_IO.Get_Line;
[[hint]]
match = '"(?P<name>[^"]+)" not declared in "(?P<name2>[^"]+)"'
hint = ""
```

El `match` sale del mensaje: cada nombre entre comillas, un grupo con nombre; cada número,
`\d+`; el resto, tal cual. Nunca se usa solo: una persona lo revisa, escribe la pista
(`{name}` va al texto), borra las que no merecen una y las copia a un fichero de
`[sources.code] hints`. En la librería, `draft_hints(calls_path)` devuelve lo mismo como una
lista.

Deshacer devuelve los ficheros de las raíces del código que un agente puede cambiar (ni
excluidos ni protegidos); no se hace ningún commit. Los tests deben estar protegidos
(`[sources.code] protected`), para que el agente no pueda cambiarlos.

## Los agentes medidos

`hephestolm/examples/agents/` trae los agentes con los que se midió lo que sigue, adaptados de los de
`atlas-socket-demo` a un proyecto Ada cualquiera (sin lo propio del servidor de órdenes ni el
resumen de `GNAT.Sockets`):

| Agente | Qué hace | Herramientas | Límites |
|---|---|---|---|
| `implementer` | los encargos: cambia el código, lo compila y lo prueba con `run_program` | `replace_subprogram`, `add_subprogram`, `write_file`, `edit_file`, `check_code`, `run_program`, `library_spec`, `search_reference`, `find_definition`, `describe_symbol`, `get_requirement` | `max_steps` 40, `stall_steps` 15, `keep_tool_results` 10 |
| `fixer` | las rondas: busca por qué falla un test y arregla el código | las mismas, más `grep` y `run_test` | igual |
| `planner` | lee y dice cómo hacer un encargo (`planner = "planner"`) | solo de lectura | `max_steps` 15, `keep_tool_results` 10 |
| `tester` | escribe una prueba de un requisito y la ejecuta | `write_file`, `run_test`... | `max_steps` 20 |

Los ficheros no fijan el muestreo: dicen en un comentario con qué modelo se midieron, y
`atlas.openrouter.toml` pone el de `gemma-4-26b` en `[agents.implementer]`, `[agents.fixer]` y
`[agents.planner]` (razonamiento `low`, temperatura 1,0, `top_p` 0,95, `top_k` 64). Con
`atlas.dev.toml` (`gemma4:12b` en Ollama) van con lo de `[llm]`, sin razonamiento. Para un
proyecto, se copian a su carpeta `agents/` y se añade al cuerpo un resumen de la biblioteca que
más usa, con sus declaraciones reales (`atlas-socket-demo` lleva el de `GNAT.Sockets`).
`tests/test_examples.py` comprueba que cargan, que todas sus herramientas existen y el
muestreo de cada TOML.

## Un agente de fuera bajo el mismo juez

Lo que hizo funcionar a gemma no es el agente sino el juez: `check`, los tests protegidos,
deshacer, `advances`, rondas e informe. `agent_command` pone ese juez a un agente de fuera
(OpenCode, Claude Code...) en lugar de `agent` y `fix_agent` (si vienen los dos, el plan da
error):

```toml
agent_command = ["opencode", "run", "--format", "json", "{prompt}"]
# o: agent_command = ["claude", "-p", "--permission-mode", "acceptEdits", "{prompt}"]
command_timeout = 1800                   # segundos de cada ejecución (0: sin límite)
check = ["src/*.ad?"]
tests = ["tests/python/test_stp_*.py"]
```

Cada ejecución del agente es ese comando, lanzado en la carpeta de trabajo (o en la copia del
intento, con `attempts`), con el encargo en lugar de `{prompt}` (o por la entrada estándar, si
ningún argumento lo lleva). Lo demás no cambia: `check_code` sobre `check`, los tests, deshacer
lo que rompe algo, `advances`, rondas, reinicios e informe. El planificador, si lo hay, sigue
siendo un agente de atlas.

- **Sus herramientas son suyas**: atlas no puede negarle que edite un test protegido. Por eso
  lee los ficheros protegidos (los de `[sources.code] protected` en las raíces del código y
  los tests del plan) antes de cada ejecución y los compara después: si cambió alguno, o creó
  o borró alguno, **se devuelven** a como estaban y el paso lo dice (`protected`, «Protected
  files put back» en el informe). Ficheros fuera de las raíces del código, en cambio, no se
  deshacen: eso queda a cargo del agente y de su configuración de permisos.
- **El tiempo**: una ejecución que pasa de `command_timeout` se corta (con todos los procesos
  que lanzó) y cuenta como una que se paró sin acabar: vuelve a empezar con `restarts`, como
  una que agota `max_steps`. Una que acaba con un código de salida distinto de 0 es un error
  del paso.
- **El registro**: lo que el agente escribe por su salida estándar va a
  `develop-<plan>.calls.jsonl` como una única llamada (`agent_command`, con su salida, su
  código de salida y sus segundos); atlas no ve sus llamadas a herramientas, así que el informe
  no cuenta sus ediciones ni sus compilaciones, ni sus tokens.
- **Los textos** de `fix_prompt` y `advance_prompt` nombran `check_code` y `run_test`: con un
  agente de fuera que no los tenga (ver abajo), conviene escribirlos con lo que sí tiene.

Para que el agente tenga además el conocimiento de atlas (documentos, requisitos, grafo y
compilador) durante la tanda, se le da `atlas serve` como servidor MCP. Con OpenCode, un
`opencode.json` en la carpeta de trabajo, que es donde atlas lo lanza:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "atlas": {
      "type": "local",
      "command": ["atlas", "serve", "atlas.toml"],
      "cwd": ".",
      "enabled": true
    }
  }
}
```

Con `"cwd": "."`, OpenCode arranca el servidor en esa carpeta, así que `atlas.toml` y su `workspace` se leen de
ella. Con `attempts` mayor que 1, cada intento corre en su copia, que no lleva el workspace:
hay que poner la ruta absoluta del `atlas.toml` (y que su `workspace` sea absoluto), y
entonces `read_file` y `check_code` del servidor ven la carpeta de trabajo y no la copia; en
ese caso es mejor dejar al servidor solo el conocimiento (`[mcp] tools`) y que el agente lea
y compile con sus herramientas. Las variables del entorno que necesite atlas (el proxy, la
clave del modelo) las hereda de `hephesto develop`.

## Parches y modo propuesta

Los parches (punto 10) son diffs unificados con 3 líneas de contexto y las rutas de la carpeta
de trabajo tras `a/` y `b/`, para `git apply` o `patch -p1` en ella; `all.patch` los junta en
orden. Con `attempts`, el parche es el del intento ganador. `answers.md` guarda la respuesta
entera de cada ejecución del agente, y la tabla del informe enlaza cada paso con su parche y su
respuesta. Los tests protegidos no salen en los parches, ni los ficheros binarios.

Con `propose = true` en el plan, la carpeta de trabajo **no se toca**: los agentes trabajan
sobre una copia (`<workspace>/attempts/0`, como la de `attempts`), los tests se ejecutan en
ella y el resultado son los parches. La persona revisa y aplica los que quiera, igual que
`atlas link` propone la etiqueta y no la escribe. La copia se queda hasta el siguiente
`hephesto develop`, para mirarla o ejecutar sus tests (con `--resume`, se sigue en ella); cada tanda empieza de la carpeta de
trabajo tal como esté entonces (con los parches que se hayan aplicado). En un proyecto
grande, copiar la carpeta de trabajo puede no caber (`max_copy_mb`): sin `propose`, los
parches dan casi lo mismo, y el código queda en la carpeta de trabajo.

Lo medido con `gemma-4-26b` (OpenRouter, razonamiento `low`, temperatura 1,0) en
`atlas-socket-demo`, el 2026-10-13 y 14, con los tests generados por `hephesto tests` y
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
