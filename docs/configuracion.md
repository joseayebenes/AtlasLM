# Guía de configuración

Toda la ejecución de atlas se define en un fichero TOML, `atlas.toml` por defecto. La línea
de comandos solo dice qué acción lanzar y con qué fichero: `atlas run atlas.toml`.
`atlas init` escribe una plantilla comentada para empezar: la de un modelo local (Ollama) o,
con `atlas init --profile gateway`, la del gateway corporativo.

Ejemplos completos en el repositorio:
- `atlas.toml`: producción, con el gateway (la plantilla `--profile gateway` con el proyecto);
- `atlas.dev.toml`: Ollama y las muestras;
- `atlas.openrouter.toml`: modelos de OpenRouter y las muestras, para compararlos
  ([guía](openrouter.md));
- `atlas.test.toml`: la API falsa de las pruebas.

## Cómo se lee

- **Validación.** Una sección o una clave desconocida, o un valor del tipo equivocado, es un
  error que nombra la sección y la clave (`[pipeline] unknown key: 'workerz'`). Los valores
  que dependen de otros, como los pasos, los tipos del índice o las herramientas, se
  comprueban al usarlos.
- **Variables.** Cualquier texto acepta `${VARIABLE}` (vacío si no existe) y
  `${VARIABLE:-valor}`.
- **Secretos.** Las claves y contraseñas se leen siempre del entorno: atlas rechaza un
  `api_key` escrito tal cual para un servidor que no es local, y una contraseña en la URL
  del proxy o en el DSN de ODBC. Ver [seguridad](seguridad.md).
- **Rutas.** Todas las rutas relativas lo son a la carpeta del fichero, se lance atlas
  desde donde se lance: `workspace`, las de las fuentes (`paths`, `roots`, `path`),
  `tool_path`, `library`, `reference`, `hints`, `language_server_project`, `[agents] dirs`,
  `[agents.eval] sets`, `[tests.languages]` y las de los planes de `atlas develop`. Una
  absoluta se usa tal cual y `~` es la carpeta del usuario. El motor nombra cada fichero de
  las fuentes por su ruta relativa a esa carpeta (absoluta si está fuera de ella): es lo que
  sale en los informes y en las herramientas de ficheros.

### Qué se reprocesa al cambiar algo

El motor guarda una huella de cada unidad (un fichero, un documento, un módulo de DOORS)
con lo que le afecta de la configuración. Cambiar una clave solo reprocesa lo que depende
de ella:

| Cambio | Qué se reprocesa |
|---|---|
| una `[[sources.docs]]` (análisis, estructura, tamaños, idioma) | los documentos de esa fuente; del modelo, solo las secciones cuyo texto o prompt cambie |
| `[llm] model` | los documentos con `analysis = "llm"` |
| `[embeddings] model` | los vectores del índice, sin volver a analizar nada |
| `[index]` | el índice |
| `[search]` | nada: solo cambia lo que devuelven `search` y `ask` |
| `[sources.code]` | el análisis del código y su parte del grafo |
| `[sources.doors]` | la lectura de los módulos y su parte del grafo |
| `[graph]` | el grafo |
| una clave o una contraseña | nada |

## [project]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `name` | texto | `"atlas"` | nombre del proyecto |
| `workspace` | ruta | `"./workspace"` | carpeta de trabajo: `atlas.db`, salidas de documentos, grafo, informes. Relativa al fichero |
| `log_level` | texto | `"INFO"` | `DEBUG`, `INFO`, `WARNING` o `ERROR`; el registro va por stderr |

## [llm]

