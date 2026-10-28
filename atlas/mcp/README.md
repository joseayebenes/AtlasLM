# atlas.mcp — servidor MCP

`atlas serve` arranca un servidor MCP por stdio con las herramientas de documentos,
requisitos y código, para OpenCode, VS Code ([cómo instalarlo](../../docs/vscode.md)) o cualquier
otro cliente MCP. Solo lee: lo que cambia o ejecuta el código lo ofrece `hephesto serve`. Sustituye a los
dos servidores anteriores (`doc_rag.py mcp` y `ada_graph_mcp.py`). Sus herramientas son los
plugins `tool` que usan también los agentes ADK, con los mismos nombres:

| Grupo | Herramientas | Clase |
|---|---|---|
| Documentos y requisitos | `list_documents`, `search`, `get_section`, `get_requirement`, `ask_docs` | `atlas.index.DocTools` |
| Grafo y trazabilidad | `graph_stats`, `find_symbol`, `search_code_docs`, `get_node`, `callers`, `callees`, `impact`, `call_chain`, `read_source`, `unit_info`, `type_users`, `variable_users`, `trace`, `trace_code`, `matrix`, `gaps`, `list_requirements`, `untraced`, `neighbors`, `communities` | `atlas.graph.GraphTools` |
| Ficheros de código | `list_files`, `grep`, `read_file`, `outline`, `read_subprogram` | `atlas.code.CodeReader` |
| Biblioteca y manuales | `library_spec`, `search_reference` | `atlas.code.Toolchain` |
| Servidor de lenguaje | `find_definition`, `find_references`, `describe_symbol` | `atlas.code.LanguageTools` |
| Agentes | `build_context`, `context_at`, `similar_requirements`, `verify_requirement`, `link_requirement`, `check_compliance`, `ask_atlas` | `atlas.agents.tools.AgentTools` |

Cambios respecto a los servidores anteriores:
- Tres herramientas cambian de nombre para que no se confundan:
  - `search_docs` de `doc_rag.py` es `search`;
  - `search_docs` de `ada_graph_mcp.py` (busca en los comentarios del código) es
    `search_code_docs`;
  - `requirement` (el código que cita un requisito) es `trace`, que ahora da también DOORS,
    documentos y pruebas.
- `get_requirement` devuelve también el texto oficial de DOORS, con su módulo, atributos,
  padre y enlaces. Si el ID no existe, sugiere IDs parecidos.
- `read_file` es el de `CodeReader`, que solo lee dentro de `[sources.code] roots`. El de
  `ada_graph_mcp.py` leía cualquier fichero.
- `get_section` acepta el ID que da `search` (`"004"`) y también el número o el título que
  la sección tiene en el documento (`"3"`, `"2.1"`, `"Logging"`). Si el número es el de un
  capítulo repartido en varias secciones, devuelve la lista de esas secciones.
- Las que toman un elemento de código (`trace_code`, `impact`, `read_source`, `get_node`,
  `build_context`...) aceptan también un fichero y una línea, `src/tracks.adb:12`: el
  elemento que contiene esa línea. `context_at` (`path`, `line`) es lo mismo para el
  fichero y la línea que el editor tiene abiertos.
- Todas responden con un diccionario JSON. Un problema llega como una respuesta con `error`
  (y a menudo con sugerencias), no como un fallo.

## Qué herramientas ofrece

Con `[mcp] tools = []`, que es el valor por defecto, el perfil (`[mcp] profile`) elige
entre las herramientas de las fuentes configuradas:

| Perfil | Herramientas | Por defecto |
|---|---|---|
| `editor` | `build_context`, `search`, `trace`, `impact` y `find_symbol` | con `[sources.code]` |
| `trace` | `trace`, `matrix`, `gaps`, `list_requirements`, `neighbors`, `get_requirement` y `search` | |
| `all` | todas las de las fuentes configuradas (abajo) | sin código |

