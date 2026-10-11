# atlas.code — código fuente

Analiza el código (Ada, con tree-sitter) y da a los agentes las herramientas habituales de
un agente de código, confinadas a las carpetas del proyecto. Viene de
`legacy/ada_graph.py`.

| Fichero | Qué tiene |
|---|---|
| `ada.py` | `AdaAnalyzer`, el analizador de Ada (plugin `code_analyzer:ada`) |
| `common.py` | la etiqueta de trazabilidad (`req_tag`, `req_regex`) |
| `tools.py` | `CodeTools`: `list_files`, `grep`, `read_file`, `edit_file`, `write_file` |

## Análisis

El paso `code.parse` analiza cada fichero y guarda en `atlas.db`:
- sus subprogramas, con sus líneas, sus llamadas propias, el comentario de cabecera (el del
  `.ads` si lo hay) y los requisitos que citan;
- sus tipos, variables, constantes y `with`.

Los requisitos se leen de la etiqueta `[sources.code] req_tag` (`"Req:"` por defecto). Para
otro formato, `req_regex`, cuyo grupo 1 es la lista de IDs. Los ficheros que casan con
`test_patterns` son pruebas. Con eso, el paso `graph` construye el grafo de código (ver
[atlas.graph](../graph/README.md)).

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

También están registradas como plugins `tool` con esos nombres, para las herramientas de
los agentes y `[mcp] tools`. Lo que se edite se vuelve a analizar en el siguiente
`atlas run`.

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
codificación y tolerancia a los fallos de los modelos pequeños).
