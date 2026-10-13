# atlas.code — código fuente

Analiza el código (Ada con tree-sitter, Python con `ast`) y da a los agentes las herramientas habituales de
un agente de código, confinadas a las carpetas del proyecto. Viene de
`legacy/ada_graph.py`.

| Fichero | Qué tiene |
|---|---|
| `ada.py` | `AdaAnalyzer`, el analizador de Ada (plugin `code_analyzer:ada`) |
| `python.py` | `PythonAnalyzer`, el de Python (plugin `code_analyzer:python`), sin dependencias |
| `common.py` | la etiqueta de trazabilidad (`req_tag`, `req_regex`) |
| `tools.py` | `CodeTools`: `list_files`, `grep`, `read_file`, `edit_file`, `write_file` |
| `build.py` | `BuildTools`: `check_code`, `run_test` y `run_program`, el compilador, las pruebas y el programa del proyecto |
| `procedures.py` | `generate_tests`: los procedimientos de prueba como tests, paso a paso, sin modelo (`atlas tests`) |

## Análisis

El paso `code.parse` analiza cada fichero y guarda en `atlas.db`:
- sus subprogramas, con sus líneas, sus llamadas propias, el comentario de cabecera (el del
  `.ads` si lo hay) y los requisitos que citan;
- sus tipos, variables, constantes y `with`.

Los requisitos se leen de la etiqueta `[sources.code] req_tag` (`"Req:"` por defecto). Para
otro formato, `req_regex`, cuyo grupo 1 es la lista de IDs. Los ficheros que casan con
`test_patterns` son pruebas. Con eso, el paso `graph` construye el grafo de código (ver
[atlas.graph](../graph/README.md)).

El paso `index.code` lo lleva además al índice de búsqueda: cada subprograma, tipo y variable,
con su comentario, sus requisitos y su código (ver [atlas.index](../index/README.md)).

**Python** (`languages = ["ada", "python"]`): funciones y métodos (`Clase.metodo`), clases
como tipos, variables del módulo e `import`. Un módulo se nombra por su ruta desde la raíz de
código que lo contiene (`tests/python/test_session.py` bajo la raíz `tests` es
`python.test_session`), así que no choca con una unidad Ada del mismo nombre. Una etiqueta
`# Req:` cuenta para una función si está en los comentarios justo encima (o encima de sus
decoradores), en su docstring o en un comentario de su cuerpo; una del módulo no cita nada.
Con `test_patterns`, una prueba Python y una Ada verifican el mismo requisito en el grafo.

Otro lenguaje es otro plugin `CodeAnalyzer` (ver «Añadir o quitar piezas» en el
[README](../../README.md)).

## Herramientas para agentes de código

`CodeTools` tiene las herramientas habituales de un agente de código sobre
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
- Los ficheros de `[sources.code] protected` (`["tests/**"]`) se leen pero no se cambian. Son
  los tests: al pedirles arreglar un test que fallaba, `gemma-4-26b` cambió el fin de línea del
  cliente a CR LF y `devstral` las respuestas que esperaba, y los tests pasaron sin que el
  servidor funcionara.

Un error es una respuesta con `error` para que el agente se corrija. Si el texto a sustituir
no aparece, dice si el texto nuevo ya está (el cambio ya se hizo: con `gemma-4-26b`, 8 de 11
ediciones fallidas de una tarea repetían una que ya se había aplicado), el bloque del fichero
más parecido a lo buscado (`closest`, con sus líneas) y las líneas como la primera.

Los modelos pequeños suelen fallar copiando el texto a sustituir: escapan las comillas o
cambian la sangría. `edit_file` lo intenta primero exacto y luego sin esos escapes y sin
tener en cuenta la sangría, siempre que la coincidencia sea única, y lo indica en `notes`.
Con eso, un agente ADK con `gemma4:12b` cambió un mensaje de log en los ejemplos en 5
intentos de 5, unos 20 s cada uno; sin esa tolerancia entraba en bucle.

En los cambios de varias líneas, `gemma4:12b` copia además el listado de `read_file`: los
números de línea (`    3 | `), una barra antes de cada salto de línea y algunos saltos como
`\n`, tanto en el texto que busca como en el nuevo. Si el texto buscado trae esas marcas,
`edit_file` las quita de los dos y lo intenta otra vez. En un experimento (poner una guarda en
`Tracks.Update`), sin esta tolerancia el agente repitió la misma edición fallida hasta agotar
`max_steps` en 2 ejecuciones de 2; con ella, hizo el cambio correcto en las 2.
`gemma-4-26b` pone a veces esas barras solo en el texto nuevo, con el buscado bien copiado: si
casi todas las líneas del texto nuevo (o del contenido de `write_file`) acaban en barra, se
quitan también.

Un texto buscado de más de 30 líneas se rechaza, con un error que pide varias ediciones
pequeñas o reescribir el fichero con `write_file`. Con `gemma-4-26b` y un fichero de 200
líneas, los textos largos no eran copias del fichero sino reconstrucciones de memoria (uno de
142 líneas tenía las funciones en otro sitio): dos tareas acabaron así en bucles de leer y
editar de 27 y 45 minutos. Con el límite, la siguiente salió en 7 llamadas.

