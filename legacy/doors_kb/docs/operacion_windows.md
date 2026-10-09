# Puesta en marcha con DOORS real

Esta guia cubre lo que no se puede probar fuera de Windows: la sesion Automation, la
ejecucion del DXL generado y el rendimiento en un modulo grande. Corresponde al cierre del
hito H3.

## Requisitos previos

- Windows con **IBM DOORS Classic instalado** en la misma maquina.
- Python 3.11 o superior, ejecutandose con **el mismo usuario de Windows** que DOORS y con
  un nivel de privilegios compatible (si DOORS va como administrador, Python tambien).
- **No sirve** WSL, un contenedor de desarrollo ni Remote SSH: el cliente COM necesita la
  sesion de escritorio real.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,win]"
```

## 1. Comprobar que DXL se comporta como el proyecto espera

**Hazlo antes que nada.** La capa DXL no se puede probar fuera de Windows, asi que este
comando verifica una por una las primitivas del lenguaje que usa el proyecto contra tu
instalacion:

```powershell
.\.venv\Scripts\doors-selftest --module "/Proyecto/Requisitos/Modulo"
```

Abre antes la ventana **DXL output** en DOORS (Tools -> Edit DXL): ahi aparece el mensaje del
interprete de cada prueba que falle.

La salida es una tabla de primitivas con OK o FALLA, y para cada fallo dice a que capacidad
del proyecto afecta. Las dos primeras pruebas son deliberadamente redundantes -la misma
conversion en los dos ordenes posibles- porque confirmar cual de las dos formas acepta tu
DOORS es justo lo que evita el error `incorrect arguments for (=)`.

## 2. Comprobar la instalacion sin tocar DOORS

Antes de involucrar a DOORS, conviene descartar problemas del propio proyecto:

```powershell
.\.venv\Scripts\pytest -q
.\.venv\Scripts\python examples\demo_sync_fake.py
```

Si esto falla, el problema no esta en DOORS.

## 3. Abrir la sesion Automation

```powershell
$env:DOORS_MODULE_PATH = "/Proyecto/Requisitos/Requisitos del sistema"
.\.venv\Scripts\doors-sync --source doors --module $env:DOORS_MODULE_PATH
```

Python abre **una ventana nueva** de DOORS. Hay que autenticarse **en esa ventana**, no en
una que ya tuvieras abierta: la sesion de Automation es independiente de la sesion manual
(ADR-002). Si tarda mas de 30 segundos, sube `DOORS_START_TIMEOUT_SECONDS`.

## 4. Primera sincronizacion

Configuracion recomendada para la primera pasada de un modulo grande:

```powershell
$env:DOORS_DXL_RUN_LIMIT_CYCLES = "0"    # desactiva el watchdog interno de DXL
$env:DOORS_DXL_TIMEOUT_SECONDS  = "90"   # el control lo lleva el timeout de Python

.\.venv\Scripts\doors-sync `
  --module "/Proyecto/Requisitos/Requisitos del sistema" `
  --page-size 25 `
  --max-attribute-chars 20000 `
  --db .\doors_kb.sqlite3 `
  --verbose
```

`--verbose` imprime una linea por pagina con su cursor: es como se comprueba CA-006 (la
duracion por pagina debe mantenerse estable segun avanza el modulo, no crecer).

## 5. Verificar los criterios de aceptacion

| Criterio | Como comprobarlo |
|---|---|
| CA-001 | La sincronizacion completa termina sin ventanas *DXL Execution Timeout* |
| CA-002 | Lanzarla dos veces seguidas: la segunda da `inserted=0, updated=0, unchanged=N` |
| CA-003 | Editar el texto de un objeto en DOORS y volver a sincronizar: `updated=1` |
| CA-004 | Crear un objeto (`inserted=1`); borrar otro y sincronizar entero (`deleted=1`) |
| CA-005 | Cortar la sincronizacion con Ctrl+C a mitad: `sync_runs.status='failed'` y `deleted=0` |
| CA-006 | Con `--verbose`, la duracion por pagina se mantiene estable |
| CA-007 | Lanzarla con `--attributes "Object Text,No Existe"`: falla antes de escribir nada |

Consulta del historial:

```powershell
sqlite3 .\doors_kb.sqlite3 "SELECT status, requirements_seen, inserted, updated, unchanged, deleted, completed_module, error FROM sync_runs ORDER BY id DESC LIMIT 5;"
```

## 6. Busqueda local: indice y embeddings

El indice textual se mantiene solo: cada `doors-sync` lo deja al dia. Si hiciera falta
rehacerlo (por ejemplo tras cambiar de tokenizador), no hay que volver a consultar DOORS:

