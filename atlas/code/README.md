# atlas.code — código fuente

Analiza el código (Ada con tree-sitter, Python con `ast`) y da a los agentes las herramientas habituales de
un agente de código, confinadas a las carpetas del proyecto. Viene de
`legacy/ada_graph.py`.

| Fichero | Qué tiene |
|---|---|
| `ada.py` | `AdaAnalyzer`, el analizador de Ada (plugin `code_analyzer:ada`) |
| `python.py` | `PythonAnalyzer`, el de Python (plugin `code_analyzer:python`), sin dependencias |
| `common.py` | la etiqueta de trazabilidad (`req_tag`, `req_regex`) |
| `tools.py` | `CodeTools`: `list_files`, `grep`, `read_file`, `edit_file`, `write_file`, `replace_subprogram`, `add_subprogram` |
| `symbols.py` | los subprogramas de un fichero con sus líneas (Ada con tree-sitter, Python con `ast`), para las herramientas que cambian un subprograma entero |
| `build.py` | `BuildTools`: `check_code`, `run_test`, `run_program` y `library_spec`, el compilador, las pruebas y el programa del proyecto |
| `library.py` | `Library`: las declaraciones públicas de las especificaciones del proyecto y del runtime del compilador (`[sources.code] library`) |
| `reference.py` | `Reference`: los manuales (`[sources.code] reference`) y las declaraciones de la biblioteca en un índice léxico propio, `<workspace>/reference.db` |
| `lsp.py` | `LanguageTools`: `find_definition`, `find_references` y `describe_symbol` de un servidor de lenguaje (el Ada Language Server) |
| `hints.toml` | las pistas de serie para los mensajes de GNAT |
| `rules.toml` | las reglas de serie para el texto nuevo de un cambio (Ada y Python) |
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
| `replace_subprogram` | sustituye un subprograma entero, buscado por su nombre (Ada y Python) |
| `add_subprogram` | añade subprogramas tras otro o al final de las declaraciones del paquete |

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

### La sintaxis antes de escribir y el compilador después

En el contexto de un agente, las herramientas que escriben reciben de `BuildTools` dos
comprobaciones:
- **Sintaxis.** Un cambio que deja mal la sintaxis de un fichero que estaba bien no se hace:
  el agente recibe los errores y las líneas de alrededor, como habría quedado el fichero. La
  comprueba `[sources.code] syntax_commands` por extensión, sobre una copia en
  `<workspace>/build/syntax` (GNAT: `gcc -c -gnats`, solo sintaxis); Python, con `ast`, sin
  comando. Un fichero nuevo se escribe aunque esté mal. Es lo que hace SWE-agent con su
  linter. En la tanda 4, un `begin` sin cerrar que dejó una edición siguió en 5 compilaciones
  de una ejecución de 37 minutos, hasta que se deshizo el encargo.
- **Compilación.** Con `check_on_edit` (por defecto), cada cambio devuelve en `check` lo que
  dice `check_code` del fichero: `ok` y los cinco primeros errores. El agente se ahorra la
  vuelta de `check_code` (el 17 % de sus llamadas en la tanda 4) y no puede decir que compila
  sin haberlo visto (`gemma-4-26b` lo dijo dos veces sin ser así).

### Pistas para los errores del compilador

Cada error de `check_code`, de `run_test`, de la comprobación de sintaxis y de la compilación
tras un cambio lleva una `hint` si alguna pista casa con su mensaje o con sus detalles. Las de
serie (`hints.toml`) salen de los errores de `gemma-4-26b` escribiendo un servidor en Ada con
GNAT: un `String` sin límites (16 veces en una tanda), argumentos de otro tipo, nombres que no
existen (`Ada.Command_Line.Arguments`), `with` que faltan, bloques sin cerrar, atributos
inventados (`'Second`)... Las del proyecto, en los ficheros de `[sources.code] hints`, van
antes:

```toml
[[hint]]
match = '"(?P<name>[^"]+)" not declared in "Controller"'
hint = "Controller declares Session and Execute; the commands are Do_* procedures of its body."
```

