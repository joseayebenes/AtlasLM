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
  `check_code`, 11 o 12 en vez de 27 con documentos y código. `trace` da las de trazabilidad
  de DOORS y los documentos, y `all` (por defecto sin código), todas las de las fuentes, como
  antes.
- Cada herramienta del servidor MCP lleva un título (`Trace code`), y las que solo leen el
  workspace y el código, sin el modelo, se anotan como idempotentes y sin salida al exterior
  (`idempotentHint`, `openWorldHint` falso): el cliente puede repetirlas sin preguntar.

### Al actualizar
- Con `[sources.code]`, `atlas serve` ofrece ahora el perfil `editor`: ya no están `callers`,
  `callees`, `matrix`, `gaps`, `ask_docs` y las demás. `[mcp] profile = "all"` vuelve a las
  de antes; una lista en `[mcp] tools` sigue mandando.

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
