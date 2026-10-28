# atlas.code — código fuente

Analiza el código (Ada con tree-sitter, Python con `ast`) y da a los agentes las herramientas de lectura de
un agente de código, confinadas a las carpetas del proyecto. atlas no cambia ni ejecuta el código.

| Fichero | Qué tiene |
|---|---|
| `ada.py` | `AdaAnalyzer`, el analizador de Ada (plugin `code_analyzer:ada`) |
| `python.py` | `PythonAnalyzer`, el de Python (plugin `code_analyzer:python`), sin dependencias |
| `common.py` | la etiqueta de trazabilidad (`req_tag`, `req_regex`) |
| `files.py` | `CodeReader`: `list_files`, `grep`, `read_file`, `outline` y `read_subprogram`, la lectura del código |
| `symbols.py` | los subprogramas de un fichero con sus líneas (Ada con tree-sitter, Python con `ast`), para las herramientas que cambian un subprograma entero |
| `toolchain.py` | `Toolchain`: `tool_path`, la carpeta de compilación, `library_spec` y `search_reference`, sin ejecutar nada |
| `library.py` | `Library`: las declaraciones públicas de las especificaciones del proyecto y del runtime del compilador (`[sources.code] library`) |
| `reference.py` | `Reference`: los manuales (`[sources.code] reference`) y las declaraciones de la biblioteca en un índice léxico propio, `<workspace>/reference.db` |
| `lsp.py` | `LanguageTools`: `find_definition`, `find_references` y `describe_symbol` de un servidor de lenguaje (el Ada Language Server) |
| `gpr.py` | `read_project`: las carpetas de fuentes y de objetos de un proyecto GPR y de los que importa (`[sources.code] project`), para las raíces, `exclude` y los comandos de gprbuild |

Lo que cambia el código, lo compila o lo prueba (`CodeTools`, `BuildTools`, `JudgeTools`, las
pistas, las reglas y los tests de los procedimientos) es de hephesto:
[sus herramientas](../../hephestolm/docs/codigo.md).

## Análisis

El paso `code.parse` analiza cada fichero y guarda en `atlas.db`:
- sus subprogramas, con su clase (`kind`: `procedure`, `function`, `method`, `task`...), su
  perfil, sus líneas, sus llamadas propias con la línea y la columna de cada una, el
  comentario de cabecera (el del `.ads` si lo hay) y los requisitos que citan;
- sus tipos, variables, constantes y `with`.

En Ada, la clase es `procedure`, `function`, `operator` (`function "+"`, que se llama `"+"`),
`task` (`task body`), `entry` (`entry ... when`, la de un objeto protegido) o `protected`
(`protected body`, cuyas llamadas y etiquetas propias son las que no están en sus entradas y
subprogramas). El prefijo de un atributo (`T` en `T'Image (X)`) no es una llamada. Dos
subprogramas con el mismo nombre en un fichero salen dos veces; el grafo los junta en un
nodo. En Python, todo es `function`.

El analizador Ada guarda también los paquetes de las cláusulas `use` (`uses`, sin los `use
type`) y los alias (`aliases`, nombre → destino): los renombramientos de subprogramas y de
paquetes y las instancias genéricas. El grafo los usa para resolver las llamadas.

Los requisitos se leen de la etiqueta `[sources.code] req_tag` (`"Req:"` por defecto). Para
otro formato, `req_regex`, cuyo grupo 1 es la lista de IDs. Los IDs de una etiqueta son los
que casan con la expresión de un ID de `[sources.doors.convention]` (`id_regex`, o
`id_pattern` sin anclas) en el resto de su línea: `Req: SRS-0004 and SRS-0005`, `Req: see
SRS-0004` y `Req: SRS-0004 / SRS-0005` dan los IDs que se esperan. El paso `code.parse` la
pasa a cada analizador en su configuración (`id_regex`, que `requirement_ids` compila) y la
incluye en su huella. Sin convención, las palabras que parecen un ID se leen hasta la primera
que no lo parece. Los ficheros que casan con
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

## La interfaz `CodeUnit`

Es lo que `CodeAnalyzer.analyze_file` devuelve de un fichero y lo que `code.parse` guarda
(`atlas/plugins.py`):