La regla del `String` ya estaba en las instrucciones del agente y el error salió igual 16
veces: la pista llega con el error, cuando hace falta. El informe de `atlas develop` lista los
errores de toda la tanda, los más frecuentes primero, y dice cuáles no tenían pista: es la
lista de lecciones que crece tras cada tanda.

### Reglas para el código nuevo

Lo que no ven ni el compilador ni los tests se queda: en la tanda 4, un `when others => null`
se tragó un `Constraint_Error` que ningún test veía y el cliente se quedaba sin respuesta. Las
reglas son expresiones regulares sobre las líneas que añade cada cambio (todas, en un fichero
nuevo), sin modelo. Las de serie (`rules.toml`) son avisos: `when others => null;` y
`pragma Suppress` en Ada, `except: pass` en Python. Las del proyecto, en los ficheros de
`[sources.code] rules` (relativos a `atlas.toml`), van antes:

```toml
[[rule]]
match = '(?i)\bdelay\s+\d'                  # expresión regular; \s cruza también los saltos de línea
message = "No delays in the server: use the timer of Controller."
level = "error"                              # error: el cambio no se hace; warning: se hace, con aviso
files = ["*.adb"]                            # opcional: los nombres de fichero a los que se aplica
```

Un `error` no se escribe: el agente recibe `error` con la línea y el mensaje, y la lista en
`rules`. Un `warning` se escribe y su mensaje va en `notes` de la respuesta, con la línea. Lo
que ya estaba en el fichero no cuenta: una edición en otra parte de un fichero que ya tenía un
`when others => null` pasa sin aviso.

### La biblioteca de verdad

`library_spec(unit, name="")` da las declaraciones públicas de una unidad: sus subprogramas con
sus parámetros, sus tipos, constantes y excepciones, con su comentario. Lee las
especificaciones de las raíces del código y las de las carpetas de `[sources.code] library`
(el runtime de GNAT, su carpeta `adainclude`), con tree-sitter: son las de la versión del
compilador que se usa. Encuentra una unidad por su nombre, no por el del fichero (GNAT los
acorta: `a-comlin.ads`), y de una instancia genérica (`GNAT.String_Split is new
GNAT.Array_Split`) da las de su genérico. Las 896 especificaciones del runtime se leen en unos
0,2 s, una vez por proceso.

Un error que nombra algo que no existe trae lo que sí existe:
- `"Arguments" not declared in "Command_Line"` lleva en `declared_in` lo que declara
  `Ada.Command_Line` parecido (`function Argument (Number : Positive) return String`) y todos
  sus nombres; el paquete se elige por los `with` del fichero del error;
- `"LF" is undefined` o `is not visible` lleva en `found_in` las unidades que declaran `LF`
  (`Ada.Characters.Latin_1`).

En la tanda 4, unos 25 de los cerca de 100 errores de compilación eran nombres inventados o sin su
`with`. Las instrucciones del `implementer` llevaban un resumen a mano de `GNAT.Sockets`
porque el modelo se inventaba su API; con `library_spec`, cualquier paquete.

### Los manuales

`search_reference(query, k=5)` busca por palabras en los manuales de `[sources.code]
reference` y en las declaraciones de la biblioteca (`library`, con su comentario), y da cada
pasaje con su origen: el manual y su sección, o `Unidad.Nombre`. Sirve para saber qué paquete
tiene algo o qué hace un pragma, un atributo o una opción del compilador:

| Búsqueda | Primer resultado |
|---|---|
| `split a string by a separator` | `GNAT.Array_Split.Create` (el genérico de `GNAT.String_Split`) |
| `Unbounded_String slice` | `Ada.Strings.Unbounded.Unbounded_Slice` |
| `pragma Assertion_Policy` | `gnat_rm.info: Pragma Assertion_Policy` |
| `-gnatwE warnings as errors` | `gnat_ugn.info: Warning Message Control` |
| `convert integer to string without leading space` | `Ada.Strings.Fixed.Trim` |