También están registradas como plugins `tool` con esos nombres, para las herramientas de
los agentes y `[mcp] tools`. Lo que se edite se vuelve a analizar en el siguiente
`atlas run`.

## Compilador y pruebas

Un modelo que lee código no ve lo que ve un compilador: en un experimento, `gemma4:12b`
escribió código con la sintaxis bien que no habría compilado (un subprograma duplicado, una
variable con el nombre de una función). `BuildTools` le da dos herramientas:

| Herramienta | Qué hace |
|---|---|
| `check_code` | compila un fichero sin generar código y devuelve los errores y avisos, cada uno con fichero, línea, columna y mensaje. El mismo mensaje en varias líneas va una vez, con las demás líneas en `also_at` (un arreglo, muchos sitios), y las pistas que el compilador da en el mismo sitio van en `details` (GNAT: «use clause would make operation legal»). Como mucho 10 de cada clase |
| `run_test` | construye un procedimiento de prueba y lo ejecuta: pasa si termina con código 0. Si no compila, los errores; si falla, su salida (el mensaje del `Program_Error`). Una prueba que no se construye, como un script de Python, solo se ejecuta |
| `run_program` | ejecuta el programa del proyecto (`program_command`) con las líneas que le da el agente en su entrada, y devuelve lo que escribe: para ver qué hace el código con una entrada, antes y después de un cambio |

Cuando la salida de `run_test` o de `run_program` cita un sitio del código
(`raised CONSTRAINT_ERROR : controller.adb:103 length check failed`, o el `File "x.py", line 12`
de Python), las líneas de alrededor van en `code_at`, con la citada marcada. Solo las de los
ficheros que el agente puede cambiar: no las de los tests protegidos ni las del compilador.
En el experimento de 2026-10-13, `devstral` tuvo ese mensaje en seis rondas sin ir a leer la
línea 103.

Los comandos están en `[sources.code]`, como listas de argumentos y sin intérprete de
órdenes, así que no están atados a ningún compilador. Con GNAT, instalado con Alire (ver la
[guía de instalación](../../docs/instalacion.md)):

```toml
[sources.code]
check_command = ["gcc", "-c", "-gnatc", "{include}", "{file}"]
test_command = ["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"]
tool_path = ["~/.local/share/alire/toolchains/gnat_native_*/bin"]
```

`{include}` se convierte en un `-I` por cada carpeta de código, `{build}` es
`<workspace>/build`, donde el compilador deja lo que produce, y `tool_path` va antes del
`PATH` solo para estos comandos. Un proyecto con fichero GPR usaría `gprbuild -P`.
`gnatmake` decide qué recompilar por las fechas de los ficheros: un cambio un instante después
de compilar puede no verse. Si pasa, `-f` en `test_command` recompila todo lo que usa la
prueba.

Una prueba que habla con un servidor necesita que esté en marcha. `run_command` dice cómo se
ejecuta lo que construyó `test_command` (por defecto, el ejecutable tal cual), así que un
envoltorio del proyecto puede arrancar el servidor, ejecutar la prueba y pararlo. Las pruebas
que no se construyen van por `test_commands`, por extensión, y pasan igual si terminan con 0:

```toml
[sources.code]
run_command = ["with_server", "{exe}"]
tool_path = ["scripts", "~/.local/share/alire/toolchains/gnat_native_*/bin"]
[sources.code.test_commands]
py = ["with_server", "python3", "{file}"]
```

GNAT ve más errores cuando genera código: una cadena de longitud fija asignada a otra de
distinta longitud es, al compilar con código, el aviso «Constraint_Error will be raised at run
time», y con `-gnatwE` un error; con `-gnatc` no se ve. Pero GNAT no genera código para una
especificación sola, así que `check_commands` cambia el comando por extensión:

```toml
[sources.code]
check_command = ["gcc", "-c", "-gnat2012", "-gnatwE", "{include}", "{file}"]
[sources.code.check_commands]
ads = ["gcc", "-c", "-gnatc", "-gnat2012", "{include}", "{file}"]
```

`run_program` necesita `program_command`, que recibe las líneas por su entrada estándar: el
programa mismo o, para un servidor, un cliente que manda cada línea y escribe la respuesta
(`["with_server", "session"]` en `atlas-socket-demo`).

`check_code` se ofrece por MCP en cuanto hay `check_command`. `run_test` y `run_program`
ejecutan código, así que solo se ofrecen si se piden en `[mcp] tools` o en las herramientas de un agente. Ningún
agente de serie las usa: `examples/agents/` trae dos de proyecto, `implementer` (cambia el
código para cumplir un requisito y compila lo que cambia) y `tester` (escribe una prueba con
entradas y salidas esperadas y la ejecuta). Las instrucciones de `tester` incluyen las reglas
de Ada en las que falló `gemma4:12b` (`use type` para comparar valores de un tipo de otro
paquete, `raise Program_Error with "..."`) y un modelo de prueba, que las pruebas de atlas
compilan y ejecutan.

