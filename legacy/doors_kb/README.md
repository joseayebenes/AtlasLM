# doors-kb — MCP y base de conocimiento para IBM DOORS Classic

Capa de acceso para agentes de IA sobre **IBM Engineering Requirements Management DOORS Classic**.

El proyecto tiene dos mitades complementarias:

1. **MCP directo a DOORS** (`doors_kb.servers.doors_server`): un servidor MCP local, de **solo lectura**,
   que consulta DOORS mediante la interfaz Automation (COM/OLE) y DXL. Sirve para comprobar informacion
   actual, trazabilidad o un requisito concreto. Requiere Windows y DOORS instalado.
2. **Copia local en SQLite** (`doors_kb.db` + `doors_kb.sync`): una replica de consulta sincronizada de
   forma incremental, que desacopla las busquedas de IA de DOORS. Es multiplataforma y no necesita DOORS.

**DOORS es siempre la fuente de verdad.** La base local es un derivado reconstruible: nunca se modifican
requisitos desde aqui.

## Estado por hito

| Hito | Contenido | Estado |
|---|---|---|
| H0 | Conexion Automation, apertura explicita de modulo, lectura de requisitos | Implementado |
| H1 | MCP avanzado: validacion, busqueda, tipos, enlaces, multi-modulo, limites, timeouts | Implementado |
| H2 | Persistencia SQLite: modelo local, atributos, hashes, `sync_runs` | Implementado |
| H3 | Sincronizacion DOORS -> SQLite con cursor y borrado seguro | Implementado (pendiente validacion con DOORS real) |
| H4 | Indice lexical FTS5 mantenido de forma incremental | Implementado |
| H5 | Embeddings incrementales contra API compatible con OpenAI | Implementado (pendiente validacion con el endpoint real) |
| H6 | Busqueda hibrida por fusion de rankings (RRF) | Implementado |
| H7 | Servidor MCP sobre la copia local | Implementado |
| H8 | Graph-RAG: expansion de contexto por trazabilidad | Planificado |

## Instalacion

```bash
python -m venv .venv
# Linux/macOS
.venv/bin/pip install -e ".[dev,embeddings]"
# Windows (incluye pywin32 para el acceso COM a DOORS)
.\.venv\Scripts\python -m pip install -e ".[dev,win,embeddings]"
```

El extra `embeddings` solo hace falta para la busqueda semantica e hibrida; el nucleo
(sincronizacion, indice textual y servidor directo) funciona sin el.

## Uso rapido sin DOORS

El proyecto incluye una fuente falsa que reproduce la semantica de DOORS (paginacion por cursor,
truncado por atributo, altas/bajas). Permite entender y probar el sistema completo sin instalar nada:

```bash
python examples/demo_sync_fake.py        # ciclo de sincronizacion
python examples/demo_busqueda_hibrida.py # los tres modos de busqueda
```

## Script autonomo de descarga (dos archivos)

Para descargar requisitos sin montar el paquete completo hay un script independiente en
`scripts/`, formado por **solo dos archivos** que se pueden copiar sueltos a la maquina con
DOORS. No importan `doors_kb` ni necesitan instalacion: solo `pywin32`.

| Archivo | Responsabilidad |
|---|---|
| [`scripts/modelo_sqlite.py`](scripts/modelo_sqlite.py) | Esquema SQLite, hash de contenido, altas/bajas y el historial de descargas |
| [`scripts/descargar_requisitos.py`](scripts/descargar_requisitos.py) | Sesion Automation, generacion de DXL, paginacion por cursor y volcado a la base |

```powershell
# un modulo concreto
python scripts\descargar_requisitos.py --modulo "/Proyecto/Requisitos/SRS"

# todos los modulos formales de un proyecto, recursivamente
python scripts\descargar_requisitos.py --carpeta "/Proyecto" --base doors.sqlite3

# que modulos encontraria, sin descargar nada
python scripts\descargar_requisitos.py --carpeta "/Proyecto" --solo-listar

# resumen de lo descargado
python scripts\modelo_sqlite.py doors.sqlite3
```

Por defecto descarga **todos** los atributos de objeto de cada modulo menos los de sistema
(`Object Heading` y `Object Text` se conservan siempre); con `--atributos "Object Text,Estado"`
se restringe a una lista, que se valida contra el esquema real del modulo antes de escribir
nada. Las descargas siguientes son incrementales: solo se reescribe lo que cambio de hash.

Comparte las reglas de seguridad del sincronizador del paquete: solo lectura, una pagina por
transaccion, y los ausentes se marcan como borrados **unicamente** si se llego al final del
modulo, de modo que una descarga interrumpida nunca borra requisitos vivos.

## Sincronizacion contra DOORS real

Antes de la primera sincronizacion conviene comprobar que DXL se comporta como el proyecto
espera; la tabla dice que primitivas funcionan en tu instalacion y a que afecta cada fallo:

```powershell
doors-selftest --module "/Proyecto/Requisitos/Requisitos del sistema"
```

```powershell
$env:DOORS_DXL_RUN_LIMIT_CYCLES = "0"
$env:DOORS_DXL_TIMEOUT_SECONDS  = "90"

doors-sync --module "/Proyecto/Requisitos/Requisitos del sistema" `
           --page-size 25 `
           --max-attribute-chars 20000 `
           --db .\doors_kb.sqlite3