`editor` es lo que necesita saber el agente de un editor antes de cambiar código, en 5
herramientas. Medido el 2026-10-28 con gemma-4-26b (sin razonamiento y con low, medium y high)
y con gpt-oss-120b, en 28 preguntas de asistente y de editor: con 5 aciertan igual o más que con
13 o con las 29, con la mitad de tokens de entrada (unos 4 400 por pregunta frente a 9 100), y
con 13 o 29 gpt-oss se perdía leyendo ficheros hasta agotar los pasos. Lo que falta en las 5 lo
cubren `build_context` (requisitos, firma, llamadores, pruebas, documentos y código de un
elemento, también por `fichero:línea`) y las herramientas del propio editor para leer y buscar
en los ficheros. `trace` es para quien revisa la trazabilidad sin tocar el código; `all`, o una
lista en `[mcp] tools`, ofrece más.

Las herramientas de las fuentes configuradas son:
- con `[[sources.docs]]`, las de documentos;
- con solo `[sources.doors]`, `get_requirement`, y `search` si `index` está entre los
  pasos;
- con `[sources.code]`, las del grafo y `read_file`;
- sin código pero con DOORS o documentos, las de trazabilidad (`trace`, `matrix`, `gaps`,
  `list_requirements` y `neighbors`), si `graph` está entre los pasos;
- con el grafo, `build_context`, y con el del código, `context_at` y
  `similar_requirements`.

Un perfil solo quita: no ofrece nada que las fuentes no tengan. Además:
- Con una lista en `[mcp] tools`, ofrece exactamente esas herramientas, sea cual sea el
  perfil.
- `verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` necesitan el
  modelo (y `ask_atlas`, también ADK): solo se ofrecen si están en la lista.
- Las que escriben o ejecutan el código (`edit_file`, `check_code`, `run_test`,
  `judge_changes`...) no se ofrecen nunca, ni en la lista: son de hephesto (abajo).
- `[plugins] disabled = ["tool:ask_docs"]` quita una herramienta, y `[mcp] enabled = false`
  apaga el servidor.

Las instrucciones del servidor explican cada grupo de herramientas ofrecidas, y solo nombran
las que se ofrecen: el párrafo de los documentos, una frase por cada una de `search`,
`list_documents`, `get_section`, `get_requirement` y `ask_docs`; el del contexto, por
`build_context`, `context_at` y `similar_requirements`; y el del grafo, el flujo habitual
(`find_symbol -> impact` en el perfil `editor`, con `get_node`, `callers`, `callees` y
`read_source` en `all`) y lo que dan `trace`, `trace_code`, `impact`, `matrix` y `gaps`. Con
`trace`, también cómo leerlo: lo que un requisito satisface está en `ancestors` y `links_out`,
y `parent` es solo su sitio en el esquema de DOORS (gemma-4-26b y gpt-oss-120b los confundían);
si DOORS y un documento difieren (`values_differ`), manda DOORS; y un `implemented_by` vacío es
que aún no hay código. Además cuentan, en un
párrafo «Traceability of this project», cómo cita el código los requisitos (`req_tag` o
`req_regex`), cómo son sus ID (`id_pattern`), qué módulos de DOORS hay y los pasos de un
cambio: contexto (`context_at` o `build_context`), editar, compilar y pasar las pruebas que da
`trace`, con las herramientas que se ofrecen (`check_code` y `run_test` si es el servidor de
hephesto el que las da).

Cada herramienta lleva un título (`Trace code`) y unas anotaciones que el cliente puede
usar para pedir confirmación o repetir llamadas sin preguntar:
- todas las de atlas solo leen (`readOnlyHint`); en el servidor de hephesto, las que escriben
  o ejecutan una prueba son destructivas (`destructiveHint`) y `check_code` no es de solo
  lectura (deja lo que compila), pero no destruye nada;
- y las que no preguntan al modelo (todas menos `ask_docs`, `ask_atlas`,
  `verify_requirement`, `link_requirement` y `check_compliance`) dan la misma respuesta a la
  misma llamada y no salen del workspace y el código (`idempotentHint`, y `openWorldHint`
  falso).