Lo medido con `gemma4:12b` en un Mac de 24 GB, sobre una copia de las muestras:
- **Cambios pequeños, bien.** Añadir el número de traza a un mensaje de log, 2 de 2; poner una
  guarda en `Update`, 2 de 2 con la tolerancia de `edit_file` al listado de `read_file`.
- **Pruebas, a medias.** Para un `Classify` de cuatro ramas escribió una prueba que cubría las
  cuatro y los dos lados de cada límite, con los valores esperados correctos, en 1 de 2
  ejecuciones; en la otra no llegó a escribirla. Con el compilador, la prueba que escribió no
  compilaba (`Invalid` sin `Speeds.`) y `run_test` le devolvió los errores de GNAT, pero el
  modelo agotó el tiempo al procesarlos.
- **Cambios en especificación y cuerpo, mal.** Añadir un contador de trazas borradas con una
  función nueva: sin compilador, código que no compilaba en 2 de 2 (un `Delete_Track`
  duplicado, una variable con el nombre de la función); con él, el agente reescribió el
  fichero entero copiando un listado antiguo, no llamó a `check_code` y agotó las llamadas.
- **La memoria limita.** El modelo ocupa 16 GB; cuando el contexto del agente crece, el equipo
  usa la memoria de intercambio y una vuelta del modelo pasa de segundos a más de 10 minutos.

Las herramientas hacen su parte: los errores que cometió el modelo (`Delete_Track` duplicado,
nombres repetidos) los detecta `check_code` con GNAT, como comprueban las pruebas. Para que un
agente los corrija hace falta un modelo que siga las instrucciones en tareas de varios pasos.

## Tests desde los procedimientos de prueba

`atlas tests MÓDULO|ID` escribe un test por procedimiento de prueba (los de un módulo de DOORS,
o uno) y por lenguaje, a partir de sus pasos numerados, sin modelo. Cada paso se reconoce con
una expresión regular de `[[tests.steps]]` y se convierte en el código de ese paso en cada
lenguaje; el fichero sale de una plantilla del proyecto:

```toml
[tests.languages.ada]
path = "tests/ada/test_{slug}.adb"                 # {id} STP-0101, {slug} stp_0101, {Slug} Stp_0101
template = "requirements/templates/test.adb"       # el fichero entero, con {steps} en su línea
[tests.languages.python]
path = "tests/python/test_{slug}.py"
template = "requirements/templates/test.py"

[[tests.steps]]
match = 'Send `(?P<command>[^`]*)`; expect `(?P<expected>[^`]*)`'
ada = 'Expect ({n}, {command:str}, {expected:str});'
python = 'client.expect({n}, {command:str}, {expected:str})'
```

`match` cubre el texto entero del paso, sin su punto final. En el código del paso, `{n}` es su
número, `{text}` su texto y `{grupo}` un grupo con nombre de `match`, tal cual; `{grupo:str}`
lo escribe como cadena del lenguaje (`"..."` con las comillas dobladas en Ada; como JSON en los
demás). En la plantilla van `{id}`, `{slug}`, `{Slug}`, `{title}` (el encabezado en DOORS),
`{verifies}` (lo que verifica el procedimiento) y `{steps}`, sola en su línea: el código de los
pasos se pone ahí con su sangría. Las demás llaves se dejan como están.

Un procedimiento con un paso que ningún patrón reconoce no se escribe, y el paso se dice: hace
falta un patrón nuevo, o escribir el paso como los demás. Un fichero solo se escribe si cambia.

Por qué: los tests que escribía un modelo no siempre eran el procedimiento. Al pedirles
arreglar uno que fallaba, los agentes de los dos modelos probados lo adaptaron al código. Con
los pasos como patrones, el test es el procedimiento en todos los lenguajes, el procedimiento
es el único sitio donde se cambia, y el modelo solo hace falta para el cliente que usan los
tests (una vez). Con `protected`, los agentes no los pueden tocar.

## Como librería

```python
from pathlib import Path
from atlas.code import AdaAnalyzer, CodeTools
from atlas.config import load_config

unit = AdaAnalyzer({}).analyze_file(Path("tracks.adb"), Path("tracks.adb").read_bytes())
print(unit.unit, [(s["name"], s["reqs"]) for s in unit.subprograms])     # y types, variables, withs...
tools = CodeTools.from_config(load_config("atlas.dev.toml"))
tools.grep("Delete_Track")
agent = LlmAgent(..., tools=[tools.grep, tools.read_file, tools.edit_file])   # ADK
```

## Pruebas

`tests/test_code.py` (etiquetas, análisis, mismo resultado que `ada_graph.py`),
`tests/test_code_python.py` (el analizador de Python y sus pruebas en el grafo junto a las de
Ada),
`tests/test_code_tools.py` (confinamiento, lectura, búsqueda, reglas de edición,
codificación y tolerancia a los fallos de los modelos pequeños) y `tests/test_build_tools.py`
(con un compilador simulado en cualquier equipo, y con GNAT si está instalado).