El modelo de chat: análisis de documentos, respuestas y agentes. Cualquier API compatible
con OpenAI.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `base_url` | URL | `"http://localhost:11434/v1"` | la API: el gateway, Ollama (`:11434/v1`) o LM Studio (`:1234/v1`) |
| `model` | texto | `"gemma4"` | el modelo, como lo nombra el servidor (`ollama list`) |
| `api_key` | texto | `"local"` | la clave; siempre `${VARIABLE}` salvo en un servidor local |
| `temperature` | número | `0.1` | temperatura del análisis y de las respuestas |
| `max_tokens` | entero | `0` | tope de la respuesta; 0 = el del servidor. Con modelos que razonan, el razonamiento cuenta dentro |
| `reasoning_effort` | texto | `""` | `"none"` apaga el razonamiento (gemma4 en Ollama razona por defecto y tarda minutos por sección); `atlas check` avisa si el modelo razona y está vacío |
| `extra_body` | tabla | `{}` | `[llm.extra_body]`: campos que van tal cual en cada petición de chat, para opciones propias del servidor. En OpenRouter, `provider = { order = ["deepinfra"], allow_fallbacks = false }` fija el proveedor; en vLLM, `chat_template_kwargs = { enable_thinking = false }` apaga el razonamiento de Qwen3 |
| `reasoning_field` | texto | `"reasoning"` | agentes: el campo del mensaje del asistente con llamadas a herramientas en el que vuelve al modelo su razonamiento (lo pide gemma 4). `"reasoning_content"` para algunos servidores; `""` no lo devuelve. `atlas probe` dice si el servidor lo aprovecha |
| `stream` | sí/no o `""` | `""` | cada petición de chat llega en trozos (SSE) y atlas los junta en la misma respuesta. `""`: con streaming hasta que el servidor demuestre que no puede (rechaza `stream`, contesta sin SSE o cierra un stream vacío); entonces se repite sin él y no se vuelve a intentar en ese proceso con ese servidor y modelo. `true` o `false` lo fuerzan (con `true`, un rechazo es un error). `atlas probe` dice «streaming: ok \| not supported» |
| `network` | tabla | — | `[llm.network]`: claves de `[network]` solo para el chat |

## [embeddings]

El modelo de embeddings del índice.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `base_url` | URL | la de `[llm]` | la API; sin ella, la misma URL y la misma clave que `[llm]` (la clave nunca se manda a otro servidor) |
| `model` | texto | `"embeddinggemma"` | el modelo; los prefijos de consulta y documento de embeddinggemma, nomic-embed y e5 se ponen solos |
| `api_key` | texto | la de `[llm]` si comparten URL | la clave |
| `batch` | entero | `32` | textos por petición. Con un límite de peticiones por minuto, cuanto mayor, mejor: el índice llena los lotes con textos de varios documentos |
| `network` | tabla | — | `[embeddings.network]`: claves de `[network]` solo para los embeddings |

## [network]

Red y límites, comunes al chat y a los embeddings (y retocables en `[llm.network]` o
`[embeddings.network]`). Si los dos van al mismo servidor con la misma clave, comparten
los límites.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `proxy` | URL | `HTTPS_PROXY` o `HTTP_PROXY` del entorno | proxy HTTP; con usuario, `"http://${PROXY_USER}:${PROXY_PASS}@proxy:8080"` |
| `no_proxy` | texto | `NO_PROXY` o `"localhost,127.0.0.1,::1"` | servidores que van directos, separados por comas; `"*"` = todos |
| `ca_cert` | ruta | `SSL_CERT_FILE` del entorno | PEM o carpeta con la CA corporativa, o `"system"` para el almacén del sistema |
| `client_cert`, `client_key` | ruta | `""` | certificado y clave de cliente (TLS mutuo); la contraseña de la clave, en `RAG_CLIENT_KEY_PASSWORD` |
| `insecure` | sí/no | `false` | `true` no verifica TLS: solo para diagnosticar |
| `timeout` | número | `600` | segundos para conectar y, en lo que no es una vuelta de chat (embeddings), entre dos lecturas |
| `idle_timeout` | número | `120` | vuelta de chat con streaming: segundos sin recibir un trozo, una vez empezada la respuesta, antes de cortarla (la conexión también) y preguntar otra vez, hasta dos veces. Los comentarios con que algunos servidores mantienen viva la conexión (OpenRouter) no cuentan. La espera hasta el primer trozo (la cola del servidor, la lectura del prompt) la acota `turn_timeout`; 0 = sin límite |
| `turn_timeout` | número | `600` | vuelta de chat: segundos en total desde que sale la petición (sin contar la espera del cupo) antes de cortarla y preguntar otra vez, hasta dos veces; es también el tiempo entre dos lecturas. 0 = sin límite |
| `requests_per_minute` | entero | `0` | peticiones por minuto; 0 = sin límite. Deja margen: 28 si el servidor admite 30 |
| `tokens_per_minute` | entero | `0` | tokens por minuto (aproximados: caracteres / 4); 0 = sin límite |
| `adaptive` | sí/no | `true` | baja el ritmo un 30 % con cada rechazo por cupo y lo recupera poco a poco |
| `max_concurrent` | entero | `0` | peticiones simultáneas al servidor; 0 = sin límite |
| `max_retries` | entero | `6` | reintentos tras un 5xx o un fallo de conexión |
| `max_backoff` | número | `60` | espera máxima entre reintentos sin `Retry-After` (s) |
| `rate_limit_max_wait` | número | `900` | con 409 o 429, espera hasta este total por petición (s) antes de dar error |
| `retry_statuses` | lista | `[409, 429, 500, 502, 503, 504]` | códigos que se reintentan |

