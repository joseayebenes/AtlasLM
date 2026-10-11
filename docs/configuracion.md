# Guía de configuración

Toda la ejecución de atlas se define en un fichero TOML, `atlas.toml` por defecto. La línea
de comandos solo dice qué acción lanzar y con qué fichero: `atlas run atlas.toml`.
`atlas init` escribe una plantilla comentada para empezar.

Ejemplos completos en el repositorio:
- `atlas.toml`: producción, con el gateway;
- `atlas.dev.toml`: Ollama y las muestras;
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
- **Rutas.** Son relativas a la carpeta del fichero las de `[project] workspace`,
  `[agents] dirs` y `[agents.eval] sets`. Las de las fuentes (`paths`, `roots`, `path`)
  son relativas a la carpeta desde la que se lanza atlas, por eso el editor arranca
  `atlas serve` en la carpeta del proyecto.

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
| `reasoning_effort` | texto | `""` | `"none"` apaga el razonamiento (gemma4 en Ollama razona por defecto y tarda minutos por sección) |
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
| `timeout` | número | `600` | segundos por petición |
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

Con un gateway limitado por minuto, `workers` (o `step_workers.docs`) es lo que mantiene
llena la cuota: con respuestas de 10 s y 28 peticiones por minuto hacen falta unas 5 a la
vez. `atlas status` muestra, por paso, el tiempo, las peticiones y cuánto se esperó al
limitador.

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
| `attributes` | lista | — | reservada: sin efecto en 0.1.0 (los atributos que pasan los elige el relleno de la base) |

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
| `id_pattern` | texto | `""` | expresión de un ID válido; los que no casan se avisan |
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
| `languages` | lista | `["ada"]` | analizadores (plugins `code_analyzer`) |
| `exclude` | lista | `[]` | patrones que no se analizan ni se dejan leer: `["**/obj/**"]` |
| `req_tag` | texto | `"Req:"` | la etiqueta de trazabilidad en los comentarios |
| `req_regex` | texto | `""` | en vez de `req_tag`: grupo 1 = la lista de IDs |
| `test_patterns` | lista | `[]` | ficheros de prueba (`["**/tests/**"]`): sus subprogramas son pruebas, que verifican y cubren requisitos |
| `summaries` | texto | — | reservada: sin efecto en 0.1.0 (resúmenes de subprogramas con el modelo) |
| `enabled` | sí/no | `true` | `false` lo deja fuera |

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
| `kinds` | lista | todos | qué se indexa: `overview`, `section_summary`, `chunk`, `requirement`, `definition`, `open_issue`, `code_doc` (este, reservado: el código se busca con `search_code_docs`) |

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

### [agents.eval]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `sets` | lista | obligatoria para `atlas eval` | conjuntos de preguntas (YAML), relativos al fichero |
| `runs` | entero | `1` | veces que se hace cada pregunta: los modelos no responden siempre igual |

## [mcp]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `enabled` | sí/no | `true` | `false` apaga `atlas serve` |
| `tools` | lista | `[]` | las herramientas del servidor; vacío = las de las fuentes configuradas. `edit_file`, `write_file`, `verify_requirement` y `ask_atlas` solo se ofrecen si se piden |