```powershell
.\.venv\Scripts\doors-sync --rebuild-index --module "/Proyecto/Requisitos/Modulo" --db .\doors_kb.sqlite3
```

Los embeddings si son un paso aparte, porque tienen coste. Primero conviene comprobar el
mecanismo sin gastar llamadas:

```powershell
.\.venv\Scripts\doors-embed --module "..." --db .\doors_kb.sqlite3 --provider fake
```

### Validar el endpoint real de embeddings

**Esto no se ha podido probar durante el desarrollo**: el entorno de desarrollo no tiene
salida hacia endpoints externos, asi que el cliente HTTP se ejercito con un transporte
inyectado. Es el equivalente, para la capa de embeddings, de lo que la capa COM tiene con
DOORS. La primera ejecucion contra el proveedor real es una validacion pendiente.

```powershell
$env:EMBEDDINGS_BASE_URL = "https://mi-proveedor/v1"
$env:EMBEDDINGS_API_KEY  = "..."
$env:EMBEDDINGS_MODEL    = "nombre-del-modelo"

.\.venv\Scripts\doors-embed --module "/Proyecto/Requisitos/Modulo" --db .\doors_kb.sqlite3
```

Que comprobar, en este orden:

| Comprobacion | Que confirma |
|---|---|
| La primera ejecucion devuelve `generated` igual al numero de requisitos | La llamada funciona y la respuesta se asocia bien |
| La **segunda** ejecucion devuelve `generated: 0` y `skipped: N` | RF-074: no se paga dos veces por lo mismo |
| Editar un requisito en DOORS, sincronizar y volver a embeder da `generated: 1` | La deteccion incremental llega hasta el indice |
| `doors-search --mode semantic` devuelve resultados sensatos | El modelo elegido sirve para este corpus |

Si el proveedor limita el tamano de peticion, baja `EMBEDDINGS_BATCH_SIZE`. Si corta por
tiempo, sube `EMBEDDINGS_TIMEOUT_SECONDS`.

### Confirmar el predicado de visibilidad (RF-022)

El recorrido por *display set* genera `isVisible(o)` en el DXL. **Conviene confirmar que ese
es el nombre en la version de DOORS instalada** antes de fiarse del filtrado: si el
interprete DXL da un error de funcion desconocida al llamar a `list_requirements` con
`respect_display_set`, el predicado hay que ajustarlo en
`src/doors_kb/sources/doors/dxl.py::_filtros_de_objeto`. El resto de las consultas no se ve
afectado, porque el filtro solo se genera cuando se pide.

