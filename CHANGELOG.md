# Cambios

Formato de [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Mientras la versión
sea 0.x, la API de la librería puede cambiar entre versiones menores; cada cambio que
obligue a tocar la configuración o reprocese datos se anota en «Al actualizar».

## [Sin publicar]

### Añadido
- `[mcp] profile`: qué herramientas ofrece `atlas serve` cuando `[mcp] tools` está vacío.
  `editor` (por defecto con `[sources.code]`) da las que necesita el agente de un editor para
  cambiar código: `search`, `get_requirement`, `get_section`, `list_documents`, `trace`,
  `trace_code`, `impact`, `build_context`, `find_symbol`, `read_source`, `read_file` y
  `check_code` (y `context_at` y `similar_requirements`), 13 o 14 en vez de 29 con documentos
  y código. `trace` da las de trazabilidad
  de DOORS y los documentos, y `all` (por defecto sin código), todas las de las fuentes, como
  antes.
- Cada herramienta del servidor MCP lleva un título (`Trace code`), y las que solo leen el
  workspace y el código, sin el modelo, se anotan como idempotentes y sin salida al exterior
  (`idempotentHint`, `openWorldHint` falso): el cliente puede repetirlas sin preguntar.
- Un elemento de código se puede dar por un fichero y una línea, `src/tracks.adb:12` (también
  `C:\src\tracks.adb:12`): `trace_code`, `impact`, `read_source`, `get_node` y
  `build_context` dan lo del subprograma que contiene esa línea, o el tipo, la variable o la
  unidad. `context_at` (`path`, `line`) es `build_context` del fichero y la línea que el
  editor tiene abiertos. El subprograma se busca en el fichero tal como está ahora, para que
  las líneas añadidas desde el último `atlas run` no lo muevan. En la librería,
  `KnowledgeGraph.at(fichero, línea)`.
- `similar_requirements(req_id, k=3)` y un bloque «Similar requirements already implemented»
  en `build_context` de un requisito: requisitos parecidos que ya tienen código, como ejemplo
  de cómo se implementa y se etiqueta uno en el proyecto, con la firma del subprograma, la
  línea de su `Req:` y, del primero, el principio de su cuerpo. Sin modelo: primero los
  enlazados en DOORS (y su padre) y los que declaran las mismas secciones, después los que
  encuentra la búsqueda por IDs y palabras. El bloque es el último, el primero que se recorta.
- Las instrucciones de `atlas serve` llevan un párrafo «Traceability of this project»: la
  etiqueta con la que el código cita los requisitos (`[sources.code] req_tag` o `req_regex`),
  el patrón de los ID (`id_pattern`), los módulos de DOORS leídos y los pasos de un cambio
  con las herramientas ofrecidas (contexto, editar, `check_code`, `run_test` de las pruebas
  que da `trace`). También explican el servidor de lenguaje (`find_definition`,
  `find_references`, `describe_symbol`), `run_program` y `search_reference`.
- `atlas develop` comprueba el plan antes de la primera ejecución: que `agent` y `fix_agent`
  existen y tienen una herramienta que cambia el código, `check_code` si hay compilador y
  `run_test` el `fix_agent`, y que los tests están protegidos. Antes, un agente que no existía
  dejaba cada encargo con un error y la tanda recorría el plan entero sin llamar al modelo.
- `atlas develop PLAN atlas.toml --check` (`Developer.check()`): `check_code` y los tests una
  vez, sin modelo y sin cambiar nada, y aviso de los encargos de más de 1200 caracteres.
- Presupuesto por intento de un encargo en el plan: `task_calls`, `task_seconds` y `task_cost`
  (0 = sin límite). Al llegar a él no se lanzan más arreglos de compilación, vueltas de
  `advances` ni reinicios, y el paso lo dice (`budget_exceeded`).
- `atlas develop PLAN atlas.toml --resume` (`develop(..., resume=True)`): sigue una tanda
  cortada desde su informe. Los encargos terminados no se repiten, los que tienen `error` o
  quedaron `interrupted` sí, y las rondas siguen donde iban; el estado lo dicen los tests.
- `wall_seconds` en el informe de `atlas develop`: el tiempo de pared de la tanda, que no es la
  suma de los `seconds` de las ejecuciones (los tests van aparte y los intentos a la vez
  solapan).
- Parches de `atlas develop`: lo que deja cada encargo y cada ronda, en
  `reports/develop-<plan>/<id>.patch` y `all.patch`, enlazados desde el informe.
- Herramientas `outline` (los subprogramas de un fichero con sus líneas, su firma y sus
  requisitos, y sus `with` o `import`) y `read_subprogram` (uno solo, con su comentario). Con
  ellas, `replace_subprogram` y `add_subprogram` ya no necesitan un `read_file` del fichero
  entero (hasta 300 líneas). `examples/agents/implementer.md` y `tester.md` las usan.
- `[sources.code]` `check_timeout`, `test_timeout` y `program_timeout`: el tiempo de cada clase
  de comando, con `command_timeout` por defecto.
- El servidor falso (`tests/fakes/fake_api.py`) inyecta más fallos, sin cambiar la forma de su
  fichero de control: 429 en el chat (`chat_429_after`), 5xx puntuales (`chat_status_once`),
  `Retry-After` y cabeceras `x-ratelimit-*` en los rechazos, llamadas a herramientas
  (`tool_call`), `usage` calculado del tamaño real (`usage_chars_per_token`, que también corta
  las respuestas más largas que `max_tokens`) y servidores que aceptan `json_object` pero no
  `json_schema` (`no_structured_output: "json_schema"`). Con ellos se prueban por HTTP la
  espera por `Retry-After`, los escalones de salida estructurada, `finish_reason` y `atlas
  probe`.
- El servidor falso contesta también en streaming SSE (razonamiento, texto y llamadas a
  herramientas por índice, `finish_reason` y `usage`), lento (`slow_between_chunks`), colgado a
  mitad (`hang_after_chunks`, con `hang_keepalive` para los comentarios que manda OpenRouter) o
  sin streaming (`no_stream`), y `GET /` cuenta las peticiones abiertas.
- `[llm] stream`: las peticiones de chat de `JsonLlm` y de los agentes llegan en trozos y se
  juntan en la misma respuesta (texto, razonamiento, llamadas a herramientas, `finish_reason`
  y `usage`). Por defecto (`""`) se intenta hasta que el servidor demuestra que no puede
  (rechaza `stream`, contesta sin SSE o con un stream vacío); entonces se sigue sin él el resto
  del proceso. `true` y `false` lo fuerzan. `atlas probe` lo prueba con una cuarta petición y
  dice `streaming: ok | not supported`.
- `[network] idle_timeout` (120 s sin un trozo, una vez empezada la respuesta) y `turn_timeout` (600 s en total) para cada
  vuelta de chat, de `JsonLlm` y de los agentes: al vencer, se corta la conexión de verdad (el
  servidor ve irse al cliente) y la vuelta se pregunta otra vez, hasta dos veces. Los
  comentarios SSE con que OpenRouter mantiene viva la conexión no cuentan como trozos.
- Preguntas de trazabilidad en `atlas ask` y `ask_docs`: si la pregunta nombra requisitos o
  código y pregunta por sus pruebas, su implementación, su padre, sus enlaces, sus llamadas o
  el impacto de un cambio, `DocTools.route` da sin modelo su `trace`, `trace_code` o `impact`
  del grafo (sin grafo, su texto, padre y enlaces de DOORS) como las primeras fuentes de la
  respuesta (`extra_sources` en `Index.answer`). «¿Qué prueba verifica SRS-0004?» gastaba dos
  peticiones y solo lo encontraba por casualidad; ahora una, y con la fuente del grafo.
- Citas comprobadas contra el texto: `check_citations` señala, sin modelo, los `[n]` fuera de
  rango (`out_of_range`) y las frases cuyos IDs o cifras con unidad no están en las fuentes
  que citan (`unsupported`). `atlas ask` imprime «n claims not supported by their sources» y
  `ask_docs` lo dice en `warnings`.
- Estado explícito de cada respuesta, sin modelo: `status` (`not_found`, `answered` o
  `partial`), `uncited` (las frases sin cita) y `consulted` en `Index.answer`, `ask_docs` y
  `ask_atlas` (este, frente a lo que devolvieron sus herramientas). `atlas ask` imprime
  «status: partial (2 sentences without a source)». El asistente y los agentes de impacto y
  cobertura dicen que su respuesta es parcial cuando lo es.
- `atlas doctor [atlas.toml]`: lo que puede fallar en un puesto nuevo, antes de un `run` que lo
  descubriría horas después. Python y plataforma, los extras que importan los pasos y
  herramientas configurados, cuántos ficheros da cada ruta de las fuentes, la base de
  intercambio de DOORS (versión, módulos y fecha), los comandos del compilador y del servidor
  de lenguaje buscados en el `PATH` y `tool_path`, las variables del entorno que faltan, el
  workspace y la conexión. Tabla OK/WARN/FAIL, código de salida 1 si algo falla. En la
  librería, `atlas.doctor.diagnose(cfg)`, de solo lectura.
- `atlas init --profile gateway`: la plantilla del gateway corporativo
  (`atlas/templates/atlas-gateway.toml`), con `reasoning_effort = "none"`,
  `requests_per_minute = 28`, `max_concurrent = 6`, `workers = 6`, `ca_cert = "system"`,
  una `[[sources.docs]]` de ejemplo y `[search] scope_chars = 300000`. Sin `--profile` (o con
  `ollama`), la de siempre.
- `atlas check` (y `atlas doctor`) avisa cuando el modelo razona y `reasoning_effort` está
  vacío: los documentos se analizan mucho más despacio.
- `atlas estimate [atlas.toml] [--latency S]`: lo que haría `atlas run` ahora, sin hacerlo.
  Por paso, las unidades pendientes, las peticiones a las API (en `docs.process`, las
  secciones que no están en la caché más un resumen por documento; en el índice, los lotes
  de los textos sin vector) y el tiempo: máx(peticiones / `requests_per_minute`, peticiones ×
  latencia / a la vez), con la latencia del último perfil del paso o `--latency`. La misma
  línea sale al empezar cada paso de `atlas run` y en `atlas status`. En el proyecto grande da
  540 peticiones de chat y 237 de embeddings, como las medidas. En la librería,
  `Runner(dry_run=True)` y `Step.estimate`.
- Presupuesto en `atlas run`: `--until HH:MM` (la de mañana si ya pasó), `--max-minutes N`,
  `--max-requests N` y las claves `[pipeline] max_minutes` y `max_requests` (0 = sin
  límite). Al llegar al límite no empieza ninguna unidad más, un documento a medias deja de
  pedir secciones y queda para la próxima ejecución, los pasos siguientes no empiezan, el
  informe dice `stopped_by = "budget"` y sale con el código 3; relanzarlo sigue sin repetir
  lo hecho. Sin hilo temporizador: el `Runner` mira el presupuesto antes de cada unidad y
  cada vez que una unidad larga consulta `stopping` (`atlas.pipeline.Stopping`).
- Progreso en `atlas run`: una línea por stderr cada 30 s o cada 25 unidades de un paso, con
  lo que queda según la estimación («docs.process 120/2080 units, 35 min, ~2 h 10 min
  left»).
- `docs/instalacion.md`: cómo programar la ingesta de noche con el Programador de tareas de
  Windows, y los códigos de salida de `atlas run` (0, 1, 2 y el nuevo 3).
- `atlas matrix` escribe los formatos de `[graph] matrix_formats` (`csv`, `md` y `xlsx`). La
  escritura pasa a la librería: `atlas.graph.write_matrix(res, prefijo, formatos, delimitador)`.
- Extra `[xlsx]` (openpyxl, wheel puro): `matrix-<módulo>.xlsx` con una hoja de matriz con
  filtros y anchos, una de huecos y un resumen con la fecha de la parte DOORS del grafo.
- `--json` en `status`, `sections`, `graph`, `trace`, `matrix`, `verify`, `comply`, `link`,
  `coverage` e `impact`: por stdout solo el diccionario de su función de librería, para el
  Programador de tareas y los scripts; los avisos siguen por stderr. Un error sale también como
  JSON (`{"error": ...}`), con código de salida 1.
- `[sources.doors.convention] link_semantics`: qué es cada tipo de enlace de DOORS para el
  grafo. `derives` (por defecto, todos los tipos que no están en las otras dos) hace del
  origen un requisito derivado del destino; `verifies` (`["verifies"]`), un procedimiento de
  prueba del destino; `ignore` (`["references"]` y el `inline_reference_type`) lo deja como
  simple referencia.
- Procedimientos de prueba de DOORS, aparte de las pruebas del código: `procedures` en
  `trace`, en cada fila de `matrix` (y una columna en el CSV y el Markdown de `atlas
  matrix`) y en `impact`; `with_procedures` en el resumen de la matriz;
  `without_test_procedure` en sus huecos y `requirements_without_test_procedure` en `gaps`.
- Consultas de una revisión de asignación, sin herramientas nuevas: `gaps` tiene
  `requirements_without_derivation`, los requisitos sin ningún enlace de derivación hacia
  otro módulo, con su recuento por módulo (`by_module`), y `linked_to_modules_not_read`, los
  IDs que DOORS enlaza en módulos que no se han leído, con quién los enlaza y con qué tipo.
  `[sources.doors.convention] top_modules` deja fuera los módulos de sistema; los de
  procedimientos de prueba quedan fuera siempre. `atlas graph` imprime los dos recuentos.
- `ancestors` en `trace`: la cadena de requisitos de los que deriva en otros módulos, hasta
  el de sistema (5 niveles), cada salto con su tipo, su nivel, desde qué requisito se llega
  y el texto corto.
- Las muestras tienen un módulo de procedimientos de prueba, `STP_TMS`, con dos enlaces
  `verifies` a SRS_TMS, y `atlas.test.toml`, `atlas.dev.toml` y `atlas.openrouter.toml` lo
  leen.
- La fecha de la exportación de DOORS, visible: `RequirementsAdapter.exported_at(módulo)` y
  `module_info(módulo)` (el adaptador `exchange` las lee de `exchange_modules`; `database`
  no las sabe). El paso `doors.requirements` las guarda en la tabla `modules` de `atlas.db`,
  y las enseñan `atlas status` (`DOORS: 3 modules, exported 2026-10-14, 9 days ago`),
  `atlas graph`, el nodo `module:X` del grafo, la cabecera del Markdown y una columna del CSV
  de `atlas matrix`, `meta` de `matrix`, y un campo `exported_at` en `trace`,
  `get_requirement` y `build_context`. Antes nada decía de qué exportación salía la matriz
  de una revisión.
- `[sources.doors] max_age_days`: `atlas run` y `atlas serve` avisan de los módulos
  exportados hace más de esos días (0, por defecto: sin aviso). Cambiarla no vuelve a leer
  nada. `tools/doors_export/README.md` dice quién exporta y cada cuánto.
- `[sources.doors] attributes` tiene efecto: son los atributos de DOORS que atlas enseña y
  por los que filtra. Van a las filas de `matrix`, a `list_requirements` y a las columnas del
  CSV y del Markdown de `atlas matrix`; no distinguen mayúsculas y por defecto son `status` y
  `verification`, así que sin tocar la configuración nada cambia. No es una lista blanca de
  importación (eso lo hacen `attribute_columns` y el `mapping.toml` del exportador).
- `matrix` devuelve `meta`: el módulo, la exportación de DOORS, la versión de atlas, la fecha
  de construcción de cada parte del grafo y, con `[graph] code_revision = true` y git, la
  revisión del código (`git rev-parse HEAD` al construir el grafo del código). `atlas matrix`
  la escribe en la cabecera del Markdown y en un `matrix-<módulo>.json`.
- `[sources.doors.convention] id_regex`: la expresión de un ID dentro de un texto. Una
  etiqueta `Req:` del código da todos los IDs del resto de su línea, y un requisito de un
  documento (`analysis = "regex"`) los que se escriben seguidos al principio. Si falta, sale
  de `id_pattern` sin `^` ni `$`, sin distinguir mayúsculas y sin casar dentro de una
  palabra más larga; sin ninguna de las dos, las etiquetas se leen como antes. La sonda del
  análisis de 2026-10: `Req: SRS-0004 and SRS-0005` daba solo `SRS-0004`, `see SRS-0004`
  nada y `SRS-0004 / SRS-0005` perdía el segundo; ahora dan los dos, uno y los dos. Los IDs
  que da el modelo a un requisito de un documento («SRS-0004 / SRS-0005», «Req. SRS-0006»)
  también se separan con ella al construir el grafo.
- `[sources.doors.convention] id_canonical = { regex, format }`: las variantes de un ID en
  el código y los documentos se escriben como en DOORS (`srs-4` y `SRS 0004` → `SRS-0004`
  con `regex = "^(SRS|SSS)[-_ ]?0*(\\d+)$"` y `format = "{1}-{2:04d}"`). Los IDs de DOORS no
  se reescriben.
- `atlas graph` enseña las formas de ID por origen (código, DOORS, documentos): prefijo,
  separador y número de cifras, con cuántos IDs tienen cada una. Están también en las
  estadísticas de cada parte del grafo (`id_shapes`). Responden a qué convención sigue de
  verdad el proyecto en cuanto se procese.
- El analizador Ada lee los cuerpos de tareas (`task body`), entradas (`entry ... when`),
  cuerpos protegidos y operadores (`function "+"`, que en el grafo se llama `"+"`). Antes no
  daban subprograma, y sus llamadas y sus `Req:` desaparecían sin aviso; en un servidor Ada el
  bucle principal suele ser una tarea. Cada subprograma lleva su `kind` (procedure, function,
  operator, task, entry o protected) en el `CodeUnit` y en el nodo (`subprogram_kind`), y
  `atlas graph` los cuenta. Las etiquetas de las entradas y subprogramas de un cuerpo
  protegido son suyas, no del cuerpo protegido.
- La resolución de llamadas del grafo de código sigue las cláusulas `use` (la lista `uses`
  del `CodeUnit`), los renombramientos (`procedure Log renames Ada.Text_IO.Put_Line`,
  `package TIO renames Ada.Text_IO`) y las instancias genéricas (`package Int_Stack is new
  Stacks (Integer)`), que el analizador Ada guarda como `aliases` (nombre → destino). Un
  nombre se busca en la propia unidad, después como lo que nombra su alias, después en los
  paquetes de sus `use` y por último en todas. `Put` con `use Store` y `with` de dos paquetes
  que lo tienen era una llamada ambigua a los dos; ahora va a `store.put`. Un alias de algo de
  fuera es una llamada externa por su nombre completo (`ada.text_io.put_line`, no `log`).
  `atlas graph` cuenta las llamadas resueltas por cada camino.
- `[index] fts_tokenizer`: el tokenizador de FTS5 de la búsqueda léxica. Con `"porter unicode61
  remove_diacritics 2"`, «deleting» encuentra «Deleted tracks» y «log», «logs». Cambiarlo rehace
  solo la tabla de texto al abrir el índice, en segundos y sin embeber. El valor por defecto no
  cambia; en las muestras, `porter` sube la léxica del 60 % al 75 % de acierto@1 y la híbrida del
  80 % al 90 %. Es solo inglés: conviene con documentos en inglés y medido con `atlas eval`.
- `[search] synonyms`: un fichero de grupos de términos equivalentes (`examples/synonyms.toml`);
  una consulta con uno de ellos hace una búsqueda léxica más con los demás, que entra en la
  fusión. Las siglas de la pregunta (de 2 a 6 mayúsculas) se buscan entre las definiciones del
  glosario, con los IDs exactos.
- Un ID escrito de otra forma en una búsqueda se busca como está en el índice: «srs0004»,
  «SRS_0004» y «srs-4» dan lo mismo que «SRS-0004» (antes no encontraban nada), con los prefijos
  de los requisitos indexados y su anchura de dígitos, y solo si el ID existe.
- `[embeddings] family` (`embeddinggemma`, `nomic`, `e5` o `none`) y los prefijos literales
  `query_prefix` y `document_prefix`. Antes los prefijos se elegían por el nombre del modelo y, si
  el gateway lo publicaba con otro, no se ponía ninguno sin aviso; ahora el registro lo avisa y
  `atlas check` imprime los prefijos efectivos. Entran en la huella del índice.
- `[search] context_radius` (1): el contexto de cada pasaje de una respuesta. Un requisito de
  DOORS llega con los registros que lo rodean en su módulo y con el pasaje del documento que
  dice el mismo ID; un resumen de sección, con los primeros fragmentos de la sección; un
  requisito o una definición de un documento, con el fragmento que lo contiene y sus vecinos.
  Antes solo se ampliaban los fragmentos.
- Filtros en la búsqueda, por la `meta` de los registros: `attribute = "status=Approved"`
  (varios pares con «;»), `module` (un módulo de DOORS) y `chapter` (un capítulo, por su
  número o su título) en la herramienta `search`; `--attribute` (repetible), `--module` y
  `--chapter` en `atlas search` y `atlas ask`; `where` en `Index.search` e `Index.answer`.
  Se suman entre sí y con `document` y `kind`. En las muestras, «deletion» no daba ningún
  requisito de DOORS entre sus ocho resultados; con `--attribute status=Approved`, ocho
  requisitos aprobados. Un filtro que no casa con nada devuelve las claves y los valores que
  hay, y una pregunta amplia con filtros lee enteros solo los registros que casan. La
  descripción de `search` y las instrucciones del servidor MCP explican los filtros.
- Cada registro del índice lleva su `meta` (columna `meta` de `index_records`), y cada
  resultado de `search` la devuelve: en los requisitos de DOORS, el módulo, el capítulo y los
  atributos de `[sources.doors] attributes`; en los fragmentos, resúmenes de sección y
  requisitos de un documento, su capítulo (el primer elemento de su ruta); en el código, la
  clase del elemento (`subprogram`, `test`, `type`, `variable` o `constant`) y su fichero.
- Plan previo de las preguntas amplias: antes de pedir nada, `atlas ask` y `ask_docs`
  calculan cuántas secciones, caracteres, partes y peticiones costará leer enteros los
  documentos (y los minutos del cupo, si hay `[network] requests_per_minute`). Con más de
  `[search] max_parts` partes (12) no se lee nada: la respuesta es un error con el plan y los
  capítulos de los documentos, para acotar la pregunta. `atlas ask --dry-run` imprime el plan
  sin preguntar al modelo. `[search] scope_documents` (3) sustituye a la constante fija.
- `atlas ask --section` y `section` en `ask_docs`: leer entera solo una sección y lo que
  cuelga de ella, por su número o título («2.1», «Logging»), por su ruta («Track management >
  Deletion») o por su ID (el de un resultado de búsqueda, o el del título en un módulo de
  DOORS). Una sección que no está da un error con los capítulos de los documentos.
- Las notas de las partes de una pregunta amplia se juntan por niveles cuando no caben en
  `[search] scope_chars`, como el resumen de un documento; antes iban todas en una última
  petición, aunque no cupiera. Las citas se conservan y se numeran 1, 2, 3… en el orden de las
  fuentes.
- `tags` en los casos de evaluación (`[paraphrase, spanish]`) y cifras por tag: acierto@k y
  MRR de la búsqueda, híbrida y léxica, y aciertos del grafo, en el informe y en la salida de
  `atlas eval`. `examples/eval/retrieval.yaml` pasa a tags los tipos que tenía en
  comentarios. Un texto esperado `section:<documento>#<id>` casa solo con esa sección.
- `atlas eval` guarda una copia de cada informe, `reports/eval-<conjunto>-<versión>-<fecha>.json`,
  con una cabecera `meta`, y `atlas eval --compare` compara cada caso con el último informe
  de otra versión: rango anterior y nuevo, y las regresiones (código 1 si hay alguna).
- Casos `ask` en los conjuntos de evaluación: la pregunta va a `Index.answer` (con
  `document`, `scope` y `section` si los tiene) y pasa si la respuesta contiene lo esperado
  y, con `cited: true`, si cada texto esperado está en lo que se leyó de una fuente citada.
  Guarda peticiones, `scope` y segundos. Cuatro casos nuevos en
  `examples/eval/assistant.yaml`.
- El informe de `atlas eval` guarda los tokens y el coste de cada respuesta de un agente, y
  una cabecera `meta` con el modelo, su `reasoning_effort` y `provider`, el modelo de
  embeddings, la versión, la fecha, `runs` y el hash del YAML.
- `atlas eval --generate FICHERO --n 50`: un conjunto de casos deterministas sacados del
  `atlas.db` y del grafo, sin modelo (búsqueda por el ID exacto de requisitos al azar y por
  el título de secciones, `trace` de requisitos con código, `impact` de subprogramas con
  llamadores), con semilla fija. La medida de regresión del proyecto real desde el primer
  día; no sobrescribe un conjunto escrito a mano.
- `tools/compare_models.py` añade tokens por caso, dólares en total y por acierto, y el
  acierto@3 y el MRR de la búsqueda; escribe `reports/compare-<fecha>.md` y `.json`.
- Las pruebas con modelo pasan contra cualquier configuración: `ATLAS_TEST_CONFIG` (por
  defecto `atlas.dev.toml`) da su `[llm]`, `[embeddings]`, `[network]` y `[agents] model`,
  y se omiten si `atlas check` falla sobre ella. Marcador `model` (`ollama` sigue valiendo) y
  `make test-gateway`, que las pasa contra el gateway de `atlas.toml`. Guía en el README
  («Niveles de modelo») y en `docs/instalacion.md`.
- `atlas cache export FICHERO` e `import FICHERO` (la librería, `atlas.cache.pack` y `unpack`):
  lo que un puesto pidió a los modelos, para los demás. El fichero es un SQLite con los vectores
  de la caché de embeddings, los análisis de sección de `docs/.cache` y, con
  `--with-judgements`, las cachés de juicios de `verify`, `comply` y `link`; la cabecera dice la
  versión de atlas, el proyecto, los modelos, la fecha y los recuentos. `import` solo añade lo
  que falta, nunca sustituye, y dice cuánto entró. Rechaza los vectores de otro modelo de
  embeddings: la clave de un vector lleva el modelo dentro del hash y nunca casarían. Sobre
  las muestras, un workspace nuevo con la caché importada procesa todo sin ninguna petición
  de chat ni de embeddings. En el proyecto real, cada puesto se ahorra las ~2000 secciones y
  ~20 000 embeddings de su primer `atlas run` (horas con 28 peticiones por minuto).
- `[project] shared_cache`: una carpeta de red con cachés exportadas, como caché secundaria de
  solo lectura. Lo que el workspace no tiene se busca ahí y se copia al workspace; `atlas run`
  nunca escribe en ella. Si no se puede leer, se sigue sin ella con un aviso. Nunca se
  comparte un `atlas.db`.
- Fuentes locales: `model = "none"` en una `[[sources.docs]]`, en `[sources.doors]` (o los
  módulos de `local_only`) y en `[sources.code]`. Ningún texto de esa fuente va a un modelo:
  sus documentos se analizan con `regex`, sus registros se indexan sin vectores (los
  encuentran las búsquedas por IDs y por palabras), y `ask`, `ask_docs`, `ask_atlas`,
  `atlas agent`, `verify`, `comply`, `link`, `impact` y `coverage` los dejan fuera del
  prompt y dicen qué dejaron (`local_only`). `search`, `trace` y las herramientas MCP que no
  llaman al modelo los siguen dando enteros. Un agente con herramientas que leen el código
  no arranca si el código es local.
- Registro de lo enviado: `atlas status` dice, por modelo y por fuente, cuántas secciones se
  analizaron, cuántos textos se embebieron y cuántas respuestas, juicios, informes y
  ejecuciones de agentes se pidieron, con la fecha del último envío; el informe de
  `atlas run` dice lo que envió esa ejecución (`sent`). Lo guarda una tabla nueva de
  `atlas.db` (`sent`), que cuenta desde la primera ejecución de esta versión.
- `docs/seguridad.md`: qué garantizan las fuentes locales y qué no (sus identificadores
  pueden salir; las herramientas MCP dan su texto al editor), y la prueba que lo comprueba con
  los cuerpos de las peticiones que recibe el servidor falso (`bodies` en su fichero de
  control).
- Tokens y coste de cada petición: `atlas.llm.note_usage` lee el `usage` de cada respuesta
  (de `JsonLlm`, los embeddings, los agentes, `atlas check` y `atlas probe`) y lo suma a
  `usage()`: `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`,
  `embedding_tokens` y `cost` cuando el servidor lo da. Los perfiles de `atlas run` y
  `atlas status` los enseñan por paso; `atlas ask`, `atlas agent` y `atlas eval` los imprimen,
  y `atlas eval` los guarda en su informe. `set_usage_label` dice qué comando y qué paso
  están en marcha. Sin precios configurables: el gateway no tiene precio público.
- Registro de uso en el workspace: una fila por llamada a una herramienta de `atlas serve` o
  de un agente y por comando de la CLI, en la tabla `usage` de `atlas.db` (varios procesos
  escriben a la vez sin pisarse, también en Windows). Lleva la fecha, el origen (`mcp`, `cli`
  o `agent`), los milisegundos, si falló, el estado de la respuesta de `atlas ask` y
  `ask_docs` (con fuentes o sin ellas) y las peticiones, tokens y coste. `[mcp] usage_log`:
  `counts` (por defecto, sin el texto de los argumentos), `full` (con ellos, cortados a 200
  caracteres) u `off`. `atlas usage [--since 30d]` lo resume: llamadas por herramienta y
  semana, mediana de milisegundos, errores, tokens y coste, respuestas con y sin fuente.
- `atlas.plugins.instrumented`: el envoltorio de cada llamada que usan el servidor MCP, los
  agentes (antes `recorded`, que sigue) y la CLI.
- Registro de cada ejecución en el workspace: `[project] log_dir` (`logs` por defecto; `""`
  lo apaga) con un fichero por día, `atlas-AAAA-MM-DD.log`, con todo lo de atlas a nivel
  `DEBUG` y los avisos de las librerías, la fecha, el proceso y el comando y paso en marcha.
  No se rota renombrando (en Windows, `atlas run` y `atlas serve` lo tienen abierto a la vez):
  cada día empieza uno, y los de más de `[project] log_keep_days` días (14) se borran al
  arrancar. Una línea al empezar y al acabar cada acción, con la versión y el fichero de
  configuración; las líneas de `atlas run` llevan el id de su fila de la tabla `runs`.
  `atlas status` dice dónde está. `atlas.logs.setup_logging` lo prepara.
- El límite de peticiones es del puesto, no de cada proceso (`[network] shared_limit`, por
  defecto con `requests_per_minute` o `tokens_per_minute`). `atlas serve`, un `atlas run` largo
  y `atlas develop` creían tener cada uno las 28 peticiones por minuto del gateway. Ahora
  comparten la ventana del último minuto, el ritmo adaptado y las pausas en
  `~/.atlas/limits/<servidor>-<puerto>.json` (en Windows, `%LOCALAPPDATA%\atlas\limits`;
  `ATLAS_LIMITS_DIR` cambia la carpeta), bajo un bloqueo de fichero como el del workspace. El
  fichero no lleva la clave ni un hash de ella; si no se puede bloquear, cada proceso vuelve a
  ir por su cuenta, y si está roto se reescribe. Medido con el servidor falso (20 peticiones por
  minuto de cupo, 1 s por respuesta), un `atlas run` de documentos con el modelo y el índice y,
  a la vez, cuatro `atlas ask` seguidos, los dos con `requests_per_minute = 20`:
  | | antes | con la ventana compartida |
  |---|---|---|
  | rechazos 429 en el servidor | 5 (1 del `run`, 4 de la cuarta pregunta) | 0 |
  | `atlas run` | 277 s | 241 s |
  | cada `atlas ask` | 10, 15, 14 y 47 s | 22, 27, 30 y 36 s |
  Ninguna petición falló al final: los 429 se reintentan. Lo que cambia es que la pregunta que
  llega con el lote en marcha espera su turno en vez de llevarse los rechazos, y que el lote no
  pierde ritmo. El servidor falso tiene `rpm_limit` para probarlo.
- `atlas status` dice, por paso y destino, cuánto se llenó el cupo y por qué:
  «chat: 540 in 31 min = 17/min of 28; latency 21 s; 6.1 in flight; 10 would fill the quota».
  Las peticiones en vuelo de media son los segundos del servidor entre los del paso, y las que
  llenarían el cupo, `requests_per_minute` × latencia / 60 (ley de Little). La guía pedía
  adivinar `workers` con una latencia supuesta de 10 s, y con 20-30 s por sección el cupo se
  llenaba a medias. Los contadores de uso separan los segundos por destino (`chat_seconds`,
  `embeddings_seconds`, `other_seconds`); en las ejecuciones de la 0.2 se reparten los de
  `api_seconds`.
- `[llm] concurrency`: las peticiones al modelo en vuelo, separadas de los documentos que se
  abren a la vez (`[pipeline] workers`). Por defecto, las `workers`, como antes.
- `[search] part_workers`: las partes de documentos enteros que se leen a la vez (4, como antes).
- `[llm] context_tokens`: el contexto del modelo (0 = desconocido, como antes). `atlas check` lo
  pide al servidor cuando lo publica (OpenRouter y vLLM en `GET /models`, Ollama en `/api/show`)
  y lo propone o avisa si no coincide. Con él, `[search] scope_chars` y `context_chars` salen del
  contexto si no se dan (el 60 % de `context_tokens − max_tokens`, a 4 caracteres por token), y
  un agente estima los tokens de cada vuelta y, si no cabe, deja fuera resultados viejos de
  herramientas con un aviso: antes enviaba todo, sin recuento ni tope. Queda por hacer la
  calibración de los caracteres por token con el `usage` del servidor: depende de guardar los
  tokens de cada petición, que aún no está en `develop`.
- `examples/agents/fixer.md` y `planner.md`, los agentes con los que se midió `atlas develop`
  con `gemma-4-26b` en `atlas-socket-demo`, adaptados a un proyecto Ada cualquiera. Cada uno
  dice en un comentario con qué modelo se midió; `atlas.openrouter.toml` les pone su muestreo
  (`[agents.implementer]`, `[agents.fixer]`, `[agents.planner]`: razonamiento `low`,
  temperatura 1,0, `top_p` 0,95, `top_k` 64). `tests/test_examples.py` comprueba que cargan y
  que sus herramientas existen.
- Encargos anclados a requisitos en `atlas develop`: `requirements = ["SRS-0113"]` en cada
  encargo, comprobados contra el grafo al arrancar (un ID que no está, o un workspace sin
  `atlas run`, para la tanda antes de empezar). El encargo lleva delante el texto oficial de
  cada requisito, las secciones que lo declaran y el código que ya lo cita (`requirement_chars`);
  sin `advances`, se rellenan con los tests del plan que lo citan y con los de los
  procedimientos enlazados por `verifies` (`test_stp_0101.py`); y tras conservarlo, el paso dice
  qué requisitos no cita ningún subprograma cambiado (`untagged`). Con `require_tags = true`,
  el encargo vuelve una vez al agente para etiquetarlos.
- Un banco de encargos en `atlas eval`: casos `develop` (un plan, una carpeta de partida que se
  copia a un temporal antes de cada ejecución, `expect_tests_passed` y `runs`) en un conjunto
  aparte, `examples/eval/develop.yaml`, sobre `examples/develop/calc`, un proyecto Python
  mínimo con su plan. El informe guarda de cada ejecución los tests que pasan, las ediciones
  hechas y fallidas, las compilaciones con errores, las llamadas, el tiempo y el coste, con el
  modelo. `atlas eval --set FICHERO` pasa un solo conjunto (`make eval-develop`) y
  `tools/compare_models.py --develop` añade una tabla por modelo.
- Reglas deterministas para el código nuevo (`[sources.code] rules`): ficheros TOML con
  `[[rule]]` `match`, `message`, `level` y `files`, aplicados por las herramientas que escriben a
  las líneas que añade cada cambio, también en ficheros nuevos. Un `error` no se escribe y el
  agente recibe la línea; un `warning` va en `notes`. De serie, como avisos (`atlas/code/rules.toml`):
  `when others => null;` y `pragma Suppress` en Ada, `except: pass` en Python. En la tanda 4 de
  `atlas develop`, un `when others => null` se tragó una excepción que ningún test veía.
- `reviewer` y `review_fixes` en el plan de `atlas develop`, apagado por defecto: tras un encargo
  que compila y no rompe nada, un agente de solo lectura lee el encargo y el diff y contesta en
  JSON; sus problemas vuelven al agente una vez. Ejemplo en `examples/agents/reviewer.md`.
- `atlas hints PLAN` y `draft_hints` (`atlas/agents/hints.py`): borradores de pistas para los
  errores del compilador que salieron sin pista en la última tanda de `atlas develop`, agrupados
  por forma, con el `match` derivado del mensaje, la `hint` vacía y, como comentario, el diff de
  la primera escritura de la misma ejecución tras la que el error ya no estaba. Van en
  `reports/hints-<plan>.toml` y en la sección «Hints to write» del informe; nunca se usan solos.
- Los ficheros que crea cada encargo conservado de `atlas develop` van en su paso (`created`), en
  la sección «New files» del informe y en la salida del comando. `stray = "delete"` en el plan
  borra, antes de juzgar el encargo, los nuevos que casan con `[sources.code] test_patterns` y no
  con `protected`: en la tanda 4 el agente dejó en `src` un fichero suyo de prueba. Por defecto,
  `stray = "report"`.
- `atlas.db` tiene versión de esquema (`PRAGMA user_version`, hoy 2) y migraciones: al abrir
  un workspace de un atlas anterior, atlas hace una copia (`atlas.db.bak-<n>`), aplica los
  cambios en una sola transacción y lo anota en el registro. Un workspace de un atlas más
  nuevo no se abre. `atlas status` muestra la versión del esquema junto a la de atlas de la
  última ejecución. Antes cada tabla se creaba si faltaba y nada comprobaba que el resto
  sirviera.
- `docs/api.md`: la API pública, lo que se promete de la librería. Cada módulo tiene
  `__all__`; `atlas` exporta `load`, `Config`, `ConfigError` y `__version__`, y `atlas.agents`
  `run_agent` y `build_context`. Una prueba comprueba que los ejemplos de `examples/library`
  solo usan esos nombres.
- Pruebas: `pytest` imprime las 15 más lentas (`--durations=15`), el marcador `slow`
  (ejemplos, proyecto grande, `kill -9`, esperas, compilador, servidor de lenguaje y Ollama) y
  `make test-fast`, que las deja fuera. El servidor falso es uno por sesión: cada prueba lo
  encuentra sin fallos y con los contadores a cero (`POST /reset`).
- `docs/contribuir.md`: ramas (gitflow), mensajes de commit, cómo pasar las pruebas y la
  lista de cada versión. El README dice cuántas pruebas hay (249 sin modelo) y cuánto tardan.
- `examples/walkthrough.py` y `make demo CONFIG=atlas.test.toml`: las acciones principales
  (`check`, `sections`, dos `run`, `status`, `graph`, `trace`, `matrix`, `search`, `ask` y
  el asistente si está el extra `[agents]`) con la configuración dada, cada una con su salida
  y su tiempo, y una tabla al final. Arranca el servidor falso si la configuración lo pide
  (`model = "fake"`) y no responde nadie; sin modelo, solo lo que no lo necesita. Es el
  guion del curso y la prueba de humo de un puesto nuevo (`--id`, `--module`, `--search`,
  `--ask`, `--skip`, `--lines`).
- Una prueba comprueba que cada acción de la ayuda de `atlas` aparece en `docs/uso.md` o en
  el README.
- `docs/uso.md`: la guía del ingeniero, por tareas (qué pide un requisito, qué rompo si
  cambio algo, qué falta por trazar, la matriz para una revisión, código para un requisito
  desde el editor, preguntas concretas y amplias): la acción o la herramienta, cómo leer su
  salida (`values_differ`, `cannot_tell`, «Nothing indexed...», `not_advanced`) y lo que
  puede y no puede un modelo pequeño.
- `docs/aceptacion.md`: el guion de las dos primeras semanas con el proyecto real (un puesto
  Windows limpio, el gateway por el proxy, DOORS, los PDF y el código sin modelo, la ingesta
  de noche, 20 preguntas con `atlas eval` y el servidor en el editor), con una casilla y una
  cifra por paso, y dónde van las de las metas 1 a 7 en `legacy/PLAN.md`.
- `docs/faq.md`: los errores conocidos y su remedio (certificados, proxy, cupo, el
  razonamiento de gemma 4, el 501 del esquema JSON, `OLLAMA_CONTEXT_LENGTH`, las rutas
  relativas, «does not exist» en las fuentes y el entorno del servidor MCP).
- `make wheelhouse` y `make wheelhouse-windows` aceptan `EXTRAS` (`all` por defecto):
  `EXTRAS=all,dev` mete pytest para pasar `tests/test_security.py` en el puesto.
- `tools/licenses.py` y `make licenses`: `dist/THIRD_PARTY_NOTICES.md` (nombre, versión,
  licencia y URL de cada dependencia de `atlaslm[all]`, agrupadas por licencia y con las AGPL
  y GPL aparte) y `dist/sbom.cdx.json` (SBOM CycloneDX 1.5 con el purl y la licencia de cada
  paquete), desde el entorno instalado; avisa de lo instalado en otra versión que la de
  `constraints.txt`. `make wheelhouse` y `make wheelhouse-windows` dejan los dos ficheros en
  el wheelhouse, sacados de sus propios wheels (así cuentan también los paquetes que solo se
  instalan en Windows). Hoy salen dos con GPL: pymupdf (AGPL-3.0) y docutils, que llega por
  `tree-sitter-ada` → `twine` → `readme-renderer`.
- `make audit`: `pip-audit` sobre las versiones de `constraints.txt` de todos los Python
  sin instalarlas (`--no-deps`, un fichero por versión de un mismo paquete); necesita red para
  consultar las vulnerabilidades y tarda menos de un minuto. `pip-audit` entra en el extra `dev`.
- `atlas plugins` dice de cada extractor de documentos sus extensiones, su licencia y si
  está desactivado. Los plugins `DocumentExtractor` tienen un atributo `license`.
- `docs/seguridad.md` («pymupdf y la AGPL-3.0»): qué supone la AGPL para una herramienta
  interna, la alternativa MIT (el extractor `markitdown`) y dos opciones: pymupdf por defecto
  con la nota, o un extra `docs-pymupdf` aparte del `docs`. La elección queda pendiente de la
  política de la empresa; mientras, sigue pymupdf por defecto.
- README, «Antes de publicar una versión»: pruebas, `make audit`, `make licenses`, wheelhouse y
  `make check-install`.
- PDF escaneados: `atlas sections`, `preview` y `process_document` cuentan las páginas con
  imágenes y sin texto (`pages_without_text`). Si son más de la mitad, el documento no se
  procesa y su unidad queda en error, reintentable, con «scanned PDF: N of M pages have no
  text; nothing is sent to the model», visible en `atlas status`. Antes quedaba «ok» con 0
  secciones útiles.
- `[[sources.docs]] ocr_command` (`["ocrmypdf", "--skip-text", "{file}", "{out}"]`): los PDF
  con páginas sin texto pasan por OCR sobre una copia, guardada en `<workspace>/docs/.ocr` por
  el hash del PDF y de la orden.
- Versión, fecha y autoridad de los documentos: `[[sources.docs]] doc_id_regex`,
  `version_regex` y `date_regex` se buscan en las cabeceras repetidas, las dos primeras
  páginas y los metadatos del PDF (de donde sale también la fecha si no hay otra), y van a
  `structure.json` y al nodo `document`. Las secciones de `trace`, `trace_code` e `impact`
  llevan `version` y `page_start`, y la columna `documents` de `matrix` dice
  `spec.pdf v2 §3.1 Timing p.12`.
- `[[sources.docs]] authority` (1 = normativo): un requisito que no está en DOORS se compara
  con la declaración del documento de mayor autoridad (`values_differ` con `against`), y
  `gaps` añade `documents_disagree`: los requisitos fuera de DOORS que varios documentos
  declaran con cifras distintas. Dos documentos con el mismo `doc_id` y versión menor dan un
  aviso en `atlas graph`.
- Tablas de los PDF: `[[sources.docs]] tables = true` (apagado por defecto) pone cada tabla
  que encuentra pymupdf (`page.find_tables`) como filas Markdown, una por línea, en vez de una
  celda por párrafo. `table_requirements = { id = "ID", text = "Requirement" }` saca un
  requisito por fila, sin modelo con `analysis = "regex"` y como complemento de `"llm"`.
- `[index] kinds` tiene `table`: cada tabla, con la ruta de su sección y su pie («Table n» o
  «Tabla n»), es un registro propio y sale de los fragmentos. `chunk_text` no parte una tabla
  por dentro de una fila y repite la cabecera en cada parte; `reflow` conserva el salto antes
  de las viñetas, los pasos numerados y las filas de tabla.
- El `embed_text` de un requisito de documento lleva la ruta de su sección.
- `DocumentExtractor.read(path)`: el texto y lo que el extractor sabe del fichero (páginas,
  páginas sin texto, metadatos del PDF). Un extractor de un plugin puede seguir implementando
  solo `extract`.
- `[sources.code] project = "tms.gpr"` y `[sources.code.project_vars]`: el proyecto GPR del
  código Ada da las raíces (las carpetas de fuentes suyas y de los proyectos que importa, con
  `**`), añade a `exclude` las de objetos, ejecutables y bibliotecas, y, si no se dan, los
  comandos de gprbuild (`gprbuild -P {project} -X... -c -u -f {file}`, `-gnatc` para una
  especificación sola, y el de las pruebas). El servidor de lenguaje abre ese proyecto con sus
  variables en vez del mínimo que escribía atlas. El lector (`atlas/code/gpr.py`) es propio y
  sin dependencias, y avisa con fichero y línea de lo que no entiende. `{project}` vale en
  cualquier comando.
- `atlas plugins` dice qué raíces de código hay (y si salen del proyecto), qué se excluye y
  cuántos ficheros `.ads` y `.adb` ven.
- `examples/samples/ada/samples.gpr` (`make_samples.py`), con una variable de escenario.
- `tools/reqif_import/reqif_to_exchange.py`: la base de intercambio desde ficheros ReqIF
  (`.reqif` y `.reqifz`), el segundo camino junto a `tools/doors_export`, en cualquier equipo y
  sin dependencias (`xml.etree`). `SPECIFICATION` → módulo, `SPEC-HIERARCHY` → padre, orden,
  nivel y numeración, `SPEC-OBJECT` → requisito, `SPEC-RELATION` → enlace del tipo de su
  `SPEC-RELATION-TYPE`, con el `mapping.toml` de `publish.py` (`[fields]`, `[attributes]`,
  `[modules]`, `[link_types]`, `[kinds]`; `Object Text` encuentra también `ReqIF.Text`) y sus
  avisos: IDs repetidos o sin ID, enlaces a objetos de ningún fichero y, con `--id-pattern`,
  IDs fuera de la convención. Varios ficheros se leen juntos, así que los enlaces entre sus
  módulos se resuelven. `make_samples.py` escribe `examples/samples/requirements.reqif`, que da
  las mismas filas que `requirements.sqlite`.

### Cambiado
- Un paso de `atlas develop` cuya ejecución falla (el servidor del modelo) queda como `error`,
  no como `kept`; el resumen final cuenta los errores.
- La salida estructurada va por escalones: `json_schema`, `json_object` y ninguno. Antes, un
  servidor que rechazaba el esquema se quedaba sin `response_format`, cuando casi todos
  aceptan `json_object`. El escalón se aprende por servidor y modelo para todo el proceso
  (antes, por instancia: cada llamada MCP de `verify` o `link` pagaba otra petición
  rechazada), y `atlas check` lo dice: `structured output: json_schema | json_object | none`.
- `ask_json` comprueba la respuesta contra su esquema (tipos, claves obligatorias, listas y
  valores permitidos) y la corrección cita el primer error («requirements[2].text must be a
  string»), en vez de pedir JSON sin más.
- Las fuentes de una respuesta de pasajes son solo las que cita; las demás van a `unused`.
- `atlas ask` sale con código 1 cuando no hay nada indexado relacionado con la pregunta.
- El `atlas.toml` de producción del repositorio sigue la plantilla del gateway: pone
  `reasoning_effort = "none"`, `max_concurrent`, una `[[sources.docs]]` y `[search]`, y lee la
  clave de `ATLAS_API_KEY`, como la plantilla y la guía de instalación (antes, `GW_KEY`).
- `atlas trace ID` imprime en Markdown todo lo del requisito (`build_context`, sin modelo y sin
  el extra `[agents]`): texto y atributos de DOORS, enlaces, las secciones que lo declaran con
  las cifras que difieren de DOORS, el código, las pruebas, los llamadores y el código fuente.
- La comparación de cifras entre DOORS y los documentos (`atlas/graph/values.py`): cantidades
  con su unidad canónica (ms, s, min, h, m, km, Hz, kHz, %, B, KB, MB), separador de miles
  solo con tres cifras detrás, comparación numérica, números escritos con letras, y fuera las
  referencias («section 3.2», «Table 3»...), las fechas y las versiones. `values_differ`
  dice además qué clase de diferencia es: `value`, `unit_only` (300 ms frente a 0,3 s: no se
  convierte) o `format_only` («1,000» y «1000»). `gaps` y `trace` solo listan `value`; el
  verificador recibe también `unit_only`. Antes, 6 de 7 casos reales daban un veredicto
  erróneo.
- Los requisitos de un módulo de DOORS se indexan en el orden del módulo (el de la base de
  intercambio), no en el de sus IDs. Las muestras tienen una nota tras SRS-0004.
- Con `embeddinggemma`, el prefijo de documento lleva el título del documento (`title: HMI
  Manual | text: …`) en vez de `title: none`, como recomienda su ficha. En las muestras, la híbrida
  pasa del 75 % al 80 % de acierto@1 (MRR de 0,83 a 0,88).
- `[sources.doors] attributes` tiene efecto: son los atributos de DOORS que atlas enseña y por
  los que filtra, `["status", "verification"]` por defecto. Cada uno que tenga un requisito va
  en su texto del índice (`status: Approved; verification: Test`), así que pesa en la búsqueda
  y el modelo lo ve con el requisito, y se puede pedir con `attribute`. No elige qué se
  importa: eso siguen haciéndolo `attribute_columns` y el relleno de la base de intercambio.
  Cambiar la lista solo reindexa los módulos de DOORS, sin volver a leerlos.
- `atlas search` sangra todas las líneas de cada resultado.
- Una pregunta amplia que no nombra ningún documento elige los que lee buscando solo en los
  resúmenes de los documentos y de sus secciones, no en los 8 pasajes de su búsqueda. Un
  módulo de DOORS, que no tiene resúmenes, solo se lee entero si la pregunta lo nombra (o con
  `--document`), salvo que ningún documento tenga resúmenes.
- Cada fuente de `Index.answer` trae `context`, el texto que leyó el modelo; `ask_docs` no
  lo devuelve al agente (tiene `excerpt` y `get_section`).
- Los vectores de las consultas de búsqueda quedan en la caché de vectores del índice: la
  misma consulta otra vez no pide nada a los embeddings (antes, cada búsqueda híbrida la
  embebía; una pasada de `retrieval.yaml` eran 20 peticiones, y la segunda ya ninguna).
- Los avisos del limitador de peticiones (rechazos por cupo, reintentos) son avisos del
  registro `atlas.llm` en vez de `print`: `[project] log_level` los gobierna y quedan en el
  fichero del registro.
- La fila de una ejecución de `atlas run` en la tabla `runs` se crea al empezar; una
  ejecución interrumpida deja su fila sin segundos y `atlas status` no la enseña.
- `examples/agents/implementer.md` es el medido: `replace_subprogram`, `add_subprogram`,
  `library_spec`, `search_reference`, `find_definition`, `describe_symbol` y `run_program`,
  `stall_steps` 15 y `keep_tool_results` 10, sin `reasoning_effort` en el fichero. El de la
  fase 5 (con `edit_file` y `write_file` solamente) se quita: con él fallaba el 53 % de las
  ediciones de `gemma-4-26b`, con el medido el 11 %.
- `atlas.llm` ya no termina el proceso: un certificado de CA que no existe, `ca_cert =
  "system"` sin `truststore` o el cliente `openai` sin instalar lanzan `ConfigError` con el
  mismo texto. En `atlas run` un `ConfigError` para la ingesta (antes cada unidad), y el
  servidor MCP sigue vivo.
- Los avisos del limitador (rechazos por límite, reintentos) van al logger `atlas.llm` en vez
  de escribirse en stderr: los gobierna `[project] log_level`.

### Corregido
- Las instrucciones de `atlas serve` no explicaban nunca `check_code`, `run_test` ni
  `library_spec`, aunque `check_code` se ofrece por defecto con un compilador.
- El párrafo del grafo de las instrucciones de `atlas serve` era fijo y, con el perfil
  `editor`, hablaba al modelo del cliente de `get_node`, `callers`, `callees`, `matrix` y
  `gaps`, que no puede llamar. Ahora se construye con las herramientas ofrecidas: el flujo
  habitual solo tiene los pasos ofrecidos (`find_symbol -> trace_code / impact ->
  read_source` en `editor`) y cada frase de `trace`, `trace_code`, `impact`, `matrix` y
  `gaps` solo está si se ofrece su herramienta.
- Un comando de `check_code`, `run_test` o `run_program` al que se le acaba el tiempo se mata
  con todo lo que arrancó (su grupo de procesos; `taskkill /T` en Windows). Antes se mataba
  solo el comando, y un envoltorio como `with_server` dejaba vivo el servidor.
- `run_test` encuentra el ejecutable con `.exe` que GNAT deja en Windows.
- Las rondas de `atlas develop` no usaban `attempts`: ahora van, como los encargos, a esos
  intentos a la vez.
- Ctrl+C a medio encargo de `atlas develop` dejaba los ficheros a medias: ahora vuelven a como
  estaban antes del paso, que queda `interrupted` en el informe.
- Todas las rutas relativas de `atlas.toml` lo son a su carpeta, se lance atlas desde donde se
  lance. Antes solo `workspace`, `[agents] dirs`, `[agents.eval] sets`, `hints` y
  `language_server_project`; las de las fuentes (`[[sources.docs]] paths`, `[sources.code]
  roots`, las bases de `[sources.doors]`), `tool_path`, `library`, `reference`,
  `[tests.languages]` y las de los planes de `atlas develop` dependían de la carpeta desde la
  que se lanzaba: un editor que arrancaba `atlas serve` en otra carpeta no encontraba nada, y
  `atlas run` daba 0 ficheros y borraba lo procesado. Las herramientas de ficheros, el grafo y
  los intentos de `atlas develop` también parten de la carpeta de `atlas.toml`.
- Una respuesta de texto cortada por `max_tokens` (o vacía, con un modelo que razona) llegaba
  al usuario sin aviso. Ahora se pregunta otra vez con el doble de presupuesto y, si sigue
  cortada, la respuesta de `atlas ask` y `ask_docs` lleva `truncated` y `atlas ask` lo avisa.
- Una pregunta amplia leída por partes perdía la respuesta entera si fallaba una sola parte.
  Ahora esa parte deja la nota «part i of n failed: …» al final de la respuesta (y en
  `failed_parts`) y la respuesta se escribe con las demás, con sus fuentes y peticiones bien
  contadas.
- Una vuelta colgada de un agente dejaba la petición viva en el gateway y su hueco de
  `max_concurrent` ocupado: se abandonaba en un hilo y se preguntaba otra vez, así que podía
  haber tres peticiones vivas por vuelta. Ahora se corta su conexión. `JsonLlm` no tenía tope
  por vuelta (el `timeout` de httpx es entre dos lecturas: una petición se quedó colgada una
  hora); ahora tiene el mismo que los agentes.
- El hueco de `max_concurrent` se soltaba al llegar las cabeceras de la respuesta, no al
  acabar de leerla: una respuesta en streaming no contaba. Ahora lo ocupa hasta el último trozo.
- Con Python 3.13, un certificado sin Authority Key Identifier, como los de algunos proxies que
  inspeccionan TLS, ya no se rechaza cuando `ca_cert` no es `"system"`: 3.13 enciende
  `VERIFY_X509_STRICT` y atlas la quita en su contexto propio (la cadena y el nombre se siguen
  verificando, como en 3.12). Si el error sale, `atlas check` recomienda `ca_cert = "system"`
  o Python 3.12 en vez de pedir la CA corporativa.
- Una raíz de `[sources.code] roots` que no existe avisa («does not exist»), como las rutas de
  `[[sources.docs]]`; antes daba 0 ficheros sin decir nada.
- Un procedimiento de prueba de DOORS (`STP-0101 -verifies-> SRS-0004`) salía en la columna
  de pruebas de la matriz como si fuera una prueba del código, en `derived` como si fuera
  un requisito derivado, y `impact` del SRS lo subía como requisito derivado. Las pruebas
  del código son ahora solo los nodos `test`, y la derivación (`derived`, la cobertura a
  través de los derivados y las subidas de `impact`) solo sigue los tipos de `derives`.
- Las referencias (`references`) entre módulos ya no cuentan como derivación.
- Dos subprogramas con el mismo nombre en un paquete (sobrecargas) eran un nodo con las
  líneas y los requisitos del último. Ahora el nodo junta los requisitos y las llamadas de
  todos, va de la primera línea del primero a la última del último y lleva `bodies` con las
  líneas de cada uno; `read_source` los lee todos y el índice tiene el código de todos. Los
  identificadores no cambian.
- `T'Image (X)`, `T'Write (S, X)` y los demás atributos ya no cuentan `T` como una llamada.
- Un nombre que declara la propia unidad (un tipo, una variable) va a ese elemento aunque
  otra unidad tenga un subprograma con el mismo nombre; antes ganaba el subprograma de fuera.
- Una pregunta amplia leída por partes perdía la respuesta entera si fallaba una sola parte.
  Ahora esa parte deja la nota «part i of n failed: …» al final de la respuesta (y en
  `failed_parts`) y la respuesta se escribe con las demás.
- En Windows, con la salida en una consola `cp1252` o redirigida a un fichero, un texto con
  `→` o `≤` (un requisito, un título) terminaba la acción con `UnicodeEncodeError`: la línea
  de comandos escribe siempre en UTF-8. Para los scripts que no pasan por ella, la guía de
  instalación añade `setx PYTHONUTF8 1`.
- Los Markdown y el `structure.json` de cada documento, y lo que se escribe con
  `atomic_write`, llevan `\n` también en Windows (antes `\r\n`): un workspace copiado entre
  sistemas tiene los mismos bytes.

### Quitado
- `[sources.code] summaries`, que se aceptaba sin efecto desde la 0.1.0.
- El modo independiente de `atlas.llm.connection` (`python -m atlas.llm.connection init |
  show | check`, con su propio `connection.toml`): nadie lo usaba y `legacy/conexion.py` lo
  conserva. `atlas check` hace lo mismo con `atlas.toml`.

### Al actualizar
- Con `[sources.code]`, `atlas serve` ofrece ahora el perfil `editor`: ya no están `callers`,
  `callees`, `matrix`, `gaps`, `ask_docs` y las demás. `[mcp] profile = "all"` vuelve a las
  de antes; una lista en `[mcp] tools` sigue mandando.
- `tool_path` relativo pasa a ser relativo a `atlas.toml`, no a donde se lanza atlas (como
  decía la 0.2.0). Las rutas de las herramientas de ficheros (`read_file`, `list_files`...)
  parten de la carpeta de `atlas.toml`: en el editor, igual que antes si arranca el servidor
  en la carpeta del proyecto. El `cwd` del cliente MCP ya no es obligatorio, aunque sigue
  siendo lo recomendable: `atlas.toml` se busca desde ahí si no se da su ruta completa.
- El motor nombra cada fichero de las fuentes por su ruta relativa a la carpeta de
  `atlas.toml` (absoluta si está fuera de ella): los mismos nombres que antes cuando atlas se
  lanzaba desde esa carpeta, así que un workspace así no reprocesa nada. Se reprocesan una
  vez (y se borra lo anterior) los ficheros de una ruta escrita como absoluta pero dentro de
  la carpeta de `atlas.toml`, los de una ruta relativa que sale de ella (`../docs`), y todo
  lo de un workspace que se llenaba lanzando atlas desde otra carpeta.
- `[network] timeout` ya no acota la vuelta de un agente: eso lo hace `turn_timeout` (600 s por
  defecto, como antes). Si subiste `timeout` para vueltas largas, sube `turn_timeout`; y si el
  servidor hace esperar más de 600 s hasta el primer trozo (cola, prompt largo), sube
  `turn_timeout`. Una vuelta de `JsonLlm` que agota sus tres intentos para `atlas run` como
  antes lo hacía un `APITimeoutError`.
- Las peticiones de chat van con streaming salvo que el servidor no pueda. Si con el gateway
  algo cambia, `[llm] stream = false` deja todo como antes; `atlas probe` dice si lo admite.
- Un `atlas.toml` copiado del ejemplo de producción lee la clave de `GW_KEY`: cámbialo a
  `${ATLAS_API_KEY}` (y la variable), o deja `GW_KEY` puesta. Si no tiene `reasoning_effort`,
  `atlas check` lo avisa; ponerlo no reprocesa nada.
- El CSV de `atlas matrix` va en UTF-8 con BOM y con `;` entre columnas (`[graph]
  csv_delimiter`), para que Excel en español lo abra en columnas y con los acentos bien. Las
  listas de una celda van una por línea en vez de separadas por «; ». Un script que lea el
  CSV necesita `encoding="utf-8-sig"` y `delimiter=";"`, o `csv_delimiter = ","`.
- `atlas trace` ya no imprime JSON sino Markdown. Un script que leía su salida necesita
  `atlas trace ID --json`, que da el mismo diccionario de antes.
- Vuelve a generar las muestras (`python examples/make_samples.py`) para tener el módulo
  `STP_TMS`; las pruebas no lo necesitan.
- La forma de `matrix` cambia: cada fila tiene `procedures`, el resumen `with_procedures` y
  los huecos `without_test_procedure`; el CSV de `atlas matrix` gana la columna
  `procedures` antes de `documents`. `trace` tiene `procedures` y `ancestors`, y
  `verified_by` ya no incluye los procedimientos de DOORS. `gaps` tiene
  `requirements_without_test_procedure`, `requirements_without_derivation` y
  `linked_to_modules_not_read`. Pon los módulos de sistema en `top_modules`, o
  `requirements_without_derivation` los listará enteros.
- Si el proyecto usa en DOORS tipos de enlace que no son derivación ni verificación
  (`traces`, `relates_to`...), ponlos en `link_semantics.ignore`, o da la lista de
  `derives`. El siguiente `atlas run` reconstruye la parte de DOORS del grafo.
- `[sources.code] summaries` ya no existe: una configuración que la tenga deja de cargar
  (`unknown key: 'summaries'`). Basta con borrar la línea.
- El CSV de `atlas matrix` tiene una columna más al final, `exported_at`.
- `values_differ` (en `trace`, `gaps` y la evidencia de `atlas verify`) pasa de
  `{"doors", "document"}` con números sueltos (`"320"`) a `{"doors", "document", "kind"}` con
  las cantidades como las escribe cada texto (`"320 ms"`). Quien lea esos campos debe mirar
  `kind`. Como cambia el prompt, `atlas verify` vuelve a preguntar una vez por los requisitos
  con cifras distintas.
- Con el adaptador `exchange`, cada módulo de DOORS se vuelve a leer una vez (su huella
  lleva ahora la fecha de la exportación) y la parte `doors` del grafo se reconstruye. No
  hay peticiones al modelo ni a los embeddings.
- El código se vuelve a analizar entero (`ada-3`, `python-2`) y el grafo se reconstruye
  (`graph-4`), sin modelo. Pueden aparecer subprogramas nuevos (tareas, entradas,
  operadores) con sus requisitos, desaparecer llamadas falsas a tipos con atributos y
  dejar de ser ambiguas las llamadas que un `use` o un alias deciden.
  `index.code` vuelve a embeber el código.
- Con `id_pattern` (o `id_regex`) en `[sources.doors.convention]`, el código y los documentos
  con `analysis = "regex"` se vuelven a analizar una vez, sin modelo, y las etiquetas dan los
  IDs que antes se perdían: pueden aparecer requisitos implementados nuevos y, en `gaps`,
  IDs citados que no están en DOORS. El grafo se reconstruye entero.
- Con `embeddinggemma`, el primer `atlas run` vuelve a embeber todos los textos del índice (el
  prefijo de documento cambia y es parte de la clave de la caché). Con otros modelos, se
  reindexa desde la caché, sin peticiones.
- El índice se reconstruye una vez (`index-2`): la siguiente `atlas run` reescribe todos sus
  registros. Solo se embeben de nuevo los requisitos de DOORS con atributos, cuyo texto
  cambia; lo demás sale de la caché de vectores. Un `atlas.db` anterior gana la columna `meta`
  al abrirse, y sus registros no casan con ningún filtro hasta esa ejecución.
- Los módulos de DOORS se vuelven a leer una vez: su huella ya no incluye `attributes`.
- `attributes` estaba «reservada: todavía sin efecto». Si alguien la puso como lista de lo que
  se importa, ahora decide lo que se enseña y por lo que se filtra; con `[]`, nada.
- La caché de embeddings de `atlas.db` gana la columna `model`. Los vectores de un workspace de
  la 0.2.0 no la tienen: `atlas cache export` se la pone a los de los textos del índice; los
  demás (textos que ya no están en el índice) no se exportan. No se reprocesa nada.
- Con `requests_per_minute` o `tokens_per_minute`, atlas escribe en `~/.atlas/limits` (o
  `%LOCALAPPDATA%\atlas\limits`). Dos procesos contra el mismo gateway ya no suman sus límites:
  si alguien subió `requests_per_minute` contando con eso, que lo baje al del gateway.
  `shared_limit = false` vuelve a lo de antes.
- La primera orden que abre un workspace de la 0.2.0 o anterior copia `atlas.db` en
  `atlas.db.bak-0` (y `reference.db` en `reference.db.bak-0`) y le pone la versión 2: la 1
  añade los segundos de cada unidad al libro de registro y la 2 el `meta` de los registros del
  índice y el modelo de cada vector de la caché. Los datos no cambian; el índice se rehace en el
  siguiente `atlas run` (`INDEX_VERSION`). La copia ocupa lo mismo que `atlas.db`: se puede
  borrar cuando todo funcione. Ver «Al actualizar» en `docs/instalacion.md`.
- Con la 0.2.0 o anterior instalada en otro puesto, ese puesto sigue abriendo el workspace
  (no mira la versión); las versiones siguientes ya no abren uno de un atlas más nuevo.
- `constraints.txt` fija `pip-audit` y sus dependencias, añadidas a mano desde Python 3.13
  (`platformdirs` solo desde 3.11: en 3.10 lo resuelve pip). La próxima vez que se genere
  con `tools/constraints.py` saldrán de las instalaciones limpias de cada Python.
- Todos los documentos se vuelven a procesar una vez (`docs-2`), para que `structure.json`
  tenga `doc_id`, `version`, `date` y las páginas sin texto. El texto de las secciones no
  cambia: el análisis sale de la caché y no se pregunta nada al modelo.
- El índice se reconstruye una vez (`index-2`): cambian los fragmentos con listas o pasos
  numerados (que ahora conservan sus líneas) y el texto que se embebe de cada requisito.
  Los textos que no cambian salen de la caché de vectores; el resto se embebe otra vez.
- `[index] kinds` tiene `table` por defecto. Si tu `atlas.toml` da su propia lista, añade
  `"table"` para que las tablas tengan registro propio.
- Encender `tables` en una fuente vuelve a procesar sus documentos, y como el texto de las
  secciones con tablas cambia, la caché de análisis no les sirve: con `analysis = "llm"`, esas
  secciones vuelven al modelo una vez. Las demás salen de la caché.
- Las cadenas de la columna `documents` de `matrix` y de `gaps` llevan versión y página
  (`spec.pdf §1 Tracks p.1`); las secciones de `trace`, `trace_code` e `impact` tienen dos
  claves más (`version`, `page_start`).

## [0.2.0] — 2026-10-16

Desde la 0.1.0: agentes que escriben código y lo prueban (`atlas develop`, `atlas tests`), el
código en el índice, Python, OpenRouter para comparar modelos, `tools/doors_export` contra un
DOORS real, y preguntas amplias que leen documentos enteros.

### Añadido
- Preguntas amplias en `atlas ask` y `ask_docs`: al reescribir la pregunta, el modelo dice si
  necesita mucho de un documento (un resumen, una función entera, todos los requisitos de
  algo) y de cuáles trata. Entonces se leen enteros, sección a sección: en una petición si
  caben en `[search] scope_chars` y, si no, por partes cuyas notas junta una última petición.
  Antes llegaban al modelo 8 pasajes: en las muestras, 8 de los 40 requisitos de SRS_TMS.
  `--whole` (o `whole` en `ask_docs`) los lee enteros siempre.
- `[search]`: `results` (cuántos resultados, 8), `per_document`, `context_chars`, `scope` y
  `scope_chars`. Cambiarlo no reprocesa nada.
- `atlas search` y `atlas ask` aceptan `--k`, `--kind` y `--document`, como la herramienta
  `search`.
- El asistente tiene `list_documents`, `list_requirements` y `ask_docs` para las preguntas
  amplias.
- `atlas probe`: si el modelo de chat funciona como agente, con tres peticiones pequeñas (una
  llamada a una herramienta, su resultado y si el servidor aprovecha el razonamiento devuelto).
  Para comprobar el gateway antes de usar agentes.
- `top_p` y `top_k` por agente, en su fichero o en `[agents.<nombre>]`. La ficha de gemma 4
  recomienda temperatura 1,0, `top_p` 0,95 y `top_k` 64, y no todos los proveedores usan esos
  valores por defecto (EmpirioLabs documenta `top_k` 20). `top_k` va en `extra_body`.
- `replace_subprogram` y `add_subprogram`: cambiar o añadir un subprograma entero, por su
  nombre, sin copiar nada del fichero (Ada con tree-sitter y Python). En la tanda 4 de
  `atlas develop`, 80 de las 151 ediciones de `gemma-4-26b` fallaron: 39 querían sustituir más
  de 30 líneas y 34 no copiaron bien el texto. `atlas serve` solo las ofrece si se piden.
- Las herramientas que escriben comprueban la sintaxis antes de escribir y compilan después:
  un cambio que rompe la sintaxis de un fichero que estaba bien no se hace (`[sources.code]
  syntax_commands`, con GNAT `gcc -c -gnats`; Python con `ast`), y cada cambio devuelve lo que
  dice `check_code` del fichero (`check_on_edit`, por defecto).
- Pistas para los errores del compilador: cada error lleva la `hint` que casa con su mensaje.
  Las de serie (`atlas/code/hints.toml`) cubren los errores de GNAT más repetidos por
  `gemma-4-26b`; las del proyecto van en `[sources.code] hints`. El informe de `atlas develop`
  lista los errores de la tanda y dice cuáles no tenían pista, y cuenta por ejecución las
  ediciones fallidas y las compilaciones con errores.
- `library_spec` y `[sources.code] library`: las declaraciones públicas reales de un paquete del
  runtime del compilador (la carpeta `adainclude` de GNAT) o del proyecto, leídas con
  tree-sitter. Un error `"X" not declared in "P"` trae lo que declara `P` parecido a `X`, y uno
  `"X" is undefined` las unidades que declaran `X`.
- `search_reference`, `[sources.code] reference` y `atlas reference`: los manuales (los de
  GNAT vienen en formato info con Alire; también Markdown, texto y HTML) y las declaraciones de
  la biblioteca, por palabras y sin modelo, en su propio índice (`<workspace>/reference.db`).
- `keep_tool_results` por agente: los resultados grandes de herramientas de antes de los últimos
  quedan en una nota, y una lectura de un fichero que se volvió a leer se quita. En la tanda 4,
  las vueltas de un arreglo llevaban 55 000 tokens de entrada, casi todo lecturas viejas.
- `stall_steps` por agente y `restarts` en el plan de `atlas develop`: una ejecución que lleva
  esas llamadas sin cambiar el código (o que agota `max_steps`) se para y vuelve a empezar con
  una conversación nueva, el diff de lo hecho y lo que dice `check_code`. En la tanda 4, las 6
  ejecuciones que agotaron sus 40 llamadas fueron el 41 % del tiempo.
- `attempts` en el plan de `atlas develop`: varios intentos de cada encargo a la vez, cada uno en
  una copia de la carpeta de trabajo; gana el que no rompe nada y deja más tests pasando, y sus
  ficheros pasan a la carpeta de trabajo.
- `planner` en el plan de `atlas develop`: un agente de solo lectura dice cómo hacer cada
  encargo, y el implementador lo recibe con ese plan (el arquitecto y el editor de Aider).
- `find_definition`, `find_references` y `describe_symbol` (`[sources.code] language_server`):
  lo que sabe el compilador de un nombre, de un servidor de lenguaje. Probado con el Ada Language
  Server de la extensión de VS Code, con un proyecto GPR que atlas escribe.
- `atlas develop PLAN`: un agente escribe el código encargo a encargo y los tests de aceptación
  lo juzgan. Tras cada encargo, `check_code` y los tests; un encargo que deja errores o hace que
  un test falle antes se deshace (los ficheros vuelven a como estaban) y se intenta otra vez
  con lo que rompió. Después, rondas sobre los tests que fallan, con su salida y el código que
  citan; un test que no avanza en `stuck_rounds` rondas se deja para una persona. Informe JSON
  y Markdown tras cada paso, y las llamadas de cada ejecución en `.calls.jsonl`. Sale del
  experimento de 2026-10-13 en `atlas-socket-demo`, donde ningún modelo sin ayuda llegó a un
  servidor que funcionara. Cada encargo puede decir qué tests hace avanzar (`advances`); si
  no avanzan, vuelve al agente con la salida del test (`advance_fixes`) y, si aun así no,
  el informe lo dice. Con `gemma-4-26b`, desde `src` vacío: los 18 encargos y 2 rondas
  dieron un servidor que pasa los 5 STP (los 10 tests, Ada y Python), sin ayuda humana en el
  código, en 23 ejecuciones y 0,61 $; sobre el código de devstral que se caía, 2 rondas; con
  `advances`, los 5 STP al acabar los encargos, sin rondas, en 28 ejecuciones y 0,72 $.
- `atlas tests MÓDULO|ID` y `[tests]`: los procedimientos de prueba como tests en cada lenguaje,
  paso a paso, con patrones y plantillas del proyecto y sin modelo. Un paso que ningún patrón
  reconoce se dice y el procedimiento no se escribe.
- `[sources.code] protected`: ficheros que los agentes leen pero no cambian (los tests). Los
  agentes de los dos modelos probados, al pedirles arreglar un test, lo adaptaron al código.
- La herramienta `run_program` (`[sources.code] program_command`): ejecuta el programa con las
  líneas que le da el agente, para ver qué hace el código con una entrada.
- `run_test` y `run_program` devuelven en `code_at` las líneas alrededor de los sitios del
  código que cita su salida (el de una excepción).
- `[sources.code] check_commands`: el comando de `check_code` por extensión (GNAT, generando
  código en los cuerpos y con `-gnatc` en las especificaciones).
- `run_agent` devuelve los tokens y, con OpenRouter, el coste de todas las vueltas, y con
  `record` apunta cada llamada a una herramienta con su resultado y su tiempo.
- `tools/doors_export`: `[kinds] others` en `mapping.toml`, el tipo de los objetos cuyo valor
  del atributo de `[kinds]` no está en `[kinds.values]`. Con `"heading_or_information"`, un
  atributo que solo dice si el objeto es requisito (`is_requisite`, «Yes» o «No») decide los
  requisitos y deja al contenido si el resto son títulos o información: atlas no pierde el
  capítulo de cada requisito. Además, los valores no distinguen mayúsculas ni espacios de más,
  un módulo sin el atributo se clasifica por contenido (antes, todo era requisito), y
  `publish.py` cuenta los objetos de cada tipo y dice qué valores no estaban en `[kinds.values]`.
- `tools/doors_export`: `[fields] id` admite una lista, y vale el primer ID que tenga cada
  objeto; y `module_number`, el nombre del módulo y el Absolute Number (`SRS_TMS-123`). Con
  `["unique-identifier", "module_number"]`, los requisitos se publican con su nombre, y los
  títulos y la información, que no lo tienen, con el otro ID: antes se quedaban fuera y atlas
  perdía el capítulo de cada requisito. `publish.py` avisa de los requisitos sin el primero, y
  atlas solo avisa de los ID fuera de `id_pattern` en los requisitos.
- `tools/doors_export`: `publish.py` lista los ID repetidos, con el Absolute Number del objeto
  publicado y del que se omite, y los módulos sin descargar a los que apuntan los enlaces que no
  se publican. El resumen separa esos enlaces de los que van a objetos sin ID o borrados.
- `atlas eval` mide también la recuperación, sin modelo: casos `search` (acierto@1, @3, @5 y
  @10 y rango recíproco medio, con la búsqueda híbrida y con la léxica) y casos con una
  consulta del grafo (`trace`, `trace_code`, `impact`, `find_symbol`, `search_code_docs`).
  `examples/eval/retrieval.yaml` tiene 30 casos sobre las muestras; con `embeddinggemma`, la
  híbrida acierta a la primera en el 75 % y entre las tres primeras en el 90 %, y el grafo,
  10 de 10. Cierra la meta 7 del plan.
- El código se indexa: paso `index.code`, una unidad por unidad de código (en Ada, el
  paquete con su especificación y su cuerpo), y un registro `code_doc` por subprograma,
  prueba, tipo, variable y constante, con su comentario de cabecera, los requisitos que cita
  y su código. `search` y `atlas ask` encuentran código; la herramienta `search` acepta
  `kind = "code_doc"`. En el proyecto grande añade 1,3 s y 48 peticiones de embeddings.
- `Index.search(..., dense=False)`: solo IDs y BM25, sin ninguna petición.

- `atlas link ID|MÓDULO` y el agente `req_linker`: para cada requisito sin código, el código
  que puede implementarlo, con confianza (alta, media, baja) y motivo. Los candidatos salen sin
  modelo (el código que mencionan sus secciones, el de los requisitos enlazados y la búsqueda
  en el código indexado), y el agente solo elige entre ellos. El informe
  (`reports/link-<objetivo>.md`) dice qué etiqueta `Req:` añadir y dónde; no se escribe nada
  en el código. También como herramienta `link_requirement` de `atlas serve`, si se pide.

- `check_code` y `run_test` (`atlas.code.BuildTools`): el compilador y las pruebas del
  proyecto para los agentes. `check_code` compila un fichero sin generar código y devuelve los
  errores y avisos con su sitio; `run_test` construye un procedimiento de prueba y lo ejecuta.
  Los comandos van en `[sources.code] check_command` y `test_command`, como listas de
  argumentos, con `tool_path` y `command_timeout`. `atlas.dev.toml` los configura para GNAT
  instalado con Alire. `run_test` ejecuta código y solo se ofrece si se pide. El mismo error
  en varias líneas se da una vez, con sus líneas, y las pistas del compilador en el mismo
  sitio van con él: para el test que escribió `gemma4:12b` para SRS-0160, 2 entradas en vez
  de 18 mensajes.
- `examples/agents/`: dos agentes de proyecto que escriben, `implementer` y `tester`. Las
  instrucciones de `tester` traen las reglas de Ada en las que falló `gemma4:12b` y un modelo
  de prueba que compila.
- `atlas comply ID|MÓDULO` y el agente `compliance_checker`: si el código que implementa cada
  requisito hace todo lo que pide, obligación a obligación (`met`, `not_met`, `partial`,
  `cannot_tell`), con las líneas que lo deciden. El veredicto sale de las obligaciones, no
  del modelo. Con `gemma4:12b` encontró un error de límite (`< 300.0` donde el requisito dice
  «300 inclusive») en 3 de 3 ejecuciones. También como herramienta `check_compliance`, si se
  pide. Es una ayuda para la inspección, no una prueba.
- `atlas.openrouter.toml`: los modelos de chat de OpenRouter sobre las muestras, con los
  embeddings en Ollama. El modelo va en `ATLAS_MODEL` (por defecto, `google/gemma-4-26b-a4b-it`,
  el del gateway) y cada uno tiene su workspace. `make check-openrouter`, `eval-openrouter` y
  `compare-openrouter`; este último usa `tools/compare_models.py`, que pasa `atlas run` y
  `atlas eval` con cada modelo y termina con una tabla. Guía en `docs/openrouter.md`.
- `[llm] extra_body`: campos que van tal cual en cada petición de chat, para las opciones
  propias del servidor (`provider` en OpenRouter, `chat_template_kwargs` en vLLM).
- Analizador de Python (`[sources.code] languages = ["ada", "python"]`), con `ast` y sin
  dependencias: funciones, métodos, clases, `import` y etiquetas `# Req:`. Un módulo se nombra
  por su ruta desde la raíz de código, así que una prueba Python y una Ada del mismo nombre son
  dos nodos que verifican el mismo requisito.
- `run_test` con lo que la prueba necesita en marcha: `[sources.code] run_command` dice cómo se
  ejecuta lo construido (un envoltorio que arranca un servidor, por ejemplo) y `test_commands`
  ejecuta tal cual, por extensión, las pruebas que no se construyen (`py = ["python3", "{file}"]`).
- Con esto, los agentes de AtlasLM y `gemma-4-26b` escribieron un proyecto de prueba completo:
  un servidor de comandos Ada por sockets (544 líneas), a partir de 25 requisitos de software, y
  sus diez pruebas en Ada y en Python, con trazabilidad de 25 de 25 requisitos. Lo que hizo falta
  para los agentes está en los cambios siguientes.

### Cambiado
- Los agentes devuelven al modelo el razonamiento de sus vueltas con llamadas a herramientas,
  en el mensaje del asistente (`[llm] reasoning_field`, `"reasoning"` por defecto; `""` como
  antes). Lo pide la ficha de gemma 4; antes se tiraba y el modelo tenía que deducir en cada
  vuelta qué estaba haciendo. Con OpenRouter, el prompt de `gemma-4-26b` lo incluye.
- `edit_file` rechaza un texto buscado de más de 30 líneas y pide varias ediciones pequeñas o
  `write_file`: `gemma-4-26b` reconstruía de memoria los textos largos y entraba en bucles de
  leer y editar de 27 y 45 minutos.
- La misma llamada a una herramienta con los mismos argumentos, la tercera vez seguida, no se
  ejecuta: el agente recibe un error que le pide otra cosa.
- `edit_file` tolera también el listado de `read_file` copiado en el texto: quita los números
  de línea, las barras antes de cada salto de línea y los `\n` escritos como texto, en el
  texto buscado y en el nuevo. Con `gemma4:12b`, un cambio de varias líneas pasó de 0 de 2 a
  2 de 2.
- Los PDF se extraen con pymupdf, y markitdown (pdfminer) queda como alternativa
  (`[plugins] disabled = ["extractor:pymupdf"]`). Es unas 30 veces más rápido (1,5 ms por
  página en vez de 45: la primera ejecución del proyecto grande baja de 17,2 a 6,1 s) y el
  texto sigue el orden en que se escribió, así que el número de un título no se separa de
  él. En la muestra sin marcadores, pdfminer perdía 18 de las 27 subsecciones y sus
  requisitos quedaban en la sección anterior.
- Cuando varios extractores leen la misma extensión, se usa el de mayor `priority`
  (atributo nuevo de `DocumentExtractor`).
- La huella de `docs.process` incluye el extractor, y `atlas sections` dice cuál se usa.

### Corregido
- La búsqueda da como mucho 3 resultados de cada documento o unidad de código mientras otros
  tengan (`[search] per_document`). Cada elemento de una unidad repite su nombre, y una
  unidad cuyo nombre estaba en la pregunta se llevaba todos los puestos: «how are tracks
  deleted» daba 8 elementos de `tracks` y ningún requisito ni documento.
- `atlas serve` sin el proxy del entorno: el cliente MCP arranca el servidor con su entorno, no
  con el de la terminal (los hechos con la librería de MCP, como Claude Desktop o ADK, solo
  pasan `PATH` y pocas más), y `${HTTPS_PROXY:-}` quedaba vacío sin decir nada. Al arrancar,
  el servidor escribe en su registro cómo llega al modelo (con el proxy o sin él) y qué
  variables de `atlas.toml` no tiene; las herramientas que usan el modelo y `atlas check` las
  nombran en su error. La guía de instalación explica cómo pasarlas a cada cliente.
- `tools/doors_export`: el módulo de enlaces de cada enlace (de donde sale su tipo) ya no se
  pide con `module(l)`, que contra un DOORS real no compila (`incorrect arguments for (=)` en
  `--check`). Se listan los módulos de enlaces del proyecto y cada objeto recorre sus enlaces
  por cada uno de ellos, además de por `"*"`; `--check` prueba esas dos primitivas.
- `tools/doors_export`: la descarga no compilaba en DOORS (`incorrect concatenated token`): el
  número absoluto se convertía con `(int)(o."Absolute Number")`, un molde que DXL no tiene.
  Ahora se asigna a un entero. `--check` ejecuta además una página de la descarga con un objeto.
- Una vuelta de un agente tiene como mucho `[llm.network] timeout` segundos en total y, si
  tarda más, se pregunta otra vez. El tiempo máximo del cliente HTTP es entre dos lecturas, y
  OpenRouter mantiene viva la conexión mientras el modelo trabaja: una petición de
  `gemma-4-26b` se quedó colgada una hora con `atlas develop` esperando.
- Una vuelta de un agente que gasta todo `max_tokens` razonando, sin texto ni llamadas, se
  repite con el doble (como mucho dos veces); antes el agente acababa en silencio.
- `edit_file`, cuando no encuentra el texto, dice si el cambio ya está hecho y enseña el bloque
  del fichero más parecido; `write_file` avisa si el contenido no cambia. Las barras al final de
  cada línea, copiadas del listado de `read_file`, se quitan también del texto nuevo.
- Una carpeta relativa de `[sources.code] tool_path` es relativa a donde se lanza atlas; antes
  se buscaba desde `<workspace>/build`.
- Un `X-RateLimit-Reset` en milisegundos, como el de OpenRouter, se leía como segundos: cada
  429 paraba todas las peticiones 5 minutos.
- Un 402 (sin crédito, o con el límite de gasto de la clave agotado) para el motor en vez de
  fallar unidad a unidad, y el diagnóstico del 402, del 404 y del 429 dice lo que respondió
  el servidor (en OpenRouter, también el proveedor del modelo).

### Al actualizar
- `edit_file`, `write_file` y las herramientas de subprogramas ya no escriben un cambio que
  rompe la sintaxis de un fichero Python que estaba bien (también desde `atlas serve`, si se
  ofrecen); `[sources.code.syntax_commands] py = []` lo apaga. Con un compilador configurado,
  sus respuestas traen `check`.
- El informe `reports/eval-<conjunto>.json` agrupa los resultados en `agents` y `retrieval`.
- `code_doc` ya estaba en `[index] kinds` por defecto: la siguiente ejecución embebe todo el
  código una vez. Para no indexarlo, quítalo de `kinds`. El código compite con los documentos
  en la búsqueda: en las muestras, una búsqueda de documentos bajó de la primera a la cuarta
  posición; `kind` en `search` busca en una sola clase de texto.
- Todos los documentos se vuelven a procesar una vez. El texto de cada sección de un PDF
  cambia (saltos de línea, posición de las cabeceras), así que la caché de análisis no sirve
  y, con `analysis = "llm"`, todas las secciones de los PDF vuelven al modelo. Los Markdown
  salen de la caché.
- pymupdf tiene licencia AGPL-3.0 (o comercial de Artifex). Ya era una dependencia del extra
  `[docs]`, para los marcadores; conviene confirmar que la política de la empresa la admite.

## [0.1.0] — 2026-10-11

Primera versión para el repositorio interno: las fases 0 a 6 del plan (`legacy/PLAN.md`).

### Fase 6: consolidación

**Rendimiento**
- Perfil de tiempo de cada ejecución, guardado en `atlas.db` con la versión. Por paso:
  segundos (y cuántos se van en buscar unidades y en preparar), unidades, las más lentas y
  lo que se pidió a las API: peticiones, rechazos, reintentos, espera del limitador y del
  servidor. `atlas status` lo muestra.
- `[pipeline.step_workers]`: unidades a la vez por paso (`docs`, `code`, `index`).
- `docs.process` procesa varios documentos a la vez, y las secciones de todos comparten
  `workers` peticiones al modelo. Con un modelo simulado de 1 s por petición y 4 hilos,
  un 15 % menos en 6 PDF (50,6 → 43,2 s).
- Lotes de embeddings llenos entre documentos y entre módulos de DOORS, varios a la vez:
  189 peticiones en vez de 207 en el proyecto grande.

**Empaquetado**
- Versión 0.1.0, dependencias con rangos compatibles y ADK fijado a 2.11. `truststore`
  pasa a ser dependencia (`ca_cert = "system"`).
- `constraints.txt` con las versiones exactas probadas en Python 3.10 a 3.13.
- `make wheel`, `wheelhouse`, `wheelhouse-windows` y `check-install` (instalación limpia
  sin red, como dice la guía).
- `atlas init` escribe una plantilla comentada de `atlas.toml`; `atlas --version`.

**Seguridad**
- Los secretos solo vienen del entorno: no carga una configuración con un `api_key` para
  un servidor que no es local, o con una contraseña en la URL del proxy o en el DSN de
  ODBC, escritos tal cual.
- Las huellas de configuración no incluyen secretos.
- `atlas check` enmascara también las credenciales de `base_url`.
- Prueba de red: una ejecución completa, búsquedas, respuestas y un agente ADK solo se
  conectan a la API configurada.

**Documentación**
- [Instalación](docs/instalacion.md), [configuración](docs/configuracion.md) (cada clave,
  comprobado por las pruebas) y [seguridad](docs/seguridad.md).
- Un README por módulo, y el README raíz como índice.
- Ejemplos de uso como librería en `examples/library/`, que las pruebas ejecutan.

**Arreglos**
- La caché de análisis de secciones se escribe de forma atómica; un fichero dañado por un
  corte se vuelve a analizar en vez de dejar la sección en error.
- Ctrl+C con unidades en varios hilos ya no espera a las que no han empezado, y el
  bloqueo del workspace no se suelta mientras alguna sigue escribiendo.

### Fases 0 a 5

- **Fase 0, cimientos.** Configuración validada con `${VAR}` y huellas por sección; motor
  incremental (`Step`, `Ledger`, `Runner`) con reanudación, cuarentena, poda y bloqueo del
  workspace; sistema de plugins; cliente de los modelos.
- **Fase 1, migración.** Los scripts de `legacy/` como módulos: `atlas.docs`
  (`pdf_struct.py`), `atlas.index` (`doc_rag.py`), `atlas.code` y `atlas.graph`
  (`ada_graph.py`), y un solo servidor MCP (`atlas serve`) con las herramientas de los dos
  anteriores y las de ficheros. Mismos resultados, salvo las diferencias buscadas.
- **Fase 2, motor incremental completo.** Todos los pasos en el motor; una segunda
  ejecución sin cambios no hace ninguna petición, y un PDF cambiado en una página solo
  vuelve al modelo con esa sección.
- **Fase 3, DOORS.** La base de intercambio de requisitos y el exportador de DOORS
  (`tools/doors_export`); los requisitos, también en el índice.
- **Fase 4, grafo unificado.** Código, DOORS y documentos en un grafo; `trace`,
  `trace_code`, `impact`, `matrix`, `gaps`, `neighbors` y comunidades, sin modelo; cifras
  que difieren entre DOORS y los documentos.
- **Fase 5, agentes.** Agentes ADK definidos en ficheros Markdown, `AtlasLlm`,
  `build_context`, verificación de requisitos con la documentación, informes de impacto y
  cobertura, `ask_atlas` y evaluación (`atlas eval`: 23 de 24 con `gemma4:12b`).

### Al actualizar

Desde un workspace de una versión anterior (sin número):
- `atlas.db` gana la columna `seconds` del libro de registro y la tabla `runs`; se añaden
  solas al abrirla.
- Si el DSN de `[sources.doors.database]` lleva contraseña, los módulos de DOORS se leen
  una vez más (su huella ya no la incluye). Nada más se reprocesa.
- Una configuración con una clave o una contraseña escrita tal cual deja de cargar: hay
  que pasarla a `${VARIABLE}` (el error dice dónde).

### Pendiente

- Instalar y repetir la prueba de red en un puesto Windows.
- Las metas medibles de la sección 1 del plan sobre el proyecto real.
- Validar en DOORS real la DXL de jerarquía y enlaces, y la descarga de un módulo grande.
- `[sources.code] summaries` y `[sources.doors] attributes` se aceptan pero no tienen
  efecto, y el código no se indexa (`code_doc`).