## [pipeline]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `steps` | lista | `["docs", "doors", "code", "graph", "index"]` | qué pasos ejecuta `atlas run`; el orden lo dan sus dependencias |
| `workers` | entero | `1` | unidades a la vez en cada paso. En `docs`, además, peticiones al modelo a la vez entre todos los documentos |
| `step_workers` | tabla | `{}` | otro número para un paso: `{ docs = 8, code = 4, index = 2 }`. Solo `docs`, `code` e `index`; `doors` y `graph` van de uno en uno |
| `on_error` | texto | `"continue"` | `continue`: una unidad que falla se anota y se sigue; `stop`: se para. Los errores fatales (conexión, credenciales, cupo agotado) siempre paran |
| `max_attempts` | entero | `3` | intentos de una unidad antes de dejarla en cuarentena |
| `retry_quarantined` | sí/no | `false` | `true` vuelve a intentar las unidades en cuarentena |
| `prune_missing` | sí/no | `true` | borra del índice y del grafo lo que ya no está en las fuentes |
| `max_minutes` | entero | `0` | `atlas run` no empieza ninguna unidad más pasados estos minutos y sale con el código 3; la siguiente ejecución sigue. `0` = sin límite. `--max-minutes N` y `--until HH:MM` en la línea de comandos |
| `max_requests` | entero | `0` | lo mismo tras tantas peticiones a las API (chat, embeddings y otras) en la ejecución. `0` = sin límite; `--max-requests N` |

