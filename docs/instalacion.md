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
C:\atlas\venv\Scripts\python -m pip install "atlaslm[all]==0.1.0" -c constraints.txt --index-url https://<repositorio>/simple
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
| `agents` | agentes ADK: `atlas agent`, `verify`, `impact`, `coverage`, `eval` (google-adk 2.11) | sin agentes |

Por ejemplo, `"atlaslm[docs,code,mcp]"` para procesar y servir sin agentes.

## 2. Crear el proyecto

```powershell
mkdir C:\proyectos\tms
cd C:\proyectos\tms
C:\atlas\venv\Scripts\atlas init
```

`atlas init` escribe un `atlas.toml` comentado con todas las secciones. Hay que cambiar:

1. `[llm] base_url` y `model`: el gateway o el modelo local.
2. Las fuentes que se tengan: `[[sources.docs]] paths`, `[sources.code] roots` y, con la
   base de requisitos, `[sources.doors] enabled = true` y su `path`.

La [guía de configuración](configuracion.md) explica cada clave.

## 3. Secretos y red

La clave del gateway no se escribe en `atlas.toml`: se lee del entorno. La plantilla ya
dice `api_key = "${ATLAS_API_KEY:-local}"`.

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
atlas check atlas.toml      # una petición real al chat y otra a los embeddings
atlas run atlas.toml        # procesamiento incremental: la primera vez, todo
atlas status atlas.toml     # qué hay, qué falló y el perfil de tiempos de la última ejecución
```

Si `check` falla, dice por qué: certificado, proxy, credenciales, URL o modelo. Con
`C:\atlas\venv\Scripts` en el `PATH` basta con `atlas`; si no, la ruta completa.

`atlas run` se puede interrumpir (Ctrl+C, un corte de red, un apagado): al relanzarlo sigue
donde se quedó. Una segunda ejecución sin cambios no hace ninguna petición al modelo.

## 5. Conectar el editor

`atlas serve atlas.toml` es un servidor MCP por stdio. El editor lo arranca en la carpeta
del proyecto.

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

El servidor hereda el entorno del editor, `ATLAS_API_KEY` incluida. Si el editor se abrió
antes del `setx`, hay que reiniciarlo; o pasar la variable con `environment` en OpenCode o
con `env` o `envFile` en VS Code.

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