GNAT trae sus manuales en formato info (`share/info/gnat_rm.info` y `gnat_ugn.info` en la
instalación de Alire); también se leen Markdown, texto y HTML. Van en su propio índice,
`<workspace>/reference.db`, y no en `atlas.db`: `search` sigue buscando solo en los documentos
del proyecto. Se construye en la primera búsqueda (0,4 s con los dos manuales de GNAT y su
runtime: 2458 pasajes y 13 452 declaraciones) o con `atlas reference`, y se rehace lo que
cambia. Es búsqueda léxica (BM25), sin modelo. Las unidades internas de GNAT (`System.*`) no
entran. El manual de referencia del lenguaje (el ARM) no viene con GNAT: se puede descargar en
HTML y añadir su carpeta.

### El servidor de lenguaje

Con `[sources.code] language_server`, tres herramientas preguntan a un servidor de lenguaje lo
que sabe el compilador de un nombre:

| Herramienta | Qué da |
|---|---|
| `find_definition(path, line, name)` | dónde se declara un nombre escrito en esa línea: fichero, línea y la línea |
| `find_references(path, line, name)` | dónde se usa en el proyecto, con la declaración |
| `describe_symbol(path, line, name)` | qué es: su declaración (perfil, tipo) y su comentario |

El modelo da una línea y un nombre; la columna se busca en esa línea. Para Ada, el Ada
Language Server que trae la extensión de Ada de VS Code (`["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"]`):
atlas escribe un proyecto GPR en `<workspace>/build` con las carpetas de las raíces que tienen
fuentes Ada (o usa `language_server_project`), y el servidor encuentra el runtime de GNAT con
el `tool_path` del proyecto. El servidor arranca en la primera llamada y sigue en marcha
mientras dure el proceso; con el ALS 2026.3, la primera respuesta tarda un segundo y las
siguientes, milisegundos. `To_Unbounded_String` en `controller.adb` lleva a `a-strunb.ads`,
línea 122, con su perfil y su postcondición.

Es lo que hace Serena con el servidor de lenguaje, y la versión posible a través de una API de
la decodificación guiada por un monitor (MGD), que con el servidor de lenguaje sube un 19-25 %
el código que compila en modelos de todos los tamaños pero necesita entrar en la decodificación.

### Subprogramas enteros

`replace_subprogram(path, name, new_text, line=0)` sustituye un subprograma entero: el agente
da el subprograma nuevo completo, de `procedure Nombre` o `function Nombre` a su `end Nombre;`
(en Python, de `def` al final del cuerpo), y no tiene que copiar nada del fichero.
`add_subprogram(path, new_text, after="", before="")` añade subprogramas detrás de otro, delante
de otro (con su comentario: en Ada, un subprograma de un cuerpo tiene que ir antes del que lo
llama) o, sin ninguno de los dos, al final de las declaraciones del paquete: antes de `private`
en una especificación, antes del `begin` o del `end` de un cuerpo; en Python, al final del
fichero. Las dos:
- encuentran los subprogramas con tree-sitter (Ada) o `ast` (Python), en `symbols.py`;
- distinguen el cuerpo de la declaración (en Ada un nombre puede tener los dos en el mismo
  fichero) y cambian el que corresponde al texto nuevo;
- aceptan un nombre cualificado (`Controller.Execute`, `Calc.mul`), y `line` elige entre varios
  con el mismo nombre (sobrecargas);
- sangran el texto nuevo como el sitio donde va, y si empieza por comentarios, sustituyen
  también los de encima del subprograma (las etiquetas `Req:`);
- piden la misma lectura previa que `edit_file` y conservan codificación y finales de línea.

Salen de la tanda 4 de `atlas develop` con `gemma-4-26b`: 80 de sus 151 `edit_file` fallaron,
39 por un texto buscado de más de 30 líneas (quería sustituir bloques enteros) y 34 por no
copiarlo exacto. Unos 43 de los 293 minutos se fueron en esas vueltas, y dos encargos acabaron
en bucles de leer y editar mal. Con un subprograma por orden, como hace el plan del servidor,
cada encargo cambia o añade un subprograma pequeño.

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
