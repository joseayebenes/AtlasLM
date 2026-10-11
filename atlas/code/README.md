# atlas.code — código fuente

Analiza el código (Ada, con tree-sitter) y da a los agentes las herramientas habituales de
un agente de código, confinadas a las carpetas del proyecto. Viene de
`legacy/ada_graph.py`.

| Fichero | Qué tiene |
|---|---|
| `ada.py` | `AdaAnalyzer`, el analizador de Ada (plugin `code_analyzer:ada`) |
| `common.py` | la etiqueta de trazabilidad (`req_tag`, `req_regex`) |
| `tools.py` | `CodeTools`: `list_files`, `grep`, `read_file`, `edit_file`, `write_file` |
| `build.py` | `BuildTools`: `check_code` y `run_test`, el compilador y las pruebas del proyecto |

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

Un error es una respuesta con `error` (y, si el texto no aparece, las líneas más
parecidas) para que el agente se corrija.

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

También están registradas como plugins `tool` con esos nombres, para las herramientas de
los agentes y `[mcp] tools`. Lo que se edite se vuelve a analizar en el siguiente
`atlas run`.

## Compilador y pruebas

Un modelo que lee código no ve lo que ve un compilador: en un experimento, `gemma4:12b`
escribió código con la sintaxis bien que no habría compilado (un subprograma duplicado, una
variable con el nombre de una función). `BuildTools` le da dos herramientas:

| Herramienta | Qué hace |
|---|---|
| `check_code` | compila un fichero sin generar código y devuelve los errores y avisos, cada uno con fichero, línea, columna y mensaje (el primero de cada sitio, como mucho 10) |
| `run_test` | construye un procedimiento de prueba y lo ejecuta: pasa si termina con código 0. Si no compila, los errores; si falla, su salida (el mensaje del `Program_Error`) |

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

`check_code` se ofrece por MCP en cuanto hay `check_command`. `run_test` ejecuta código, así
que solo se ofrece si se pide en `[mcp] tools` o en las herramientas de un agente. Ningún
agente de serie las usa: `examples/agents/` trae dos de proyecto, `implementer` (cambia el
código para cumplir un requisito y compila lo que cambia) y `tester` (escribe una prueba con
entradas y salidas esperadas y la ejecuta).

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

`tests/test_code.py` (etiquetas, análisis, mismo resultado que `ada_graph.py`) y
`tests/test_code_tools.py` (confinamiento, lectura, búsqueda, reglas de edición,
codificación y tolerancia a los fallos de los modelos pequeños) y `tests/test_build_tools.py`
(con un compilador simulado en cualquier equipo, y con GNAT si está instalado).