## 7. Usar los servidores MCP desde VS Code

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
    }
  }
}
```

Orden de uso recomendado para el agente: `doors_configuration` -> `start_doors_session`
(el usuario se autentica) -> `doors_status` -> `list_object_attributes` -> consultas.

El servidor local (`doors-kb`) se configura aparte y no necesita DOORS abierto; la
configuracion completa de ambos esta en el README. La pauta util es: **buscar en la copia
local y verificar en DOORS** cuando el dato tenga que estar al dia. Cada respuesta del
servidor local incluye `freshness` precisamente para poder tomar esa decision.

---

# Fallos conocidos y que hacer

Todos ocurrieron durante el desarrollo del proyecto (seccion 10 de la especificacion).

### No encuentra la sesion de DOORS que tengo abierta

**No es un fallo.** El proyecto nunca se conecta a una ventana existente: crea la suya con
`Dispatch`, porque una ventana abierta a mano no aparece de forma fiable en la Running
Object Table de Windows (ADR-002). Autenticate en la ventana que abre Python.

### `NO_MODULE` al consultar

La sesion de Automation no comparte el modulo que tengas abierto en tu ventana. Comprueba
que la ruta es el `fullName` completo del modulo, con las barras y mayusculas exactas. En
DOORS: clic derecho sobre el modulo -> Propiedades -> ruta completa.

### `DXL Execution Timeout`

El watchdog interno de DXL corto el script. Opciones, por orden:

1. Asegurate de que `DOORS_DXL_RUN_LIMIT_CYCLES=0` (desactiva el watchdog y deja el control
   al timeout de Python, que si distingue un script colgado de uno que va lento).
2. Baja `--page-size` (por ejemplo a 10).
3. Baja `--max-attribute-chars` si el modulo tiene objetos con textos enormes.

Si el tiempo por pagina **crece** segun avanza el modulo, algo ha vuelto a paginar por
offset: revisa `dxl.py::_posicionar_cursor` (ADR-006).

### "El worker COM esta bloqueado desde una llamada anterior"

Una llamada supero su timeout y el script DXL puede seguir corriendo dentro de DOORS.
Reutilizar esa sesion podria mezclar respuestas, asi que el proceso queda inutilizable a
proposito (RNF-005): **reinicia el proceso** (o el servidor MCP). Si se repite, sube
`DOORS_DXL_TIMEOUT_SECONDS` o baja el tamano de pagina.

### Un atributo devuelve siempre vacio

Casi siempre es un nombre mal escrito. Los nombres de DOORS distinguen mayusculas y llevan
espacios: `"Object Text"`, no `"Object text"`. Usa `list_object_attributes` o
`validate_attributes`, que sugiere el nombre correcto. El proyecto valida contra el esquema
del modulo justo para que esto sea un error y no una cadena vacia silenciosa.

### DOORS responde "ocupado" o rechaza la llamada

Ocurre cuando estas interactuando con la ventana de DOORS mientras corre la sincronizacion.
El worker lo reintenta con espera creciente (RNF-006). Si persiste, cierra los dialogos
abiertos en DOORS y deja la ventana quieta.

### El servidor MCP no arranca o el cliente lo da por caido

Comprueba que nada escribe en **stdout**: con transporte stdio, stdout es del protocolo y
cualquier `print` corrompe la sesion. Los logs del proyecto van a stderr (RNF-009).

### Respuestas truncadas en el agente

Es intencionado (RF-044). El campo `truncated` de la respuesta dice cuantos resultados se
omitieron. Reduce `limit`, reduce `max_attribute_chars`, o continua el recorrido con el
`next_cursor` que devuelve la propia respuesta.

### "DOORS ha devuelto el resultado de una llamada anterior"

El script DXL fallo a mitad y no llego a ejecutar `oleSetResult`, asi que la propiedad
`result` de DOORS conservaba lo que devolvio la llamada anterior. El proyecto lo detecta
porque cada llamada lleva un testigo propio, pero **el motivo del fallo no esta en ese
mensaje**: esta en DOORS.

En la ventana de DOORS, abre **Tools -> Edit DXL** (o la ventana *DXL output* si ya esta
abierta): ahi aparece el error del interprete con su numero de linea. Ese texto es lo unico
que explica el fallo; pasalo tal cual al arreglarlo.

### "incorrect arguments for (=)"

Casi siempre es una conversion de numero a texto mal escrita. En esta instalacion la forma
valida es **el numero delante**: `string n = valor ""`, no `string n = "" valor`.

Y ojo con concatenar directamente el resultado de una llamada: `length(s) ""` se interpreta
como `length(s "")` y devuelve un entero, de modo que la asignacion a una cadena falla. Hay
que guardar el resultado en una variable primero. Todas las conversiones del proyecto pasan
por `aTexto()` (ADR-016).

### "wrong attribute type '...' for Enumeration"

Ocurrio en la primera lectura real de atributos: `at.size` solo existe en los tipos de
enumeracion y consultarlo en un `Integer`, `String`, `Date` o `Text` aborta el script. Ya
esta corregido con una guarda `at.type == attrEnumeration`. Si vuelve a aparecer con otra
propiedad, el patron es el mismo: una propiedad de DXL que solo aplica a ciertos tipos y se
esta leyendo sin comprobar el tipo antes.

### La busqueda semantica falla con "necesita numpy"

La busqueda vectorial no viene en el nucleo. Instala el extra:
`pip install -e ".[embeddings]"`. La busqueda textual sigue funcionando sin el.

### La busqueda semantica falla con "los embeddings guardados tienen N dimensiones"

El indice se genero con otro modelo. Es lo que ocurre al cambiar `EMBEDDINGS_MODEL` sin
reindexar. Vuelve a ejecutar `doors-embed`: los embeddings del modelo nuevo conviven con los
del anterior, asi que no te quedas sin busqueda mientras se regeneran.

### Un atributo del perfil de embeddings no aparece en el texto

El texto de embedding solo puede usar atributos que la sincronizacion haya traido a la copia
local. Anadelo tambien a `DOORS_SYNC_ATTRIBUTES` y vuelve a sincronizar. `doors-embed` avisa
por log cuando detecta este caso, en lugar de aplicarlo en silencio.

### El agente responde con informacion desactualizada

Mira el campo `freshness` de la respuesta: dice cuando se sincronizo el modulo por ultima
vez. Si `last_full_sync_at` esta vacio, la copia nunca completo un recorrido entero. La
solucion es sincronizar; el aviso existe para que el agente pueda decir que no lo sabe en
lugar de contestar con datos viejos.