Con un gateway limitado por minuto, `workers` (o `step_workers.docs`) es lo que mantiene
llena la cuota: con respuestas de 10 s y 28 peticiones por minuto hacen falta unas 5 a la
vez. `atlas estimate` dice cuántas peticiones y cuánto tiempo queda antes de empezar, y
`atlas status` muestra, por paso, el tiempo, las peticiones y cuánto se esperó al
limitador. Programar la ingesta de noche: [instalación](instalacion.md#de-noche-con-un-límite).

## [plugins]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `modules` | lista | `[]` | módulos Python con plugins propios (`"mi_paquete.analizador_cpp"`); también se cargan los entry points `atlas.plugins` de los paquetes instalados |
| `disabled` | lista | `[]` | plugins apagados: `"code_analyzer:ada"`, `"extractor:pymupdf"` (los PDF vuelven a markitdown), `"tool:ask_docs"`... |

## [[sources.docs]]

Cada tabla es un grupo de documentos con su forma de procesarlos. Puede haber varias.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `name` | texto | obligatoria | nombre del grupo; da nombre a su carpeta de salida |
| `paths` | lista | obligatoria | ficheros o carpetas (se recorren enteras) |
| `patterns` | lista | las extensiones de los extractores activos | qué ficheros de las carpetas: `["*.pdf", "*.md"]` |
| `analysis` | texto | `"llm"` | `llm`: resumen, requisitos, glosario, entidades y pendientes por sección, con el modelo. `regex`: solo requisitos, sin modelo. `none`: solo la estructura |
| `req_regex` | texto | `(?i)\bReq\s*:\s*(\S+)` | con `regex`, marca cada requisito; el grupo 1 es su ID |
| `structure` | texto | `"auto"` | de dónde salen las secciones: `bookmarks` (marcadores del PDF), `headings` (títulos numerados), `size` (bloques), `auto` (lo primero que funcione) |
| `heading_regex` | texto | `""` | si los títulos no se reconocen: grupo 1 = número, grupo 2 = título |
| `max_chars` | entero | `12000` | tamaño máximo de una sección (las mayores se dividen) |
| `min_chars` | entero | `300` | las secciones más cortas se unen a la anterior |
| `lang` | texto | `""` | idioma de los resúmenes (`"Spanish"`); vacío = el del documento |
| `enabled` | sí/no | `true` | `false` la deja fuera sin borrarla |

`atlas sections atlas.toml` muestra qué secciones saldrían de cada documento, sin llamar al
modelo.

## [sources.doors]

Los requisitos oficiales. atlas los lee de una base de intercambio (ver
[atlas/doors](../atlas/doors/README.md)).

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `adapter` | texto | `"database"` | `exchange`: la base de intercambio (lo normal). `database`: otra base con consultas propias |
| `modules` | lista | `[]` | qué módulos; vacío = todos |
| `enabled` | sí/no | `true` | `false` la deja fuera |
| `exchange` | tabla | — | `[sources.doors.exchange]` |
| `database` | tabla | — | `[sources.doors.database]` |
| `convention` | tabla | — | `[sources.doors.convention]` |
| `attributes` | lista | — | reservada: todavía sin efecto (los atributos que pasan los elige el relleno de la base) |

### [sources.doors.exchange]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `path` | ruta | obligatoria | la base SQLite de intercambio |
| `kinds` | lista | `["requirement", "heading", "information"]` | qué objetos se leen |

### [sources.doors.database]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `driver` | texto | `"sqlite"` | `sqlite` u `odbc` (este necesita `pip install pyodbc`) |
| `path` | ruta | — | con `sqlite`, la base |
| `dsn` | texto | — | con `odbc`, la conexión; la contraseña, `PWD=${DB_PASS}` |
| `modules_query` | SQL | obligatoria | los módulos (primera columna) |
| `requirements_query` | SQL | obligatoria | los requisitos de `:module` |
| `links_query` | SQL | — | los enlaces que salen de `:module` |
| `snapshot_query` | SQL | — | un valor que cambia cuando cambia el módulo; sin ella se usa el contenido entero |

### [sources.doors.convention]

Cómo casan los IDs con los documentos y el código. Con `exchange` solo cuentan `id_pattern`,
`id_normalize` y las referencias en el texto; el resto son columnas de `database`.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `id_pattern` | texto | `""` | expresión de un ID válido; se avisan los requisitos que no casan (los títulos y la información, no) |
| `id_normalize` | texto | `"upper"` | `upper` o `none`; los espacios se quitan siempre. Se aplica igual a DOORS, código y documentos |
| `inline_reference_regex` | texto | `""` | IDs citados dentro del texto, que pasan a ser enlaces |
| `inline_reference_type` | texto | `"references"` | tipo de esos enlaces |
| `id_column`, `text_column`, `module_column`, `parent_column` | texto | `id`, `text`, `module`, `""` | columnas de `requirements_query`; `parent_column` da la jerarquía |
| `attribute_columns` | lista | `[]` | columnas que pasan como atributos |
| `link_columns` | tabla | `{ source = "source_id", target = "target_id", type = "link_type" }` | columnas de `links_query` |

## [sources.code]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `roots` | lista | `[]` | carpetas del código (se recorren enteras); también las únicas que pueden leer y editar las herramientas de ficheros |
| `languages` | lista | `["ada"]` | analizadores (plugins `code_analyzer`): `"ada"` y `"python"` |
| `exclude` | lista | `[]` | patrones que no se analizan ni se dejan leer: `["**/obj/**"]` |
| `req_tag` | texto | `"Req:"` | la etiqueta de trazabilidad en los comentarios |
| `req_regex` | texto | `""` | en vez de `req_tag`: grupo 1 = la lista de IDs |
| `test_patterns` | lista | `[]` | ficheros de prueba (`["**/tests/**"]`): sus subprogramas son pruebas, que verifican y cubren requisitos |
| `summaries` | texto | — | reservada: todavía sin efecto (resúmenes de subprogramas con el modelo) |
| `check_command` | lista | `[]` | el compilador para la herramienta `check_code`, como lista de argumentos: `["gcc", "-c", "-gnatc", "{include}", "{file}"]`. `{file}` es el fichero, `{include}` un `-I` por carpeta de código y `{build}` la carpeta `<workspace>/build`. Vacía: sin `check_code` |
| `check_commands` | tabla | `{}` | otro `check_command` para los ficheros de una extensión. GNAT ve más errores cuando genera código (una cadena asignada a otra de distinta longitud es un aviso, y un error con `-gnatwE`), pero no puede generarlo para una especificación sola: `check_command` sin `-gnatc` y `{ ads = ["gcc", "-c", "-gnatc", "{include}", "{file}"] }` |
| `test_command` | lista | `[]` | cómo construir una prueba para `run_test`, que después la ejecuta: `["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"]`. `{exe}` es el ejecutable que se lanza: en `run_command`, el que exista de `<build>/<nombre>` y `<build>/<nombre>.exe` (GNAT le añade `.exe` en Windows) |
| `run_command` | lista | `["{exe}"]` | cómo se ejecuta lo que construyó `test_command`. Un envoltorio puede arrancar antes lo que la prueba necesita: `["with_server", "{exe}"]` |
| `test_commands` | tabla | `{}` | pruebas que no se construyen, por extensión: el comando que las ejecuta tal cual, `{ py = ["python3", "{file}"] }` |
| `program_command` | lista | `[]` | lo que ejecuta la herramienta `run_program`, con las líneas que le da el agente en su entrada estándar: el programa, o un cliente que se las pasa, `["with_server", "session"]`. Vacía: sin `run_program` |
| `protected` | lista | `[]` | patrones de ficheros que los agentes leen pero no cambian: `["tests/**"]`. Los tests, para que el agente que escribe el código no pueda cambiar lo que lo juzga |
| `syntax_commands` | tabla | `{}` | la comprobación de sintaxis por extensión, sobre una copia del texto nuevo: un cambio que rompe la sintaxis de un fichero que no tenía errores no se hace, y el agente recibe los errores y las líneas de alrededor. GNAT: `{ adb = ["gcc", "-c", "-gnats", "{file}"], ads = [...] }`. Python no necesita comando (`ast`); una lista vacía apaga la comprobación de su extensión (`py = []`) |
| `hints` | lista | `[]` | ficheros de pistas para los mensajes del compilador, relativos a `atlas.toml`: `[[hint]]` con `match` (expresión regular; sus grupos con nombre van al texto como `{nombre}`) y `hint`. Van antes que las de serie (`atlas/code/hints.toml`, para GNAT). El informe de `atlas develop` dice qué errores salieron sin pista: se añade una tras cada tanda |
| `library` | lista | `[]` | carpetas de especificaciones (`.ads`) de bibliotecas que el proyecto usa, con comodines: el runtime de GNAT, `["~/.local/share/alire/toolchains/gnat_native_*/lib/gcc/*/*/adainclude"]` (`gcc -print-file-name=adainclude` dice cuál). Para la herramienta `library_spec` y para los errores que nombran algo que no existe; las especificaciones de las raíces del código van siempre |
| `reference` | lista | `[]` | manuales para la herramienta `search_reference` (y `atlas reference`), con comodines; una carpeta, todos los suyos: `.info` (los de GNAT, `~/.local/share/alire/toolchains/gnat_native_*/share/info/gnat_rm.info` y `gnat_ugn.info`), `.md`, `.txt` y `.html`. Van con las declaraciones de `library` en `<workspace>/reference.db`, no en el índice del proyecto |
| `language_server` | lista | `[]` | el servidor de lenguaje de las herramientas `find_definition`, `find_references` y `describe_symbol`, con comodines en el programa: el Ada Language Server de la extensión de VS Code, `["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"]` |
| `language_server_project` | texto | `""` | el proyecto GPR que lee el servidor, relativo a `atlas.toml`; vacío: atlas escribe uno con las carpetas de las raíces que tienen fuentes Ada |
| `check_on_edit` | sí/no | `true` | cada cambio de las herramientas de ficheros devuelve en `check` lo que dice `check_code` del fichero (si hay compilador para su extensión): una vuelta menos, y el agente no puede decir que compila sin verlo |
| `tool_path` | lista | `[]` | carpetas que van antes del `PATH` para esos comandos, con comodines: `["~/.local/share/alire/toolchains/gnat_native_*/bin"]`. Una relativa lo es a `atlas.toml`, como las raíces del código |
| `command_timeout` | entero | `120` | segundos como mucho para cada comando: compilar, construir o ejecutar una prueba, ejecutar el programa. Al acabarse, se mata el comando con todo lo que arrancó (su grupo de procesos): un envoltorio como `with_server` ya no deja vivo el servidor |
| `check_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para `check_code` y la comprobación de sintaxis |
| `test_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para cada comando de `run_test` (construir la prueba y ejecutarla) |
| `program_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para `run_program` |
| `enabled` | sí/no | `true` | `false` lo deja fuera |

## [tests]

Para `atlas tests`, que escribe un test por procedimiento de prueba y lenguaje a partir de sus
pasos numerados, sin modelo (ver [atlas/code](../atlas/code/README.md#tests-desde-los-procedimientos-de-prueba)).

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `languages` | tabla | `{}` | `[tests.languages.<lenguaje>]`, con `path`, dónde se escribe el test (`"tests/ada/test_{slug}.adb"`; `{id}`, `{slug}` y `{Slug}` salen del ID), y `template`, la plantilla del fichero entero, con `{steps}` en su propia línea |
| `steps` | lista | `[]` | `[[tests.steps]]`: `match`, una expresión regular para el texto entero de un paso, sin su punto final, y el código del paso en cada lenguaje, con `{n}`, `{text}` y los grupos con nombre de `match`; `{grupo:str}` lo escribe como cadena del lenguaje |

## [graph]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `exports` | lista | `["graphml", "json"]` | formatos del grafo completo en `<workspace>/graph/atlas.*` |
| `communities` | sí/no | `false` | `true`: cada elemento de código lleva su grupo (Louvain), para colorearlo en Gephi |

## [index]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `chunk_chars` | entero | `1200` | tamaño de los fragmentos (al menos 200) |
| `chunk_overlap` | entero | `200` | solape entre fragmentos (menos de la mitad de `chunk_chars`) |
| `kinds` | lista | todos | qué se indexa: `overview`, `section_summary`, `chunk`, `requirement`, `definition`, `open_issue`, `code_doc` (cada subprograma, tipo y variable del código, con su comentario y su código; necesita `[sources.code]`) |

## [search]

Lo que `atlas search`, `atlas ask` y las herramientas `search` y `ask_docs` toman del índice.
Cambiarlo no reprocesa nada.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `results` | entero | `8` | resultados de una búsqueda y pasajes de una respuesta (`--k` en la línea de comandos) |
| `per_document` | entero | `3` | como mucho, resultados de un mismo documento, módulo o unidad de código mientras otros tengan; `0` = sin límite. No se aplica si la búsqueda se limita a un documento |
| `context_chars` | entero | `14000` | caracteres de pasajes (con sus vecinos) que recibe el modelo al responder |
| `scope` | texto | `"auto"` | `auto`: si el modelo ve que la pregunta es amplia (un resumen, una función entera, todos los requisitos de algo), lee sus documentos enteros; `passages`: siempre los mejores pasajes; `whole`: siempre enteros |
| `scope_chars` | entero | `100000` | caracteres de documentos enteros que se leen en una petición; si son más, se leen por partes y una última petición junta las notas de cada una |

`scope_chars` depende del contexto del modelo: 100 000 caracteres son unos 25 000 o 30 000
tokens. Con el gateway, de unos 200 000 tokens de entrada, puede subir a 400 000 (menos
peticiones, cada una más lenta); con Ollama y `OLLAMA_CONTEXT_LENGTH=16384`, 30 000. Las
partes se leen de cuatro en cuatro; `[network] max_concurrent` lo limita. En un Mac de 24 GB
con `gemma4:12b`, cuatro a la vez llenan la memoria de intercambio: un documento de 20 000
caracteres en partes de 5000 tardó 11 minutos.

## [agents]

Los agentes son ficheros Markdown (ver [atlas/agents](../atlas/agents/README.md)).

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `model` | texto | `""` | modelo de los agentes; vacío = el de `[llm]`. Un agente puede tener el suyo |
| `dirs` | lista | `["agents"]` | carpetas con los `.md` del proyecto, relativas al fichero; un nombre repetido sustituye al de serie |
| `eval` | tabla | — | `[agents.eval]` |

### [agents.\<nombre\>]

Retoca un agente sin tocar su fichero. Solo estas claves:

| Clave | Tipo | Qué hace |
|---|---|---|
| `description` | texto | lo que el agente dice que hace |
| `tools` | lista | sus herramientas (plugins `tool`) |
| `model` | texto | su modelo |
| `max_steps` | entero | llamadas al modelo como mucho en una respuesta |
| `reasoning_effort` | texto | `"none"` apaga el razonamiento |
| `temperature` | número | su temperatura |
| `top_p` | número | su `top_p` (de 0 a 1); sin poner, el del servidor. La ficha de gemma 4 recomienda 0,95 |
| `top_k` | entero | su `top_k`; sin poner, el del servidor. No es de la API de OpenAI: va en `extra_body`, y un servidor estricto puede rechazarlo. La ficha de gemma 4 recomienda 64 |
| `keep_tool_results` | entero | cuántos resultados grandes de herramientas conserva enteros; los anteriores quedan en una nota con sus campos cortos, y una lectura de un fichero que se volvió a leer se quita. 0 (por defecto): todos |
| `stall_steps` | entero | llamadas seguidas sin que el código avance (un cambio tras el que el fichero compila o tiene menos errores, o un test que pasa) que paran una ejecución; `atlas develop` la vuelve a empezar, limpia. Solo en agentes que pueden cambiar el código. 0 (por defecto): sin límite |

### [agents.eval]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `sets` | lista | obligatoria para `atlas eval` | conjuntos de casos (YAML), relativos al fichero: preguntas a un agente, búsquedas en el índice y consultas del grafo |
| `runs` | entero | `1` | veces que se hace cada pregunta a un agente: los modelos no responden siempre igual |

## [mcp]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `enabled` | sí/no | `true` | `false` apaga `atlas serve` |
| `tools` | lista | `[]` | las herramientas del servidor; vacío = las del perfil (`profile`). `edit_file`, `write_file`, `replace_subprogram`, `add_subprogram`, `run_test`, `library_spec`, `search_reference`, las del servidor de lenguaje, `verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` solo se ofrecen si se piden |
| `profile` | texto | `editor` con `[sources.code]`; si no, `all` | qué herramientas de las fuentes configuradas ofrece el servidor cuando `tools` está vacío: `editor`, las que necesita el agente de un editor para cambiar código (buscar, requisitos, trazabilidad, contexto y lectura de código); `trace`, las de trazabilidad de DOORS y los documentos; `all`, todas. Ver [los perfiles](../atlas/mcp/README.md#qué-herramientas-ofrece) |