```

El paso a paso completo, incluyendo el login manual de la sesion Automation y que hacer ante cada fallo
conocido, esta en [`docs/operacion_windows.md`](docs/operacion_windows.md).

## Variables de entorno

| Variable | Por defecto | Uso |
|---|---|---|
| `DOORS_MODULE_PATH` | *(vacio)* | Modulo predeterminado; las tools MCP pueden sobreescribirlo |
| `DOORS_PROG_ID` | `DOORS.Application` | ProgID Automation de DOORS |
| `DOORS_START_TIMEOUT_SECONDS` | `30` | Timeout de creacion/inicio de la sesion Automation |
| `DOORS_DXL_TIMEOUT_SECONDS` | `90` | Timeout externo de Python para una llamada DXL |
| `DOORS_HARD_MAX_RESPONSE_CHARS` | `500000` | Limite duro del tamano serializado de las respuestas MCP |
| `DOORS_DXL_RUN_LIMIT_CYCLES` | `0` | Watchdog interno de DXL (`pragma runLim`); `0` lo desactiva |
| `DOORS_DB_PATH` | `./doors_kb.sqlite3` | Ruta de la copia local SQLite |
| `DOORS_SYNC_ATTRIBUTES` | `Object Heading,Object Text` | Atributos que entran en la sincronizacion y en el hash |
| `DOORS_SYNC_PAGE_SIZE` | `25` | Objetos por pagina DXL |
| `DOORS_MAX_ATTRIBUTE_CHARS` | `20000` | Truncado por atributo |
| `DOORS_SOURCE_LAST_MODIFIED_ATTRIBUTE` | *(vacio)* | Atributo del proyecto que se mapea a `source_last_modified` |
| `EMBEDDINGS_BASE_URL` | *(vacio)* | URL base del servicio de embeddings (protocolo OpenAI) |
| `EMBEDDINGS_API_KEY` | *(vacio)* | Clave de ese servicio. **Nunca se expone en las respuestas MCP** |
| `EMBEDDINGS_MODEL` | *(vacio)* | Nombre del modelo de embeddings |
| `EMBEDDINGS_ATTRIBUTES` | *(vacio)* | Atributos que entran en el texto a embeder. Solo pueden ser atributos ya sincronizados |
| `EMBEDDINGS_BATCH_SIZE` | `32` | Textos por peticion |
| `EMBEDDINGS_TIMEOUT_SECONDS` | `60` | Timeout de cada peticion al proveedor |

## Busqueda local

Una vez sincronizado el modulo, la copia local se consulta sin tocar DOORS:

```bash
doors-embed  --module "/Proyecto/Requisitos/Modulo" --db ./doors_kb.sqlite3
doors-search --module "/Proyecto/Requisitos/Modulo" --db ./doors_kb.sqlite3 \
             --mode hybrid "cuanto tarda en cerrarse la conexion"
```

| Modo | Para que sirve | Necesita embeddings |
|---|---|---|
| `lexical` | Identificadores, codigos y terminos exactos | No |
| `semantic` | Preguntas conceptuales y parafrasis | Si |
| `hybrid` | Cuando no sabes cual encaja mejor | Si |

`doors-embed` solo genera lo que falta: una segunda ejecucion sin cambios no hace ninguna
llamada al proveedor. Si el indice se corrompe, `doors-sync --rebuild-index` lo rehace desde
la copia local, sin volver a consultar DOORS.

## Configuracion MCP en VS Code

```json
{
  "servers": {
    "doors": {
      "type": "stdio",
      "command": "C:\\proyecto\\.venv\\Scripts\\python.exe",
      "args": ["-m", "doors_kb.servers.doors_server"],
      "env": {
        "DOORS_MODULE_PATH": "/Proyecto/Requisitos/Modulo",
        "DOORS_START_TIMEOUT_SECONDS": "30",
        "DOORS_DXL_TIMEOUT_SECONDS": "90",
        "DOORS_HARD_MAX_RESPONSE_CHARS": "500000"
      }
    },
    "doors-kb": {
      "type": "stdio",
      "command": "C:\\proyecto\\.venv\\Scripts\\python.exe",
      "args": ["-m", "doors_kb.servers.kb_server"],
      "env": {
        "DOORS_MODULE_PATH": "/Proyecto/Requisitos/Modulo",
        "DOORS_DB_PATH": "C:\\proyecto\\doors_kb.sqlite3",
        "EMBEDDINGS_BASE_URL": "https://mi-proveedor/v1",
        "EMBEDDINGS_MODEL": "nombre-del-modelo",
        "EMBEDDINGS_API_KEY": "..."
      }
    }
  }
}
```

Los dos servidores son complementarios: `doors-kb` es rapido y no necesita DOORS abierto,
pero responde con la ultima sincronizacion, asi que cada respuesta suya indica su frescura;
`doors` da el estado vivo. Lo habitual es buscar en el primero y verificar en el segundo.

## Documentacion

| Documento | Para que sirve |
|---|---|
| [`docs/especificacion.md`](docs/especificacion.md) | Especificacion de requisitos consolidada (fuente de RF/RNF) |
| [`docs/arquitectura.md`](docs/arquitectura.md) | Capas, responsabilidades y flujos internos |
| [`docs/adr.md`](docs/adr.md) | Decisiones de arquitectura y su motivacion |
| [`docs/trazabilidad.md`](docs/trazabilidad.md) | Que requisito cubre cada modulo y que test lo verifica |
| [`docs/guia_desarrollo.md`](docs/guia_desarrollo.md) | Montar el entorno, ejecutar tests, extender el sistema |
| [`docs/operacion_windows.md`](docs/operacion_windows.md) | Puesta en marcha con DOORS real y fallos conocidos |

## Desarrollo

```bash
.venv/bin/pytest -q          # toda la suite corre sin DOORS ni Windows
.venv/bin/ruff check src tests examples
```
