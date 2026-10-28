# atlas en VS Code

atlas se usa en VS Code como servidor MCP: el agente del editor (Copilot en modo agente, o
cualquier otro cliente MCP) le pregunta por los requisitos, los documentos, la trazabilidad y
el código antes de cambiar nada. atlas solo lee: no edita ficheros ni ejecuta el compilador.
Si además quieres que el agente compile, pase los tests y juzgue cada cambio con las reglas de
`develop`, añade el servidor de hephesto (al final de esta página).

## 1. Instalar

En el puesto, con el wheelhouse de la entrega o desde el repositorio (ver la
[guía de instalación](instalacion.md)):

```bash
python -m venv C:/atlas/venv
C:/atlas/venv/Scripts/python -m pip install --no-index --find-links wheelhouse "atlaslm[all]"
```

El servidor MCP ya viene con `atlaslm`: no hace falta ningún extra para él.

## 2. Preparar el proyecto

En la carpeta del proyecto, la que vas a abrir en VS Code, y con el Python del entorno de atlas:

```bash
C:/atlas/venv/Scripts/python -m atlas init --vscode
```

Esto hace dos cosas:

- escribe un `atlas.toml` comentado, si no existe ya (con `--profile gateway`, el del gateway
  corporativo);
- añade el servidor `atlas` a `.vscode/mcp.json`, al lado de `atlas.toml`, sin tocar los
  demás servidores que tenga el fichero.

El servidor se arranca con el mismo Python que ejecutó la orden
(`python -m atlas serve atlas.toml`), así que funciona igual con un entorno virtual que con el
wheelhouse, y VS Code no necesita saber dónde está el ejecutable `atlas`. Si `.vscode/mcp.json`
tiene comentarios, atlas no lo toca: te da la entrada para que la copies a mano.

Después edita `atlas.toml` con las fuentes y el modelo, y procesa una vez:

```bash
C:/atlas/venv/Scripts/atlas doctor atlas.toml
C:/atlas/venv/Scripts/atlas run atlas.toml
```

El servidor responde desde lo que `atlas run` deja en el workspace. Puedes volver a lanzar
`atlas run` con el servidor arrancado: lo nuevo se ve al momento.

## 3. Arrancarlo en VS Code

1. Abre la carpeta del proyecto (la de `atlas.toml`).
2. Paleta de órdenes → **MCP: List Servers** → `atlas` → **Start Server**. VS Code también
   puede ofrecer arrancarlo solo la primera vez que el agente lo necesite.
3. Comprueba el registro en el panel de salida **MCP: atlas**. Al arrancar dice cómo llega al
   modelo y qué variables de `atlas.toml` le faltan:

   ```text
   INFO atlas.mcp: MCP server over C:\proyecto\workspace with 13 tools (profile editor): ...
   INFO atlas.mcp: model: https://gateway.empresa/v1 through the proxy http://proxy:8080
   ```

4. En el chat, en modo agente, las herramientas de atlas aparecen en el selector de
   herramientas. Con código en `[sources.code]`, el perfil `editor` da `build_context`,
   `context_at`, `trace`, `trace_code`, `impact`, `similar_requirements`, `read_file`... y la
   búsqueda en los documentos ([configuración](configuracion.md), `[mcp] profile`).

El proxy y la clave tienen que estar en el entorno de VS Code, no solo en el de una terminal:
mira [el entorno del servidor](instalacion.md#el-entorno-del-servidor-proxy-y-clave). Con
`envFile` en la entrada de `mcp.json`, las contraseñas no se escriben en el repositorio.

## 4. Añadir hephesto (opcional)

Con `hephestolm` instalado en el mismo entorno:

```bash
C:/atlas/venv/Scripts/python -m pip install --no-index --find-links wheelhouse hephestolm
C:/atlas/venv/Scripts/python -m hephesto init --vscode
```

Esto añade el servidor `hephesto` junto al de atlas. Ofrece `check_code` (el compilador del
proyecto), `run_test`, y `run_tests` y `judge_changes`, el juez de `develop`: después de cada
cambio dice qué ha cambiado, si compila y qué tests han avanzado o retrocedido. Quien edita es el
agente del editor. Con `hephesto init --vscode --edit`, hephesto también ofrece sus herramientas
de ficheros (`edit_file`, `replace_subprogram`...), para un cliente que no tenga las suyas.

atlas nunca ofrece estas herramientas, aunque se pidan en `[mcp] tools`: lo que escribe o
ejecuta el código del proyecto es siempre de hephesto.
