# Instalación

Esta guía basta para instalar atlas en un puesto nuevo, con o sin acceso a internet. Para
desarrollar atlas, ver [Instalación para desarrollo](#instalación-para-desarrollo).

## Qué se entrega

| Fichero | Qué es |
|---|---|
| `atlaslm-<versión>-py3-none-any.whl` | atlas: código Python puro, el mismo para Windows, macOS y Linux |
| `wheelhouse-win_amd64-py312/` | el wheel de atlas y todas sus dependencias, para Windows de 64 bits con Python 3.12: se instala sin red |
| `constraints.txt` | la versión exacta de cada dependencia con la que pasan las pruebas |

El wheelhouse vale para un sistema y una versión de Python. Se prepara desde cualquier
equipo con red (ver [Preparar la entrega](#preparar-la-entrega)).

## Requisitos

- **Python 3.10 a 3.13**, de 64 bits. Se recomienda 3.12. La instalación limpia y las
  pruebas se han pasado en macOS con 3.10, 3.11, 3.12 y 3.13. En Windows se ha comprobado
  que todas las dependencias tienen wheel para Python 3.12 de 64 bits; falta instalarlo en
  un puesto.
- **Un modelo de chat y uno de embeddings** con API compatible con OpenAI: el gateway
  corporativo, o Ollama o LM Studio en local.
- Nada más: ni servicios ni bases de datos. Todo se guarda en la carpeta de trabajo
  (`workspace`) del proyecto.

## 1. Instalar

### Windows, sin red (wheelhouse)

En PowerShell, con el wheelhouse copiado, por ejemplo, en `C:\atlas\wheelhouse`:

```powershell
py -3.12 -m venv C:\atlas\venv
C:\atlas\venv\Scripts\python -m pip install --no-index --find-links C:\atlas\wheelhouse "atlaslm[all]"
C:\atlas\venv\Scripts\atlas --version
```

`--no-index` impide que pip busque en internet: todo sale del wheelhouse.

### Desde el repositorio interno de paquetes

Si el wheel está publicado en el repositorio interno (Nexus, Artifactory, devpi...):

```powershell
py -3.12 -m venv C:\atlas\venv
C:\atlas\venv\Scripts\python -m pip install "atlaslm[all]==0.2.0" -c constraints.txt --index-url https://<repositorio>/simple
```

`-c constraints.txt` instala exactamente las versiones probadas.

### macOS o Linux

```bash
python3.12 -m venv ~/atlas/venv
~/atlas/venv/bin/python -m pip install --no-index --find-links ~/atlas/wheelhouse "atlaslm[all]"
~/atlas/venv/bin/atlas --version
```

### Qué instalar

`atlaslm[all]` instala todo. Para algo más ligero, se eligen las partes:

| Extra | Para qué | Sin él |
|---|---|---|
| `docs` | leer PDF y Office (markitdown, pymupdf) | solo Markdown y texto |
| `code` | analizar Ada y construir el grafo (tree-sitter, networkx) | sin código ni grafo |
| `mcp` | `atlas serve`, el servidor para OpenCode y VS Code | sin servidor MCP |
| `xlsx` | `atlas matrix` en `.xlsx` (openpyxl); no está en `all` | solo CSV y Markdown |
| `agents` | agentes ADK: `atlas agent`, `verify`, `impact`, `coverage`, `eval` (google-adk 2.11) | sin agentes |

Por ejemplo, `"atlaslm[docs,code,mcp]"` para procesar y servir sin agentes.

### El compilador de Ada (opcional)

Para que los agentes compilen el código y ejecuten las pruebas (`check_code` y `run_test`, ver
[atlas.code](../atlas/code/README.md)), GNAT. GNAT FSF es libre (GPL 3 con la excepción de la
biblioteca de ejecución de GCC: los programas que compila se pueden distribuir con cualquier
licencia). En un Mac con Apple Silicon no está en Homebrew; se instala con Alire, el gestor
de paquetes de Ada:

```bash
curl -LO https://github.com/alire-project/alire/releases/download/v2.1.1/alr-2.1.1-bin-aarch64-macos.zip
unzip alr-2.1.1-bin-aarch64-macos.zip -d ~/.local/alire
~/.local/alire/bin/alr -n toolchain --select gnat_native gprbuild
```

Deja GNAT y gprbuild en `~/.local/share/alire/toolchains` (unos 640 MB con GNAT 16.1 y
gprbuild 26.0) sin tocar la configuración de la shell: `[sources.code] tool_path` los pone
delante del `PATH` solo para atlas. En Windows, Alire tiene instalador propio. Un proyecto que
ya use GNAT Pro pone sus comandos en `check_command` y `test_command`.

## 2. Crear el proyecto

```powershell
mkdir C:\proyectos\tms
cd C:\proyectos\tms
C:\atlas\venv\Scripts\atlas init --profile gateway
```

`atlas init` escribe un `atlas.toml` comentado con todas las secciones. Con `--profile
gateway`, el del gateway corporativo, con los valores que ya se saben buenos:
`reasoning_effort = "none"` (gemma 4 razona por defecto; un agente que lo necesita lo
enciende en su fichero), `requests_per_minute = 28`, `max_concurrent = 6`, `workers = 6`,
`ca_cert = "system"` y `[search] scope_chars = 300000`. Sin `--profile` (o con `--profile
ollama`), el de un modelo local en Ollama. Hay que cambiar:

1. `[llm] base_url` y `model`: el gateway (`https://<gateway>/v1` en la plantilla) o el
   modelo local.
2. Las fuentes que se tengan: `[[sources.docs]] paths`, `[sources.code] roots` y, con la
   base de requisitos, `[sources.doors] enabled = true` y su `path`.

La [guía de configuración](configuracion.md) explica cada clave.

## 3. Secretos y red

La clave del gateway no se escribe en `atlas.toml`: se lee del entorno, siempre de
`ATLAS_API_KEY`. La plantilla del gateway dice `api_key = "${ATLAS_API_KEY}"` (y la de Ollama,
`"${ATLAS_API_KEY:-local}"`, que sin la variable vale `local`).

```powershell
setx ATLAS_API_KEY "la-clave"            # Windows: abre otra terminal después
```

```bash
export ATLAS_API_KEY="la-clave"          # macOS o Linux: en ~/.zshrc o ~/.bashrc
```

En la red corporativa:
- **Proxy**: `[network] proxy = "${HTTPS_PROXY:-}"` lo toma del entorno. Si el gateway es
  interno y no pasa por el proxy, va en `no_proxy`.
- **Certificados**: si un proxy inspecciona TLS, `[network] ca_cert = "system"` usa el
  almacén de certificados de Windows (o de macOS), donde ya está la raíz corporativa.

atlas solo se conecta a las URL de `[llm]` y `[embeddings]`. Ver
[seguridad](seguridad.md).

## 4. Comprobar y procesar

```powershell
atlas doctor atlas.toml     # lo que puede fallar en este puesto, antes de procesar nada
atlas check atlas.toml      # una petición real al chat y otra a los embeddings
atlas probe atlas.toml      # el modelo como agente: herramientas y razonamiento (antes de usar agentes)
atlas run atlas.toml        # procesamiento incremental: la primera vez, todo
atlas status atlas.toml     # qué hay, qué falló y el perfil de tiempos de la última ejecución
```

`atlas doctor` solo lee: no crea el workspace ni toca las fuentes. Imprime una tabla con una
línea por comprobación (`OK`, `WARN` o `FAIL`, dónde y qué; debajo, cómo arreglarlo) y sale
con 1 si algo falla:

- la versión de Python y la plataforma;
- los extras que importan los pasos y herramientas configurados (`tree_sitter_ada`,
  `pymupdf`, `mcp`, `google.adk`);
- cada ruta de `[[sources.docs]] paths` y de `[sources.code] roots`: si existe y cuántos
  ficheros daría (0 es un aviso: una raíz mal escrita no procesa nada);
- la base de intercambio de DOORS (`[sources.doors.exchange] path`): si abre, su versión de
  esquema, sus módulos y cuándo se rellenó;
- los comandos de `[sources.code]` (`check_command`, `test_command`, `syntax_commands`,
  `language_server`...) buscados en el `PATH` y en las carpetas de `tool_path`;
- las variables de entorno que nombra el TOML y no están puestas;
- si el workspace existe y se puede escribir;
- y, al final, la conexión: la misma petición al chat y a los embeddings que `atlas check`.

Si `check` falla, dice por qué: certificado, proxy, credenciales, URL o modelo. Con
`C:\atlas\venv\Scripts` en el `PATH` basta con `atlas`; si no, la ruta completa.

`atlas run` se puede interrumpir (Ctrl+C, un corte de red, un apagado): al relanzarlo sigue
donde se quedó. Una segunda ejecución sin cambios no hace ninguna petición al modelo.

### Cuánto va a tardar

Con el gateway (28 peticiones por minuto, unos 30 s cada una) la primera ingesta de un
proyecto real son horas. Antes de lanzarla:

```powershell
atlas estimate atlas.toml               # por paso: unidades, peticiones y tiempo
atlas estimate atlas.toml --latency 30  # sin ninguna ejecución anterior que mida la latencia
```

```
docs.process: 80 units, 2160 chat requests, about 77-85 min at 28/min
index.documents: after docs.process
```

Las peticiones de `docs.process` son las secciones que no están en la caché más un resumen
por documento; el tiempo va de esa cifra a la misma con un 10 % más (secciones que se
dividen). Los pasos que dependen de otros con trabajo pendiente («after docs.process») no
se pueden estimar hasta que esos terminen. `atlas status` repite la estimación y `atlas
run` la escribe al empezar cada paso y, mientras trabaja, una línea de progreso cada 30 s o
cada 25 unidades:

```
docs.process 12/80 units, 15 min, ~1 h 10 min left
```

### De noche, con un límite

`atlas run` puede parar solo, con el estado guardado, y seguir la próxima vez:

```powershell
atlas run atlas.toml --until 07:00      # no empieza nada después de las 7:00 (de mañana, si ya pasó)
atlas run atlas.toml --max-minutes 300  # o pasadas 5 horas
atlas run atlas.toml --max-requests 5000
```

También en `atlas.toml`, `[pipeline] max_minutes` y `max_requests` (ver
[configuración](configuracion.md#pipeline)). Al llegar al límite no empieza ninguna unidad
más, un documento a medias deja de pedir secciones (las que ya tiene quedan en la caché) y
sale con el código 3; la siguiente ejecución lo retoma sin repetir lo hecho.

Para lanzarla cada noche en Windows, con el **Programador de tareas**:

1. *Crear tarea…* (no «tarea básica»), con un nombre como `atlas nocturno`; marca
   «Ejecutar tanto si el usuario inició sesión como si no» si el puesto se queda bloqueado.
2. *Desencadenadores*: diario, a las 21:00.
3. *Acciones*: iniciar un programa.
   - Programa: `C:\atlas\venv\Scripts\atlas.exe`
   - Argumentos: `run atlas.toml --until 07:00`
   - Iniciar en: la carpeta del proyecto, la del `atlas.toml` (las rutas de las fuentes son
     relativas a ella).
4. *Configuración*: «Detener la tarea si se ejecuta durante más de» 12 horas, como red de
   seguridad. Aunque el Programador la mate, el motor sobrevive: la próxima ejecución
   rehace solo la unidad que estaba en marcha.

Las variables de la clave y del proxy tienen que estar en el entorno del usuario de la
tarea (variables de usuario de Windows, no las de una consola abierta). Para guardar el
registro, una acción con `cmd.exe` y los argumentos
`/c atlas run atlas.toml --until 07:00 >> logs\atlas-run.log 2>&1`. Por la mañana,
`atlas status atlas.toml` dice qué se hizo y qué queda.

### Códigos de salida de `atlas run`

| Código | Qué pasó | Qué hacer |
|---|---|---|
| 0 | todo al día | nada |
| 1 | alguna unidad falló (se anota y se sigue) | `atlas status` dice cuáles y por qué; la próxima ejecución las reintenta |
| 2 | un error fatal paró la ejecución (conexión, credenciales, cupo agotado) | corregir la causa y relanzar: sigue donde se quedó |
| 3 | se alcanzó el límite (`--until`, `--max-minutes`, `max_requests`) | relanzar cuando toque: sigue donde se quedó |

## 5. Conectar el editor

`atlas serve atlas.toml` es un servidor MCP por stdio. Conviene que el editor lo arranque en
la carpeta del proyecto (`cwd`), para que encuentre `atlas.toml`; las rutas de dentro del
fichero son relativas a él, así que con su ruta completa funciona desde cualquier carpeta.

OpenCode (`opencode.json` en la carpeta del proyecto):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "atlas": {
      "type": "local",
      "command": ["C:/atlas/venv/Scripts/atlas.exe", "serve", "atlas.toml"],
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
      "command": "C:/atlas/venv/Scripts/atlas.exe",
      "args": ["serve", "atlas.toml"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

### El entorno del servidor: proxy y clave

El editor arranca `atlas serve` con su entorno, no con el de la terminal. Por eso
`atlas check` puede funcionar en PowerShell y el servidor no tener el proxy ni la clave:

- `$env:HTTPS_PROXY = "..."` solo vale en esa ventana de PowerShell, y lo que hay en el
  `$PROFILE`, solo en PowerShell. Para los demás programas: `setx HTTPS_PROXY "..."` (o
  «Editar las variables de entorno de esta cuenta») y reiniciar el editor, cerrando todas
  sus ventanas. Abrir el editor desde esa PowerShell (`code .`, `opencode`) también le da
  sus variables.
- Los clientes hechos con la librería de MCP (Claude Desktop, el `McpToolset` de ADK o un
  script con `stdio_client`) solo pasan unas pocas variables: `PATH`, `USERPROFILE`,
  `APPDATA`, `TEMP` y alguna más. El proxy, su usuario y contraseña y la clave hay que
  dárselos en `env`, aunque estén en el entorno.
- En OpenCode van en `environment` y en VS Code en `env` o `envFile`. Con `envFile` las
  contraseñas no se escriben en `.vscode/mcp.json`: van en un fichero fuera del repositorio.

Al arrancar, el servidor escribe en su registro (en VS Code, en la salida «MCP: atlas»)
cómo llega al modelo y qué variables de `atlas.toml` no tiene:

```text
INFO atlas.mcp: model: https://gateway.empresa/v1 without proxy
WARNING atlas.mcp: HTTPS_PROXY of atlas.toml not set in the environment of atlas. ...
```

Las herramientas que usan el modelo (`search`, `ask_docs`, `ask_atlas`...) añaden esas
variables al error que devuelven, y `atlas check` también las nombra.

## Actualizar y desinstalar

- **Actualizar**: el mismo `pip install` con el wheelhouse o la versión nueva y
  `--upgrade`. El workspace sirve: atlas añade lo que le falte a `atlas.db` al abrirlo. Si
  la versión nueva cambia cómo se procesa algo, lo reprocesa solo (lo dice el CHANGELOG).
- **Desinstalar**: borrar la carpeta del entorno (`C:\atlas\venv`). Los datos están en el
  workspace de cada proyecto.

## Preparar la entrega

Desde el repositorio, en cualquier equipo con red:

```bash
make wheel                         # dist/atlaslm-<versión>-py3-none-any.whl
make wheelhouse-windows            # dist/wheelhouse-win_amd64-py312/ (WIN_PY=3.11 para otro Python)
make wheelhouse                    # dist/wheelhouse/ para este sistema y este Python
make check-install                 # instalación limpia desde dist/wheelhouse, sin red
```

`make check-install` (`tools/check_install.py`) hace los pasos de esta guía en una carpeta
nueva: entorno, instalación sin red, `atlas init`, la plantilla apuntando a las muestras y
a la API falsa, `check`, dos `run` (el segundo no procesa nada), `status`, `trace` y
`search`.

Para publicar en el repositorio interno, se sube el wheel (y, si se quiere, el `.tar.gz`)
con la herramienta del repositorio, por ejemplo
`twine upload --repository-url https://<repositorio>/ dist/atlaslm-<versión>*`.

`tree-sitter-ada` 0.1.0 declara `build` y `twine` como dependencias. No los usa: solo
ocupan sitio en el wheelhouse.

### Versiones fijadas

`pyproject.toml` da rangos compatibles; ADK está fijado a 2.11 porque su API cambia entre
versiones menores. `constraints.txt` fija la versión exacta de todo lo instalado al pasar
las pruebas en una instalación limpia. Para renovarlo tras cambiar dependencias:

1. Para cada Python soportado: un entorno nuevo, `pip install "dist/atlaslm-<versión>-py3-none-any.whl[all,dev]"`,
   las pruebas contra lo instalado y `pip freeze > freeze-3.12.txt`.
2. `python tools/constraints.py freeze-3.10.txt:3.10 freeze-3.11.txt:3.11 freeze-3.12.txt:3.12 freeze-3.13.txt:3.13 > constraints.txt`.

## Instalación para desarrollo

```bash
git clone <repositorio> && cd AtlasLM
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]" -c constraints.txt
python examples/make_samples.py          # datos de ejemplo
python -m pytest -q                      # sin modelo; las de Ollama se omiten si no está
```

El [README](../README.md) explica los tres niveles de modelo (servidor falso, Ollama y
gateway) y cómo probar con cada uno.