El servidor responde con lo que hay en el workspace. Puedes lanzar `atlas run` con el
servidor arrancado: el grafo y los vectores se vuelven a leer cuando cambian, sin
reiniciarlo.

Las rutas de `atlas.toml` son relativas a su carpeta, así que el servidor encuentra las
fuentes aunque el cliente lo arranque en otra; las rutas de las herramientas de ficheros
también parten de ella. Aun así conviene arrancarlo en la carpeta del proyecto (`cwd`):
`atlas.toml` se busca desde ahí si no se da su ruta completa. El servidor
arranca en medio segundo y escribe su registro por stderr, porque stdout es el canal del
protocolo.

Cada llamada del editor queda en el registro de uso del workspace (la tabla `usage` de
`atlas.db`, `[mcp] usage_log`): la herramienta, los milisegundos, si falló, si la respuesta
de `ask_docs` tenía fuentes, y las peticiones, tokens y coste que costó. Por defecto sin el
texto de los argumentos. `atlas usage --since 30d` lo resume por herramienta y semana.

## Lo que escribe o ejecuta el código

atlas solo lee el código. Una herramienta que cambia ficheros o ejecuta comandos del proyecto
(`edit_file`, `check_code`, `run_test`, `run_tests`, `judge_changes`...) es de hephesto, y
`atlas serve` no la ofrece nunca, tampoco si se pide en `[mcp] tools` (es un error que remite a
`hephesto serve`). Las ofrece [hephesto serve](../../hephestolm/docs/servidor.md), sobre este
mismo servidor.

## Configurar el editor

OpenCode (`opencode.json` en la carpeta del proyecto):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "atlas": {
      "type": "local",
      "command": ["/ruta/al/entorno/bin/atlas", "serve", "atlas.toml"],
      "cwd": ".",
      "enabled": true
    }
  }
}
```

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "atlas": {
      "type": "stdio",
      "command": "/ruta/al/entorno/bin/atlas",
      "args": ["serve", "atlas.toml"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

En Windows, el ejecutable es `<entorno>\Scripts\atlas.exe` (ver la
[guía de instalación](../../docs/instalacion.md)).

El servidor recibe el entorno del cliente, no el de la terminal. Si `atlas.toml` lee el
proxy o la clave del entorno (`${HTTPS_PROXY}`, `${ATLAS_API_KEY}`), el cliente tiene que
tenerlos: un editor abierto antes del `setx` hay que reiniciarlo, y los clientes hechos con
la librería de MCP (Claude Desktop, el `McpToolset` de ADK) solo pasan `PATH` y pocas más.
Se dan con `environment` en OpenCode, con `env` o `envFile` en VS Code y con `env` en los
demás. Al arrancar, el servidor escribe en su registro cómo llega al modelo (con proxy o
sin él) y qué variables de `atlas.toml` no tiene; las herramientas que usan el modelo las
añaden a su error. Ver [el entorno del
servidor](../../docs/instalacion.md#el-entorno-del-servidor-proxy-y-clave).

## Como librería

Las herramientas también se usan sin MCP:

```python
from atlas.config import load_config
from atlas.graph import GraphTools
from atlas.index import DocTools
cfg = load_config("atlas.dev.toml")
DocTools.from_config(cfg).get_requirement("SRS-0004")    # DOORS y documentos
GraphTools.from_config(cfg).trace("SRS-0004")            # DOORS, documentos, código y pruebas
```

`atlas.mcp.create_server(cfg)` da el servidor (de la librería `mcp`) sin arrancarlo,
`tool_names(cfg)` las herramientas que ofrecería y `profile(cfg)` el perfil que aplica.

## Pruebas

`tests/test_mcp.py`: las herramientas según la configuración, y `atlas serve` con un
cliente MCP real (documentos, DOORS, grafo y ficheros), incluido un `atlas run` con el
servidor arrancado.