| Campo | Qué es |
|---|---|
| `file`, `unit`, `language` | el fichero, su unidad (`tracks.db`, `python.test_session`) y el lenguaje, el nombre del analizador |
| `subprograms` | `{name, start_line, end_line, calls, doc, reqs}` y, si el analizador las sabe, `kind` (`procedure`, `function`, `method`, `task`, `entry`, `operator`) y `signature`, el perfil en una línea (`procedure Update (I : Integer)`, `def send(self, line)`) |
| `subprograms[].calls` | cada llamada, como el nombre escrito (`"pkg.op"`) o como `{"name", "line", "column"}`: dónde empieza el nombre, las dos cosas contadas desde 1 y la columna en caracteres. Ada y Python dan una por cada sitio donde se llama; el grafo toma cada nombre una vez por subprograma |
| `declarations` | las de una especificación: `{name, line, doc, reqs}` |
| `types`, `variables`, `imports` | tipos, variables y constantes del paquete o módulo, y sus `with` o `import` |

`CodeAnalyzer.predefined` son los nombres del lenguaje que nunca son llamadas: en Ada,
`Integer`, `Float`, `String`...; en Python ninguno, porque el analizador ya deja fuera los
`builtins`. El grafo los busca en el analizador del lenguaje de la unidad que llama, así que
`integer (...)` se ignora en Ada y es una llamada externa en Python, y otro lenguaje no
tiene que tocar `atlas.graph`. `CodeUnit.from_dict` lee lo que guardaron versiones anteriores,
sin el campo `tests` (no lo leía nadie y ya no existe) y con las llamadas sin posición.

La posición de cada llamada es lo que necesitaría un paso que preguntase al servidor de
lenguaje (`textDocument/definition`) por cada una y guardase aristas exactas. Medido en
octubre de 2026, no compensa todavía: la resolución por nombres no deja ninguna llamada
ambigua en los proyectos Ada que hay a mano, y donde el Ada Language Server sabe la
definición dentro del proyecto da la misma arista (ver [atlas.graph](../graph/README.md#precisión-de-la-resolución-por-nombres)).

## Lectura del código

`CodeReader` (`files.py`) tiene las herramientas de lectura de un agente de código sobre
`[sources.code] roots`. No tocan nada fuera de las raíces ni de lo excluido con `exclude`;
tampoco con `..` ni con enlaces simbólicos. Lo que se cambia, se compila o se prueba es de
hephesto ([sus herramientas](../../hephestolm/docs/codigo.md)), que parte de un `CodeReader`.

| Herramienta | Qué hace |
|---|---|
| `list_files` | ficheros por patrón glob (`*.adb` busca en todas las subcarpetas) |
| `grep` | expresión regular, con fichero, línea y líneas de contexto |
| `read_file` | un fragmento numerado |
| `outline` | los subprogramas de un fichero (Ada y Python), con sus líneas, su firma y los requisitos de su comentario, y sus `with` o `import` |
| `read_subprogram` | un subprograma, numerado, con su comentario de encima, buscado por su nombre |

`Toolchain` (`toolchain.py`) sabe dónde está el compilador (`tool_path`), cuál es la carpeta de
compilación y el proyecto GPR, sin ejecutar nada: da `library_spec` y `search_reference`, y el
servidor de lenguaje parte de él.

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

## Como librería

```python
from pathlib import Path
from atlas.code import AdaAnalyzer, CodeReader
from atlas.config import load_config

unit = AdaAnalyzer({}).analyze_file(Path("tracks.adb"), Path("tracks.adb").read_bytes())
print(unit.unit, [(s["name"], s["reqs"]) for s in unit.subprograms])     # y types, variables, withs...
reader = CodeReader.from_config(load_config("atlas.dev.toml"))
reader.grep("Delete_Track")
agent = LlmAgent(..., tools=[reader.grep, reader.read_file, reader.outline])   # ADK
```

## Pruebas

`tests/test_code.py` (etiquetas y los IDs de la convención, análisis, el fichero de la sonda con
tareas, entradas, cuerpos protegidos, operadores, sobrecargas y atributos, posiciones de las
llamadas, lo guardado por la 0.2, mismo resultado que `ada_graph.py`; el proyecto GPR con
proyectos de pega —variables, `with`, `**`— y con GNAT, gprbuild y el Ada Language Server si
están instalados),
`tests/test_code_python.py` (el analizador de Python y sus pruebas en el grafo junto a las de
Ada),
`tests/test_toolchain.py` (`library_spec`, `search_reference` y el servidor de lenguaje, con
GNAT y el Ada Language Server si están instalados) y `tests/test_layers.py` (atlas no importa
hephesto, y sus herramientas de lectura no crean nada que escriba o ejecute). Las de editar,
compilar y probar están en `hephestolm/tests`.
