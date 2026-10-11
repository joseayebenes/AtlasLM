# atlas.mcp — servidor MCP

`atlas serve` arranca un servidor MCP por stdio con las herramientas de documentos,
requisitos y código, para OpenCode, VS Code o cualquier otro cliente MCP. Sustituye a los
dos servidores anteriores (`doc_rag.py mcp` y `ada_graph_mcp.py`). Sus herramientas son los
plugins `tool` que usan también los agentes ADK, con los mismos nombres:

| Grupo | Herramientas | Clase |
|---|---|---|
| Documentos y requisitos | `list_documents`, `search`, `get_section`, `get_requirement`, `ask_docs` | `atlas.index.DocTools` |
| Grafo y trazabilidad | `graph_stats`, `find_symbol`, `search_code_docs`, `get_node`, `callers`, `callees`, `impact`, `call_chain`, `read_source`, `unit_info`, `type_users`, `variable_users`, `trace`, `trace_code`, `matrix`, `gaps`, `list_requirements`, `untraced`, `neighbors`, `communities` | `atlas.graph.GraphTools` |
| Ficheros de código | `list_files`, `grep`, `read_file`, `edit_file`, `write_file` | `atlas.code.CodeTools` |
| Compilador | `check_code`, `run_test` | `atlas.code.BuildTools` |
| Agentes | `build_context`, `verify_requirement`, `link_requirement`, `ask_atlas` | `atlas.agents.tools.AgentTools` |

Cambios respecto a los servidores anteriores:
- Tres herramientas cambian de nombre para que no se confundan:
  - `search_docs` de `doc_rag.py` es `search`;
  - `search_docs` de `ada_graph_mcp.py` (busca en los comentarios del código) es
    `search_code_docs`;
  - `requirement` (el código que cita un requisito) es `trace`, que ahora da también DOORS,
    documentos y pruebas.
- `get_requirement` devuelve también el texto oficial de DOORS, con su módulo, atributos,
  padre y enlaces. Si el ID no existe, sugiere IDs parecidos.
- `read_file` es el de `CodeTools`, que solo lee dentro de `[sources.code] roots`. El de
  `ada_graph_mcp.py` leía cualquier fichero.
- `get_section` acepta el ID que da `search` (`"004"`) y también el número o el título que
  la sección tiene en el documento (`"3"`, `"2.1"`, `"Logging"`). Si el número es el de un
  capítulo repartido en varias secciones, devuelve la lista de esas secciones.
- Todas responden con un diccionario JSON. Un problema llega como una respuesta con `error`
  (y a menudo con sugerencias), no como un fallo.

## Qué herramientas ofrece

- Con `[mcp] tools = []`, que es el valor por defecto, ofrece las de las fuentes
  configuradas:
  - con `[[sources.docs]]`, las de documentos;
  - con solo `[sources.doors]`, `get_requirement`, y `search` si `index` está entre los
    pasos;
  - con `[sources.code]`, las del grafo y `read_file`, y `check_code` si hay `check_command`;
  - sin código pero con DOORS o documentos, las de trazabilidad (`trace`, `matrix`, `gaps`,
    `list_requirements` y `neighbors`), si `graph` está entre los pasos;
  - con el grafo, `build_context`.
- `verify_requirement`, `link_requirement` y `ask_atlas` necesitan el modelo (y
  `ask_atlas`, también ADK): solo se ofrecen si están en la lista.
- Con una lista, ofrece exactamente esas herramientas.
- `edit_file` y `write_file` solo se ofrecen si están en la lista: atlas aporta conocimiento
  y el editor edita. El servidor las marca como herramientas que escriben, y el cliente
  puede pedir confirmación antes de usarlas. Lo mismo `run_test`, que ejecuta código.
- `[plugins] disabled = ["tool:ask_docs"]` quita una herramienta, y `[mcp] enabled = false`
  apaga el servidor.

El servidor responde con lo que hay en el workspace. Puedes lanzar `atlas run` con el
servidor arrancado: el grafo y los vectores se vuelven a leer cuando cambian, sin
reiniciarlo.

Las rutas de las fuentes son relativas a la carpeta desde la que se lanza atlas, así que el
cliente tiene que arrancar el servidor en la carpeta del proyecto (`cwd`). El servidor
arranca en medio segundo y escribe su registro por stderr, porque stdout es el canal del
protocolo.

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
[guía de instalación](../../docs/instalacion.md)). Si `atlas.toml` lee secretos del
entorno (`${ATLAS_API_KEY}`), el servidor los hereda del editor; si no, se pasan con
`environment` en OpenCode o con `env` o `envFile` en VS Code.

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

`atlas.mcp.create_server(cfg)` da el servidor (de la librería `mcp`) sin arrancarlo, y
`tool_names(cfg)` las herramientas que ofrecería.

## Pruebas

`tests/test_mcp.py`: las herramientas según la configuración, y `atlas serve` con un
cliente MCP real (documentos, DOORS, grafo y ficheros), incluido un `atlas run` con el
servidor arrancado.
