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
| `[[sources.docs]] authority` | el grafo |
| `[llm] model` | los documentos con `analysis = "llm"` de las fuentes sin `analysis_model` propio |
| `analysis_model` de una `[[sources.docs]]` | los documentos de esa fuente (sus secciones, con ese modelo) |
| `[embeddings] model`, `family` o los prefijos | los vectores del índice, sin volver a analizar nada |
| `[index]` | el índice |
| `[index] fts_tokenizer` | solo la tabla de texto, al abrir el índice, sin embeber |
| `[search]` | nada: solo cambia lo que devuelven `search` y `ask` |
| `[sources.code]` | el análisis del código y su parte del grafo |
| `[sources.doors]` | la lectura de los módulos y su parte del grafo |
| `[sources.doors] attributes` | el índice de los módulos de DOORS, sin volver a leerlos |
| `[graph]` `exports` o `communities` | el grafo (`matrix_formats` y `csv_delimiter`, nada) |
| `model` de una fuente | su análisis (si cambia a `regex`) y su parte del índice, que pasa a tener vectores o a no tenerlos |
| una clave o una contraseña | nada |

## [project]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `name` | texto | `"atlas"` | nombre del proyecto |
| `workspace` | ruta | `"./workspace"` | carpeta de trabajo: `atlas.db`, salidas de documentos, grafo, informes. Relativa al fichero |
| `log_level` | texto | `"INFO"` | `DEBUG`, `INFO`, `WARNING` o `ERROR`: lo que sale por stderr, también los avisos del limitador de peticiones |
| `shared_cache` | ruta | `""` | carpeta (normalmente de red, `//servidor/atlas/tms-cache`) con cachés exportadas por `atlas cache export` (`*.sqlite`): caché secundaria de solo lectura. Lo que el workspace no tiene se busca ahí (análisis de secciones y vectores) y se copia al workspace; atlas nunca escribe en ella. Si no se puede leer, se sigue sin ella con un aviso. Relativa al fichero; cambiarla no reprocesa nada. Ver [instalación](instalacion.md#6-varios-puestos-cachés-compartidas) |
| `log_dir` | ruta | `"logs"` | carpeta del registro de cada ejecución, relativa al workspace: un fichero por día (`atlas-AAAA-MM-DD.log`) con todo lo de atlas a nivel `DEBUG` y los avisos de las librerías, con fecha, nivel, proceso y qué comando y paso estaban en marcha. `atlas run` y `atlas serve` escriben a la vez en el mismo fichero. `""` lo apaga. `atlas status` dice dónde está |
| `log_keep_days` | entero | `14` | días que se guardan los ficheros del registro: los más viejos se borran al arrancar atlas; `0` = se guardan todos |

## [llm]

El modelo de chat: análisis de documentos, respuestas y agentes. Cualquier API compatible
con OpenAI. Cada tarea puede usar otro modelo o razonamiento por la misma conexión: el
análisis de los documentos de una fuente (`model` y `reasoning_effort` de
`[[sources.docs]]`), la reescritura de las preguntas y las respuestas (`[search.rewrite]` y
`[search.answer]`) y cada agente (`[agents.<nombre>]`).

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
| `context_tokens` | entero | `0` | el contexto del modelo en tokens (entrada y salida); 0 = desconocido. `atlas check` lo pide al servidor si lo publica (OpenRouter y vLLM en `/models`, Ollama el `num_ctx` del modelo) y lo propone o avisa si no coincide. Con él, `[search] scope_chars` y `context_chars` salen de él si no se dan, y cada vuelta de un agente que no cabe en `context_tokens − max_tokens` deja fuera resultados viejos de herramientas, con un aviso. Tiene que ser mayor que `max_tokens` |
| `concurrency` | entero | `0` | peticiones al modelo en vuelo a la vez desde un mismo cliente: en `docs`, entre las secciones de todos los documentos que se procesan juntos. 0 = las `workers` del paso, como antes. Sirve para llenar el cupo sin abrir más documentos a la vez: `atlas status` dice cuántas lo llenarían |
| `network` | tabla | — | `[llm.network]`: claves de `[network]` solo para el chat |

## [embeddings]

El modelo de embeddings del índice.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `base_url` | URL | la de `[llm]` | la API; sin ella, la misma URL y la misma clave que `[llm]` (la clave nunca se manda a otro servidor) |
| `model` | texto | `"embeddinggemma"` | el modelo; los prefijos de consulta y documento de su familia se ponen solos si el nombre la dice (`embeddinggemma`, `nomic-embed`, `e5`) |
| `api_key` | texto | la de `[llm]` si comparten URL | la clave |
| `family` | texto | `""` | la familia del modelo, para sus prefijos: `embeddinggemma`, `nomic`, `e5` o `none` (ninguno). Vacío: la que dice su nombre. Hace falta si el servidor publica el modelo con otro nombre |
| `query_prefix` | texto | `""` | prefijo literal de las consultas, en lugar del de la familia |
| `document_prefix` | texto | `""` | prefijo literal de los textos indexados, en lugar del de la familia; `{title}` es el título del documento |
| `batch` | entero | `32` | textos por petición. Con un límite de peticiones por minuto, cuanto mayor, mejor: el índice llena los lotes con textos de varios documentos |
| `network` | tabla | — | `[embeddings.network]`: claves de `[network]` solo para los embeddings |

Algunos modelos de embeddings esperan un prefijo que distingue las consultas de los textos
indexados. Con `embeddinggemma`, `task: search result | query: ` y `title: {title} | text: `,
con el título real del documento (un módulo de DOORS, `SRS_TMS (DOORS)`; una unidad de código,
`tracks (code)`). Un modelo cuyo nombre no dice su familia (`corp/gemma-embed-300m`) va sin
prefijos, y el registro lo avisa; `atlas check` imprime los prefijos efectivos o ese aviso:

```
  embeddings prefixes: query='task: search result | query: ' document='title: {title} | text: '
```

Los prefijos entran en la huella del índice: cambiar la familia o un prefijo vuelve a embeber
los textos.

## [network]

Red y límites, comunes al chat y a los embeddings (y retocables en `[llm.network]` o
`[embeddings.network]`). Si los dos van al mismo servidor con la misma clave, comparten
los límites.

El cupo de un gateway es de la clave, no del proceso. Con `shared_limit` (por defecto), los
procesos del puesto se reparten `requests_per_minute`: un rechazo que ve uno baja el ritmo de
todos, y una pregunta del editor espera su turno en vez de llevarse los 429. El fichero de cada
servidor se nombra por su máquina y su puerto, sin nada de la clave; dos claves distintas contra
el mismo servidor comparten la ventana, que es lo prudente. Si el fichero no se puede bloquear,
cada proceso vuelve a llevar su ritmo solo, y lo avisa; si está roto, se reescribe.

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
| `requests_per_minute` | entero | `0` | peticiones por minuto; 0 = sin límite. Deja margen: 28 si el servidor admite 30. `atlas develop` compara con él los agentes que lanza a la vez (`parallel` × `attempts` del plan, una vuelta cada unos 30 s) y avisa si van a llenarlo |
| `tokens_per_minute` | entero | `0` | tokens por minuto (aproximados: caracteres / 4); 0 = sin límite |
| `adaptive` | sí/no | `true` | baja el ritmo un 30 % con cada rechazo por cupo y lo recupera poco a poco |
| `max_concurrent` | entero | `0` | peticiones simultáneas al servidor desde este proceso; 0 = sin límite |
| `shared_limit` | sí/no | `true` | con `requests_per_minute` o `tokens_per_minute`, el límite es de todos los procesos del puesto que van al mismo servidor (`atlas serve`, un `atlas run` largo, `atlas develop`): comparten la ventana del último minuto, el ritmo adaptado y las pausas en `~/.atlas/limits/<servidor>-<puerto>.json` (en Windows, `%LOCALAPPDATA%\atlas\limits`; otra carpeta con la variable `ATLAS_LIMITS_DIR`). `false`: cada proceso cree tener el cupo entero |
| `max_retries` | entero | `6` | reintentos tras un 5xx o un fallo de conexión |
| `max_backoff` | número | `60` | espera máxima entre reintentos sin `Retry-After` (s) |
| `rate_limit_max_wait` | número | `900` | con 409 o 429, espera hasta este total por petición (s) antes de dar error |
| `retry_statuses` | lista | `[409, 429, 500, 502, 503, 504]` | códigos que se reintentan |

## [pipeline]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `steps` | lista | `["docs", "doors", "code", "graph", "index"]` | qué pasos ejecuta `atlas run`; el orden lo dan sus dependencias |
| `workers` | entero | `1` | unidades a la vez en cada paso. En `docs`, además, peticiones al modelo a la vez entre todos los documentos, salvo que `[llm] concurrency` diga otra cosa |
| `step_workers` | tabla | `{}` | otro número para un paso: `{ docs = 8, code = 4, index = 2 }`. Solo `docs`, `code` e `index`; `doors` y `graph` van de uno en uno |
| `on_error` | texto | `"continue"` | `continue`: una unidad que falla se anota y se sigue; `stop`: se para. Los errores fatales (conexión, credenciales, cupo agotado) siempre paran |
| `max_attempts` | entero | `3` | intentos de una unidad antes de dejarla en cuarentena |
| `retry_quarantined` | sí/no | `false` | `true` vuelve a intentar las unidades en cuarentena |
| `prune_missing` | sí/no | `true` | borra del índice y del grafo lo que ya no está en las fuentes |
| `max_minutes` | entero | `0` | `atlas run` no empieza ninguna unidad más pasados estos minutos y sale con el código 3; la siguiente ejecución sigue. `0` = sin límite. `--max-minutes N` y `--until HH:MM` en la línea de comandos |
| `max_requests` | entero | `0` | lo mismo tras tantas peticiones a las API (chat, embeddings y otras) en la ejecución. `0` = sin límite; `--max-requests N` |

Con un gateway limitado por minuto, lo que mantiene lleno el cupo son las peticiones en
vuelo: con respuestas de 21 s y 28 peticiones por minuto hacen falta 28 × 21 / 60 ≈ 10 a la
vez (ley de Little). `atlas status` lo calcula, por paso, con lo medido en la última
ejecución:

```
  docs.process   1860.00   0.40     42     42  spec.pdf (412.10 s); 540 chat, 3 rejected
                 chat: 540 in 31 min = 17/min of 28; latency 21 s; 6.1 in flight; 10 would fill the quota
```

Si las peticiones en vuelo se quedan por debajo de las que llenarían el cupo, sube `[llm]
concurrency` (o `workers`); si hay rechazos, bájalo. `concurrency` separa las peticiones al
modelo de los documentos que se abren a la vez (`workers`), que cuestan memoria.
`atlas estimate` dice cuántas peticiones y cuánto tiempo queda antes de empezar. Programar la
ingesta de noche: [instalación](instalacion.md#de-noche-con-un-límite).

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
| `model` | texto | `"full"` | `"none"`: ningún texto de estos documentos sale del puesto (ver [Fuentes locales](#fuentes-locales-model--none)). `analysis` pasa a `"regex"` si no se dice, y `"llm"` es un error |
| `ocr_command` | lista | `[]` | orden que pasa por OCR los PDF con páginas sin texto: `["ocrmypdf", "--skip-text", "{file}", "{out}"]` |
| `doc_id_regex` | texto | `""` | código del documento (grupo 1): `'Doc\.\s+(\S+)'` |
| `version_regex` | texto | `""` | su versión (grupo 1): `'\bIssue\s+(\w+)'` |
| `date_regex` | texto | `""` | su fecha (grupo 1): `'\b(\d{4}-\d{2}-\d{2})\b'`; sin ella, la de los metadatos del PDF |
| `tables` | sí/no | `false` | las tablas de los PDF como filas Markdown (`page.find_tables` de pymupdf) |
| `table_requirements` | tabla | `{}` | un requisito por fila de las tablas con esas columnas: `{ id = "ID", text = "Requirement" }` |
| `authority` | entero | `0` | autoridad del grupo frente a otros: `1` = normativo, `2`..., `0` = ninguna. Solo la usa el grafo |
| `analysis_model` | texto | `""` | el modelo que analiza los documentos de esta fuente; vacío = el de `[llm]`. Entra en la huella: cambiarlo vuelve a analizar los documentos de la fuente (las secciones se guardan por modelo, así que volver al anterior las toma de la caché) |
| `reasoning_effort` | texto | `""` | el razonamiento de ese análisis; vacío = el de `[llm]`. Como el de `[llm]`, no vuelve a preguntar por las secciones ya analizadas |
| `enabled` | sí/no | `true` | `false` la deja fuera sin borrarla |

`atlas sections atlas.toml` muestra qué secciones saldrían de cada documento, sin llamar al
modelo.

### Versión, fecha y autoridad

`doc_id_regex`, `version_regex` y `date_regex` se buscan, en este orden, en las cabeceras y
pies que se repiten en las páginas, en las dos primeras páginas y en el título, asunto y
palabras clave de los metadatos del PDF; vale el grupo 1 (o todo, si la expresión no tiene
grupos). Sin fecha, se toma la de modificación (o creación) del PDF, como `AAAA-MM-DD`. Se
guardan en `structure.json` (`doc_id`, `version`, `date`) y en el nodo del documento, y
`atlas sections` los muestra. Para las muestras, cuya portada dice «Doc. TMS-SRS-001  Issue 2»:

```toml
doc_id_regex = 'Doc\.\s+(\S+)'
version_regex = '\bIssue\s+(\w+)'
```

Con `authority`, un requisito que no está en DOORS se compara con lo que declara el
documento del grupo de menor número: `trace` dice qué cifras difieren y `atlas graph` lista
en `documents disagree` los requisitos que varios documentos declaran con cifras distintas.
Dos documentos con el mismo `doc_id` y versiones distintas dan un aviso en `atlas graph`
(¿está sustituido el más antiguo?). Cambiar `authority` solo reconstruye el grafo.

### Tablas

Con `tables = true`, cada tabla que pymupdf encuentra en un PDF sustituye a los bloques de
texto que caen dentro de ella por sus filas en Markdown, una por línea (`| ICD-0001 | The
TMF shall... | Test |`), con la cabecera primero; antes salía cada celda como un párrafo
suelto. Una línea justo antes o después de la tabla que empieza por «Table n» o «Tabla n»
es su pie. pymupdf encuentra bien las tablas con bordes; las que no tienen, no siempre:
compruébalo con `atlas sections`, que dice cuántas tablas encontró, y mirando el
`_extracted_text.md` del documento.

`table_requirements` saca un requisito de cada fila de las tablas cuya cabecera tiene esas
dos columnas (sin distinguir mayúsculas): el ID es la primera palabra de su celda. Con
`analysis = "regex"` se suman a los de `req_regex`; con `"llm"`, completan lo que el modelo
no sacó (una fila que el modelo dio sin ID recibe el de la tabla). También vale para las
tablas de los Markdown.

En el índice, cada tabla es un registro de tipo `table` con la ruta de su sección y su pie
(si `[index] kinds` lo incluye; si no, va en los fragmentos). Una tabla grande se divide por
filas, y cada parte repite la cabecera; un fragmento nunca parte una fila.

### PDF escaneados

Una página que solo es una imagen (un escaneo sin OCR) no tiene texto. Si son más de la
mitad de las páginas de un PDF, el documento no se procesa: su unidad queda en error con
«scanned PDF: N of M pages have no text; nothing is sent to the model», que se ve en
`atlas status`, y `atlas sections` lo avisa. Se reintenta en cada `atlas run`, hasta la
cuarentena de `[pipeline] max_attempts`. Con menos páginas así, el documento se procesa y el
registro avisa de cuántas no tienen texto.

Con `ocr_command`, un PDF con alguna página sin texto pasa antes por esa orden: `{file}` es
una copia del PDF (el original no se toca) y `{out}` el PDF que debe escribir. El resultado
se guarda en `<workspace>/docs/.ocr`, por el hash del PDF y de la orden, así que el OCR se
hace una vez por versión del fichero; la orden forma parte de la huella de la fuente. Los
PDF con texto en todas las páginas no pasan por ella. [OCRmyPDF](https://ocrmypdf.readthedocs.io)
con `--skip-text` deja como están las páginas que ya tienen texto; necesita Tesseract y los
idiomas del documento (`"-l", "spa+eng"`).

## [sources.doors]

Los requisitos oficiales. atlas los lee de una base de intercambio (ver
[atlas/doors](../atlas/doors/README.md)).

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `adapter` | texto | `"database"` | `exchange`: la base de intercambio (lo normal). `database`: otra base con consultas propias |
| `modules` | lista | `[]` | qué módulos; vacío = todos |
| `model` | texto | `"full"` | `"none"`: el texto de ningún módulo sale del puesto (ver [Fuentes locales](#fuentes-locales-model--none)) |
| `local_only` | lista | `[]` | los módulos cuyo texto no sale del puesto, aunque `model` sea `"full"`: `["SRS_CLIENTE"]` |
| `enabled` | sí/no | `true` | `false` la deja fuera |
| `exchange` | tabla | — | `[sources.doors.exchange]` |
| `database` | tabla | — | `[sources.doors.database]` |
| `convention` | tabla | — | `[sources.doors.convention]` |
| `attributes` | lista | `["status", "verification"]` | los atributos de DOORS que atlas enseña y por los que filtra (ver abajo); `[]`, ninguno |
| `max_age_days` | entero | `0` | días que puede tener la exportación de un módulo antes de que `atlas run` y `atlas serve` avisen en el registro de que los requisitos pueden estar desfasados; 0 = sin aviso. La fecha es la que escribe el relleno de la base de intercambio (`exchange_modules.filled_at`); el adaptador `database` no la tiene |

`attributes` no elige qué atributos se leen: eso lo deciden el relleno de la base de
intercambio (con `exchange`) o `attribute_columns` (con `database`) y el `mapping.toml` del
exportador. Dice cuáles de los leídos enseña atlas y por cuáles se puede filtrar. Se
escriben como los nombra el relleno de la base, sin distinguir mayúsculas (`["Status",
"Verification Method"]`), y salen con el nombre que se escribe aquí. Cada uno que tenga un
requisito va:
- en las columnas de `atlas matrix` (CSV, Markdown y JSON) y en lo que dan `matrix` y
  `list_requirements` de cada requisito;
- en su texto del índice, como una línea `status: Approved; verification: Test`, así que
  también pesa en la búsqueda y llega al modelo con el requisito;
- en su `meta`, que da cada resultado de `search`;
- en los filtros: `attribute = "status=Approved"` en la herramienta `search`, y
  `--attribute status=Approved` en `atlas search` y `atlas ask`.

Cambiar `attributes` solo reindexa los módulos de DOORS, sin volver a leerlos; cambiar
`max_age_days` no reprocesa nada.

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
`id_normalize`, `id_regex`, `id_canonical` y las referencias en el texto; el resto son
columnas de `database`. `atlas graph` dice qué formas de ID aparecen en cada origen.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `id_pattern` | texto | `""` | expresión de un ID válido; se avisan los requisitos que no casan (los títulos y la información, no) |
| `id_normalize` | texto | `"upper"` | `upper` o `none`; los espacios se quitan siempre. Se aplica igual a DOORS, código y documentos, y a los IDs escritos de otra forma en una búsqueda («srs-4») |
| `id_regex` | texto | `""` | la expresión de un ID dentro de un texto: los IDs de una etiqueta `Req:` del código (todos los del resto de la línea: `Req: SRS-0004 and SRS-0005`, `see SRS-0004`, `SRS-0004 / SRS-0005`), los de un requisito de un documento con `analysis = "regex"` (los que van seguidos al principio) y los que da el modelo a uno con `llm`. Vacía: `id_pattern` sin `^` ni `$`, sin distinguir mayúsculas y sin casar dentro de una palabra más larga; sin las dos, las etiquetas se leen palabra a palabra hasta la primera que no parece un ID. Cambiarla vuelve a analizar el código y esos documentos |
| `id_canonical` | tabla | `{}` | la forma de los IDs del código y de los documentos, para que casen con DOORS: `{ regex = "^(SRS\|SSS)[-_ ]?0*(\\d+)$", format = "{1}-{2:04d}" }` hace `SRS-0004` de `srs-4` y de `SRS 0004`. `format` es un `str.format` de Python con los grupos de `regex` por número (`{0}`, el ID entero); un grupo de cifras es un número. Se aplica tras `id_normalize`; los IDs de DOORS no se tocan. Para encontrar `SRS 0004` en una etiqueta hace falta además un `id_regex` que lo admita |
| `inline_reference_regex` | texto | `""` | IDs citados dentro del texto, que pasan a ser enlaces |
| `inline_reference_type` | texto | `"references"` | tipo de esos enlaces |
| `link_semantics` | tabla | `{ derives = [], verifies = ["verifies"], ignore = ["references"] }` | qué es cada tipo de enlace para el grafo. `derives`: el origen deriva del destino (un SRS que satisface un SSS); cuenta en `derived` de la matriz, en el código y las pruebas «a través de» y en `impact`; vacía, todos los tipos que no están en las otras dos. `verifies`: el origen es un procedimiento de prueba del destino (un STP); sale en `procedures`, nunca entre las pruebas del código. `ignore`: solo referencias, que se ven en `trace` y no cuentan. Por defecto `ignore` lleva también el `inline_reference_type`. Un tipo no puede estar en dos listas. Cambiarla reconstruye la parte de DOORS del grafo |
| `top_modules` | lista | `[]` | los módulos de más arriba (los requisitos de sistema, `["SSS"]`), que no derivan de nada: `gaps` no los lista en `requirements_without_derivation`. Tampoco lista los módulos de procedimientos de prueba (aquellos cuyos enlaces a otros módulos son todos de un tipo de `verifies`) |
| `id_column`, `text_column`, `module_column`, `parent_column` | texto | `id`, `text`, `module`, `""` | columnas de `requirements_query`; `parent_column` da la jerarquía |
| `attribute_columns` | lista | `[]` | columnas que pasan como atributos |
| `link_columns` | tabla | `{ source = "source_id", target = "target_id", type = "link_type" }` | columnas de `links_query` |

## [sources.code]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `project` | texto | `""` | el proyecto GPR del código Ada (`"tms.gpr"`), relativo a `atlas.toml`: da las raíces, lo que se excluye, los comandos de GNAT y el proyecto del servidor de lenguaje que no se den (ver «El proyecto GPR», abajo) |
| `project_vars` | tabla | `{}` | las variables de escenario del proyecto, `{ BUILD = "debug" }`: el valor de cada `external ("BUILD", ...)` del GPR y un `-XBUILD=debug` en los comandos de gprbuild. Una variable que no está toma el valor del entorno y, si no, el suyo por defecto, como gprbuild |
| `roots` | lista | `[]` | carpetas del código (se recorren enteras); también las únicas que pueden leer y editar las herramientas de ficheros. Con `project` y sin `roots`, las carpetas de fuentes del proyecto y de los que importa |
| `languages` | lista | `["ada"]` | analizadores (plugins `code_analyzer`): `"ada"` y `"python"` |
| `exclude` | lista | `[]` | patrones que no se analizan ni se dejan leer: `["**/obj/**"]`. Con `project`, se añaden las carpetas de objetos, ejecutables y bibliotecas del proyecto |
| `req_tag` | texto | `"Req:"` | la etiqueta de trazabilidad en los comentarios |
| `req_regex` | texto | `""` | en vez de `req_tag`: grupo 1 = la lista de IDs |
| `test_patterns` | lista | `[]` | ficheros de prueba (`["**/tests/**"]`): sus subprogramas son pruebas, que verifican y cubren requisitos. Con `stray = "delete"` en el plan de `atlas develop`, un fichero nuevo de un encargo que casa con ellos y no con `protected` (un test del agente entre el código) se borra |
| `check_command` | lista | `[]` | el compilador para la herramienta `check_code`, como lista de argumentos: `["gcc", "-c", "-gnatc", "{include}", "{file}"]`. `{file}` es el fichero, `{include}` un `-I` por carpeta de código, `{build}` la carpeta `<workspace>/build` y `{project}` el fichero de `project`. Vacía: sin `check_code`. Con `project` y sin esta clave, `["gprbuild", "-P", "{project}", "-X...", "--autoconf={build}/gprbuild.cgpr", "--relocate-build-tree={build}/check", "--root-dir=...", "-c", "-u", "-f", "{file}"]`, y las `.ads` con `-cargs -gnatc` en `check_commands`: las comprobaciones compilan en un árbol aparte, para que el `.ali` de una especificación comprobada con `-gnatc` no deje sin enlazar la siguiente prueba |
| `check_commands` | tabla | `{}` | otro `check_command` para los ficheros de una extensión. GNAT ve más errores cuando genera código (una cadena asignada a otra de distinta longitud es un aviso, y un error con `-gnatwE`), pero no puede generarlo para una especificación sola: `check_command` sin `-gnatc` y `{ ads = ["gcc", "-c", "-gnatc", "{include}", "{file}"] }` |
| `test_command` | lista | `[]` | cómo construir una prueba para `run_test`, que después la ejecuta: `["gnatmake", "-q", "{include}", "-D", "{build}", "{file}", "-o", "{exe}"]`. `{exe}` es el ejecutable que se lanza: en `run_command`, el que exista de `<build>/<nombre>` y `<build>/<nombre>.exe` (GNAT le añade `.exe` en Windows). Con `project` y sin esta clave, `["gprbuild", "-P", "{project}", "-X...", "--autoconf=...", "{file}", "-o", "{exe}"]`: la prueba tiene que estar en las carpetas de fuentes del proyecto |
| `run_command` | lista | `["{exe}"]` | cómo se ejecuta lo que construyó `test_command`. Un envoltorio puede arrancar antes lo que la prueba necesita: `["with_server", "{exe}"]` |
| `test_commands` | tabla | `{}` | pruebas que no se construyen, por extensión: el comando que las ejecuta tal cual, `{ py = ["python3", "{file}"] }` |
| `program_command` | lista | `[]` | lo que ejecuta la herramienta `run_program`, con las líneas que le da el agente en su entrada estándar: el programa, o un cliente que se las pasa, `["with_server", "session"]`. Vacía: sin `run_program` |
| `protected` | lista | `[]` | patrones de ficheros que los agentes leen pero no cambian: `["tests/**"]`. Los tests, para que el agente que escribe el código no pueda cambiar lo que lo juzga |
| `syntax_commands` | tabla | `{}` | la comprobación de sintaxis por extensión, sobre una copia del texto nuevo: un cambio que rompe la sintaxis de un fichero que no tenía errores no se hace, y el agente recibe los errores y las líneas de alrededor. GNAT: `{ adb = ["gcc", "-c", "-gnats", "{file}"], ads = [...] }`. Python no necesita comando (`ast`); una lista vacía apaga la comprobación de su extensión (`py = []`) |
| `hints` | lista | `[]` | ficheros de pistas para los mensajes del compilador, relativos a `atlas.toml`: `[[hint]]` con `match` (expresión regular; sus grupos con nombre van al texto como `{nombre}`) y `hint`. Van antes que las de serie (`atlas/code/hints.toml`, para GNAT). El informe de `atlas develop` dice qué errores salieron sin pista y deja un borrador de cada una en `reports/hints-<plan>.toml` (también `atlas hints PLAN`), con la `hint` vacía: se escribe y se copia aquí |
| `rules` | lista | `[]` | ficheros de reglas para el texto nuevo de cada cambio, relativos a `atlas.toml`: `[[rule]]` con `match` (expresión regular sobre las líneas que añade el cambio, todas en un fichero nuevo), `message`, `level` (`"error"`: el cambio no se hace y el agente recibe la línea; `"warning"`, por defecto: se hace y el mensaje va en `notes`) y, opcional, `files` (patrones de nombres de fichero, `["*.adb"]`). Van antes que las de serie (`atlas/code/rules.toml`: avisos para `when others => null;`, `pragma Suppress` y `except: pass`) |
| `library` | lista | `[]` | carpetas de especificaciones (`.ads`) de bibliotecas que el proyecto usa, con comodines: el runtime de GNAT, `["~/.local/share/alire/toolchains/gnat_native_*/lib/gcc/*/*/adainclude"]` (`gcc -print-file-name=adainclude` dice cuál). Para la herramienta `library_spec` y para los errores que nombran algo que no existe; las especificaciones de las raíces del código van siempre |
| `reference` | lista | `[]` | manuales para la herramienta `search_reference` (y `atlas reference`), con comodines; una carpeta, todos los suyos: `.info` (los de GNAT, `~/.local/share/alire/toolchains/gnat_native_*/share/info/gnat_rm.info` y `gnat_ugn.info`), `.md`, `.txt` y `.html`. Van con las declaraciones de `library` en `<workspace>/reference.db`, no en el índice del proyecto |
| `language_server` | lista | `[]` | el servidor de lenguaje de las herramientas `find_definition`, `find_references` y `describe_symbol`, con comodines en el programa: el Ada Language Server de la extensión de VS Code, `["~/.vscode/extensions/adacore.ada-*/*/*/ada_language_server"]` |
| `language_server_project` | texto | `""` | el proyecto GPR que lee el servidor, relativo a `atlas.toml`; vacío: el de `project`, con `project_vars` como variables de escenario, y sin `project`, atlas escribe uno con las carpetas de las raíces que tienen fuentes Ada |
| `check_on_edit` | sí/no | `true` | cada cambio de las herramientas de ficheros devuelve en `check` lo que dice `check_code` del fichero (si hay compilador para su extensión): una vuelta menos, y el agente no puede decir que compila sin verlo |
| `tool_path` | lista | `[]` | carpetas que van antes del `PATH` para esos comandos, con comodines: `["~/.local/share/alire/toolchains/gnat_native_*/bin"]`. Una relativa lo es a `atlas.toml`, como las raíces del código |
| `command_timeout` | entero | `120` | segundos como mucho para cada comando: compilar, construir o ejecutar una prueba, ejecutar el programa. Al acabarse, se mata el comando con todo lo que arrancó (su grupo de procesos): un envoltorio como `with_server` ya no deja vivo el servidor |
| `check_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para `check_code` y la comprobación de sintaxis |
| `test_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para cada comando de `run_test` (construir la prueba y ejecutarla) |
| `program_timeout` | entero | `command_timeout` | en vez de `command_timeout`, para `run_program` |
| `model` | texto | `"full"` | `"none"`: nada del código sale del puesto (ver [Fuentes locales](#fuentes-locales-model--none)); los agentes con herramientas que leen o compilan el código no arrancan |
| `enabled` | sí/no | `true` | `false` lo deja fuera |

### El proyecto GPR

Un proyecto Ada ya dice en sus `.gpr` dónde están sus fuentes, dónde deja el compilador lo que
produce y qué otros proyectos usa. Con `project`, atlas lo lee en vez de pedirlo otra vez:

```toml
[sources.code]
project = "tms.gpr"
tool_path = ["~/.local/share/alire/toolchains/gnat_native_*/bin",
             "~/.local/share/alire/toolchains/gprbuild_*/bin"]
[sources.code.project_vars]
BUILD = "debug"
```

- `roots`, si no se dan: las carpetas de `Source_Dirs` del proyecto y de los que importa con
  `with` (también `extends` y los `Project_Files` de un proyecto agregado), una vez cada una;
  `src/**` es esa carpeta y todas las de debajo.
- `exclude`: se le añaden `Object_Dir`, `Exec_Dir` y las carpetas de biblioteca de esos
  proyectos, salvo las que tienen fuentes (el `Object_Dir` por defecto es la carpeta del
  proyecto).
- `check_command`, `check_commands` y `test_command`, si no se dan: gprbuild sobre el
  proyecto, con un `-X` por variable de `project_vars`. `--autoconf` guarda en
  `<workspace>/build/gprbuild.cgpr` la configuración de los compiladores que gprbuild busca en
  cada llamada (la mitad de tiempo por comprobación); se borra si cambia el compilador.
- El servidor de lenguaje abre ese proyecto con esas variables.

`atlas plugins` dice qué raíces salen del proyecto, qué carpetas se excluyen y cuántos
ficheros `.ads` y `.adb` ve. El lector es propio y sin dependencias: entiende lo habitual
(`with`, tipos, variables, `external`, `case`, listas, `&`, `for ... use`, `Proyecto'Atributo`)
y salta los paquetes (`Compiler`, `Builder`, `Naming`...). Lo que no entiende es un aviso con
su fichero y su línea, y lo demás se sigue leyendo; tampoco usa `Source_Files` ni
`Source_List_File` (lee todos los ficheros de las carpetas) ni un `with` que no esté junto al
proyecto que lo importa (una biblioteca instalada, que gprbuild busca en `GPR_PROJECT_PATH`).
Las raíces se recorren enteras: una carpeta de `Source_Dirs` sin `**` incluye también sus
subcarpetas.


## Fuentes locales (`model = "none"`)

Hay documentos (del cliente, con control de exportación) que no pueden ir ni al gateway.
`model = "none"` en una `[[sources.docs]]`, en `[sources.doors]` (o los módulos de
`local_only`) o en `[sources.code]` deja esa fuente en el grafo y en la búsqueda por IDs y
palabras sin que ningún texto suyo vaya a un modelo:

- **Análisis.** Los documentos se analizan con `regex` (o `none`); `analysis = "llm"` es un
  error.
- **Índice.** Sus registros se indexan sin vector: ninguna petición de embeddings. La
  búsqueda los encuentra por sus IDs y sus palabras, no por su significado.
- **Lo que llama al modelo** (`atlas ask`, `ask_docs`, `ask_atlas`, `atlas agent`,
  `verify`, `comply`, `link`, `impact`, `coverage`) los deja fuera del prompt y los
  devuelve aparte: en `atlas ask`, como fuentes `[local]`; en las herramientas, en
  `local_only`. Un requisito de un módulo local, o cuyo código es local, no se juzga: su
  veredicto es `local_only`.
- **Lo que no sale del puesto** (`atlas search`, `trace`, y las herramientas MCP `search`,
  `get_section`, `get_requirement`, `trace`, `read_source`, `build_context`) los da enteros.

Sí pueden ir al modelo sus identificadores: IDs de requisito, nombres de código y de
fichero. Qué garantiza y qué no, en [seguridad](seguridad.md).

```toml
[[sources.docs]]
name = "cliente"
paths = ["docs/cliente"]
model = "none"               # analysis = "regex" por defecto

[sources.doors]
local_only = ["SRS_CLIENTE"]
```

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
| `matrix_formats` | lista | `["csv", "md", "json"]` | qué escribe `atlas matrix` en `<workspace>/graph/matrix-<módulo>.*`: `csv` (para Excel), `md`, `json` (filas, resumen, huecos y `meta`) y `xlsx` (hoja de matriz con filtros, hoja de huecos y resumen con la fecha de DOORS; necesita `pip install "atlaslm[xlsx]"`). No reconstruye el grafo |
| `csv_delimiter` | texto | `";"` | separador de columnas del CSV: `";"` para Excel en español, `","` para Excel en inglés. El CSV va en UTF-8 con BOM. No reconstruye el grafo |
| `code_revision` | sí/no | `false` | `true`: al construir la parte del código del grafo se guarda `git rev-parse HEAD` de la primera raíz del código, y `meta` de la matriz la lleva (la cabecera del `.md` y el `.json` de `atlas matrix`). Es la revisión de la última construcción: un commit que no cambia el código no la mueve. Sin git o fuera de un repositorio, no hay revisión |

## [index]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `chunk_chars` | entero | `1200` | tamaño de los fragmentos (al menos 200) |
| `chunk_overlap` | entero | `200` | solape entre fragmentos (menos de la mitad de `chunk_chars`) |
| `kinds` | lista | todos | qué se indexa: `overview`, `section_summary`, `chunk`, `requirement`, `definition`, `open_issue`, `code_doc` (cada subprograma, tipo y variable del código, con su comentario y su código; necesita `[sources.code]`), `table` (cada tabla de un documento, con la ruta de su sección y su pie; sin él, las tablas van en los fragmentos) |
| `fts_tokenizer` | texto | `"unicode61 remove_diacritics 2"` | el tokenizador de FTS5 de la búsqueda léxica. `"porter unicode61 remove_diacritics 2"` reduce cada palabra inglesa a su raíz: «deleting» encuentra «Deleted tracks» y «log», «logs». Cambiarlo no reindexa nada: al abrir el índice, la tabla de texto se rehace desde los registros en segundos, sin embeber |

`porter` solo conoce el inglés. Conviene si los documentos están en inglés; los resúmenes de
sección siguen el `lang` de su fuente (español por defecto en la plantilla), y sobre texto
español las raíces de `porter` no son las del español. Antes de cambiarlo, mide con `atlas
eval` antes y después sobre un conjunto del proyecto. En las muestras (documentos en inglés,
resúmenes del manual en español), con `embeddinggemma` en Ollama y el título del documento
en su prefijo:

| | léxica acierto@1 | léxica MRR | híbrida acierto@1 | híbrida MRR |
|---|---|---|---|---|
| `unicode61` (por defecto) | 60 % | 0,66 | 80 % | 0,88 |
| `porter unicode61` | 75 % | 0,79 | 90 % | 0,93 |
| `porter` y `examples/synonyms.toml` | 80 % | 0,86 | 90 % | 0,93 |

Las dos preguntas en español no empeoran con `porter`: buscan en documentos en inglés.

## [search]

Lo que `atlas search`, `atlas ask` y las herramientas `search` y `ask_docs` toman del índice.
Cambiarlo no reprocesa nada. Los filtros (`--module`, `--chapter`, `--attribute`) no se
configuran aquí: van en cada búsqueda (ver [atlas.index](../atlas/index/README.md#filtros)).

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `results` | entero | `8` | resultados de una búsqueda y pasajes de una respuesta (`--k` en la línea de comandos) |
| `per_document` | entero | `3` | como mucho, resultados de un mismo documento, módulo o unidad de código mientras otros tengan; `0` = sin límite. No se aplica si la búsqueda se limita a un documento |
| `context_chars` | entero | `14000` | caracteres de pasajes (con sus vecinos) que recibe el modelo al responder |
| `context_radius` | entero | `1` | cuánto contexto lleva cada pasaje de una respuesta: los fragmentos vecinos de un fragmento, los registros de alrededor de un requisito de DOORS (en el orden del módulo) y los primeros fragmentos de un resumen de sección (`2 × context_radius + 1`); `0`, solo el propio |
| `scope` | texto | `"auto"` | `auto`: si el modelo ve que la pregunta es amplia (un resumen, una función entera, todos los requisitos de algo), lee sus documentos enteros; `passages`: siempre los mejores pasajes; `whole`: siempre enteros |
| `scope_chars` | entero | `100000` | caracteres de documentos enteros que se leen en una petición; si son más, se leen por partes y una última petición junta las notas de cada una |
| `max_parts` | entero | `12` | partes de documentos enteros que se leen como mucho para una pregunta. Antes de pedir nada, `atlas ask` calcula el plan (documentos, secciones, caracteres, partes y peticiones; y minutos del cupo si hay `[network] requests_per_minute`); con más partes no lee nada y pide acotar la pregunta a un documento. `atlas ask --dry-run` imprime el plan sin preguntar al modelo |
| `scope_documents` | entero | `3` | documentos que lee enteros, como mucho, una pregunta amplia que no nombra ninguno: los que más salen al buscarla en los resúmenes de los documentos y de sus secciones |
| `part_workers` | entero | `4` | partes de documentos enteros que se leen a la vez |
| `rrf_k` | entero | `60` | la `k` de Reciprocal Rank Fusion, que junta las listas de una búsqueda: cada resultado suma `peso / (k + puesto)` de cada lista. Menor, más cuentan los primeros puestos frente a los siguientes |
| `rewrite` | tabla | `{}` | `[search.rewrite]`: `model` y `reasoning_effort` de la reescritura de las preguntas (vacíos: los de `[llm]`) |
| `answer` | tabla | `{}` | `[search.answer]`: `model` y `reasoning_effort` de las respuestas, también de las que leen documentos enteros por partes (vacíos: los de `[llm]`) |
| `weights` | tabla | `{ids = 1.0, dense = 1.0, lexical = 1.0, keywords = 1.0}` | el peso de cada lista: `ids` (los IDs exactos de la pregunta), `dense` (los vectores), `lexical` (BM25 con la pregunta y sus reescrituras), `keywords` (BM25 con las palabras exactas de la reescritura). La que no se da pesa 1; con 0 no cuenta |
| `synonyms` | ruta | `""` | un fichero TOML de grupos de términos equivalentes del proyecto, relativo a este fichero (abajo) |

`synonyms` apunta a un fichero como `examples/synonyms.toml`:

```toml
[[groups]]
terms = ["track", "traza", "pista"]
```

Una consulta que trae un término de un grupo (en cualquier caso, con o sin tildes ni la «s»
del plural) hace una búsqueda léxica más, con los demás términos del grupo añadidos, que
entra en la fusión como una lista más. Sirve para el vocabulario del proyecto y para las
preguntas en español sobre documentos en inglés: en las muestras, con él, «alarma cuando se
pierde una traza» encuentra el requisito también sin vectores. `atlas check` y `atlas run`
rechazan un fichero que no existe o un grupo con menos de dos términos.

Las siglas de la pregunta (de 2 a 6 mayúsculas: «TMF») se buscan siempre entre las
definiciones del glosario, que entran con los IDs exactos.

`scope_chars` depende del contexto del modelo: 100 000 caracteres son unos 25 000 o 30 000
tokens. Con `[llm] context_tokens`, si `[search]` no los da, `scope_chars` es el 60 % de
`context_tokens − max_tokens` a 4 caracteres por token, y `context_chars` el menor de 14 000 y
ese mismo número: con 200 000 tokens y `max_tokens = 8192`, 460 000 caracteres por petición;
con 16 384 tokens (Ollama), 19 660. Lo que diga `[search]` manda. Con el gateway, de unos 200 000 tokens de entrada, puede subir a 400 000 (menos
peticiones, cada una más lenta); con Ollama y `OLLAMA_CONTEXT_LENGTH=16384`, 30 000. Las
partes se leen de `part_workers` en `part_workers`; `[llm] concurrency` y `[network]
max_concurrent` lo limitan. En un Mac de 24 GB
con `gemma4:12b`, cuatro a la vez llenan la memoria de intercambio: un documento de 20 000
caracteres en partes de 5000 tardó 11 minutos.

La reescritura es una petición corta que solo devuelve consultas y palabras clave, y las
respuestas necesitan leer bien las fuentes: cada una puede ir con su modelo y su
razonamiento, por la misma conexión, cupo y límite de peticiones a la vez que el resto:

```toml
[search.rewrite]
model = "gemma4:e4b"               # un modelo pequeño para la reescritura, sin razonar
reasoning_effort = "none"

[search.answer]
reasoning_effort = "low"           # el modelo de [llm], razonando poco
```

Los pesos se ajustan solo con un conjunto de evaluación escrito sobre el proyecto real
(`atlas eval`, ver [atlas/agents](../atlas/agents/README.md)), no con las muestras: son
sintéticas y repetitivas. En ellas, con `embeddinggemma`, `dense = 2.0` (o `lexical = 0.5`)
sube el MRR de la búsqueda híbrida de 0,83 a 0,93, pero eso dice más de las muestras que
del proyecto.

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

### El plan de `atlas develop`

El plan no va en `atlas.toml`: es su propio TOML, descrito en
[atlas/agents](../atlas/agents/README.md#desarrollo-guiado-por-los-tests). Dos de sus claves
dependen de esta configuración:

- `attempts` y `parallel` (encargos a la vez, los que ya tienen hechos los de su `after`)
  copian la carpeta de trabajo: las raíces de `[sources.code]` tienen que estar dentro de ella.
- `parallel` × `attempts` agentes a la vez piden una vuelta cada unos 30 s cada uno; se
  comparan con `[network] requests_per_minute` (o el de `[llm.network]`) y el plan avisa si van
  a llenarlo. Hasta que el cupo se comparta entre procesos, `parallel` es como mucho 2.

### [agents.eval]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `sets` | lista | obligatoria para `atlas eval` | conjuntos de casos (YAML), relativos al fichero: preguntas a un agente, búsquedas en el índice, consultas del grafo, respuestas de los documentos (`ask`) y planes de `atlas develop`. `atlas eval --set FICHERO` pasa solo ese conjunto |
| `runs` | entero | `1` | veces que se hace cada pregunta a un agente o caso `ask`, o cada plan de un caso `develop` que no diga las suyas: los modelos no responden siempre igual |

Los casos `develop` (`examples/eval/develop.yaml`, fuera de los conjuntos por defecto porque
tardan) ejecutan un plan sobre una copia temporal de su carpeta de partida (`from`). Toman el
modelo y la red de este fichero, y `[sources.code]` y `[agents] dirs` del `atlas.toml` que hay
junto al plan (o del de su clave `config`). Sus rutas son relativas a este fichero.
`ATLAS_PYTHON`, si no está puesta, es el Python que ejecuta atlas, para los comandos de un
proyecto en Python.

## El plan de `atlas develop`

El plan no va en `atlas.toml`: es un TOML aparte, `atlas develop PLAN atlas.toml` (ver
[atlas.agents](../atlas/agents/README.md#desarrollo-guiado-por-los-tests)). Una clave
desconocida es un error, como aquí.

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `agent`, `fix_agent`, `planner` | texto | `implementer`, el de `agent`, `""` | los agentes de los encargos, de las rondas y el que planifica cada encargo |
| `check`, `tests` | lista | `[]`; `tests` obligatoria | los ficheros que compilar tras cada encargo, y los tests que lo juzgan |
| `progress` | texto | `""` | expresión regular de hasta dónde llega un test que falla (el último número) |
| `build_fixes`, `advance_fixes`, `fix_rounds`, `stuck_rounds`, `restarts`, `attempts` | entero | 2, 2, 12, 2, 1, 1 | vueltas de cada clase |
| `task_calls`, `task_seconds`, `task_cost` | entero, entero, número | 0 | presupuesto de cada intento de un encargo: llamadas, segundos y coste (0 = sin límite) |
| `require_tags` | sí/no | `false` | un encargo conservado cuyos `requirements` no cita ningún subprograma cambiado vuelve una vez al agente |
| `requirement_chars` | entero | `1200` | caracteres de cada requisito que van delante del encargo |
| `[[tasks]]` | tabla | — | `id`, `text` (obligatorio), `advances` (tests que hace avanzar) y `requirements` (IDs del grafo: hace falta `atlas run`; con ellos, el encargo recibe su texto y, sin `advances`, los tests que los verifican) |

## [mcp]

| Clave | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `enabled` | sí/no | `true` | `false` apaga `atlas serve` |
| `tools` | lista | `[]` | las herramientas del servidor; vacío = las del perfil (`profile`). `edit_file`, `write_file`, `replace_subprogram`, `add_subprogram`, `run_test`, `library_spec`, `search_reference`, las del servidor de lenguaje, `verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` solo se ofrecen si se piden |
| `profile` | texto | `editor` con `[sources.code]`; si no, `all` | qué herramientas de las fuentes configuradas ofrece el servidor cuando `tools` está vacío: `editor`, las que necesita el agente de un editor para cambiar código (buscar, requisitos, trazabilidad, contexto y lectura de código); `trace`, las de trazabilidad de DOORS y los documentos; `all`, todas. Ver [los perfiles](../atlas/mcp/README.md#qué-herramientas-ofrece) |
| `usage_log` | texto | `"counts"` | registro de uso en la tabla `usage` de `atlas.db`: una fila por llamada a una herramienta del servidor o de un agente y por comando de la CLI, con su tiempo, si falló, el estado de la respuesta y las peticiones, tokens y coste. `counts`: sin el texto de los argumentos; `full`: con los argumentos, cortados a 200 caracteres; `off`: nada. `atlas usage` lo resume |
