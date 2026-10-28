# Análisis de mejoras de atlas

Sobre la versión 0.2.0 (`main`, etiquetada el 16 de octubre de 2026, el día en que se hizo
este análisis); las referencias `fichero:línea` son de esa versión, en la que las 242
pruebas sin modelo pasan en 35 s. La sección 2 explica cómo se hizo y qué no cubre.

## 1. Resumen

atlas está donde el plan decía que estaría al acabar la fase 6, y algo más: el motor
incremental cumple las siete metas medibles con proyectos sintéticos, el grafo y el índice
responden sin modelo en milisegundos, los agentes de serie aciertan 23 de 24 preguntas con
`gemma4:12b`, y `atlas develop` ha pasado en tres días de «ningún modelo llega a un servidor
que funcione» a un servidor que pasa los 5 STP con el 11 % de ediciones fallidas. La base es
sólida y está medida; casi ninguna de las mejoras de abajo es un arreglo de algo roto.

Lo que separa la 0.2.0 de una herramienta en uso diario está en cinco sitios:

1. **El conocimiento no llega bien al editor.** Todo lo que atlas sabe se pide por
   identificador (`tracks.delete_track`), y el agente del editor tiene un fichero y una
   línea. Tras la primera edición, el grafo que atlas le enseña ya no es el código que tiene
   delante, y nada lo avisa. Las instrucciones del servidor nunca le dicen que compile ni
   cómo se etiqueta un requisito.
2. **`atlas develop` no sobrevive a un corte ni se mide dentro del repositorio.** Una tanda
   de cinco horas se pierde con un Ctrl+C, un plan con el nombre de un agente mal escrito
   recorre todos los encargos sin llamar al modelo y sin avisar, y los agentes con los que
   se midió el 11 % viven en otro repositorio. Y el juez determinista que hizo funcionar a
   gemma (tests protegidos, deshacer, `advances`) solo sirve a los agentes propios de ADK,
   no al agente del editor.
3. **La aceptación en el proyecto real sigue pendiente**, y lo que la va a frenar es
   conocido: ningún puesto Windows ha ejecutado la suite, la primera ingesta son horas que
   nadie estima, la matriz se abre en una columna en el Excel español, nadie enseña de qué
   exportación de DOORS sale cada dato, y un procedimiento de prueba de DOORS aparece en la
   matriz como si fuera una prueba de código.
4. **El gobierno de lo que sale al gateway** no está resuelto: no hay forma de marcar un
   documento que no puede ir al gateway (hoy todo se embebe y una pregunta amplia lo lee
   entero), ni registro de lo enviado, y la licencia AGPL del extractor de PDF sigue como
   «conviene confirmar». Y «procesar una vez» se cumple por workspace, no por equipo: cada
   puesto repite la ingesta de horas.
5. **La infraestructura del propio repositorio** va por detrás del código: sin CI, sin
   linter ni tipado (8 errores de ruff, 239 de mypy), sin WAL en `atlas.db` (un `atlas run`
   bloquea las lecturas de `atlas serve`), con rutas relativas a la carpeta de lanzamiento y
   sin versión de esquema.

Las catorce mejoras de más valor, por orden de prioridad y con el esfuerzo (S: horas, M:
días, L: semanas):

| # | Mejora | Área | Esf. |
|---|---|---|---|
| 1 | Contexto por fichero y línea (`src/tracks.adb:42`) en `build_context`, `trace_code`, `impact` y `read_source` | MCP | S |
| 2 | Instrucciones del servidor: emitir el grupo «build», añadir el de LSP y la convención `Req:` del proyecto | MCP | S |
| 3 | Grafo fresco tras editar: `stale` en las respuestas y herramienta `refresh` | MCP | M |
| 4 | `atlas develop --resume`, Ctrl+C limpio y parche por encargo | develop | M |
| 5 | Validar el plan al arrancar (`agent`, `fix_agent`, tests protegidos) y presupuesto por encargo | develop | S |
| 6 | Encargos anclados a requisitos: contexto del grafo, `advances` automáticos, etiquetas comprobadas | develop | M |
| 7 | Los agentes medidos (`implementer`, `fixer`, `planner`) dentro del repositorio y un banco de encargos en `atlas eval` | develop | M |
| 8 | Semántica de los enlaces de DOORS: los procedimientos de prueba no son pruebas de código | grafo | M |
| 9 | Un enrutador determinista: una pregunta de trazabilidad con ID se responde desde el grafo | índice | M |
| 10 | Fecha de la exportación de DOORS visible en `trace`, `matrix`, `status` y los informes | grafo | S |
| 11 | CI con Windows, `ruff` y `mypy`; WAL en `atlas.db`; rutas relativas al TOML | arquitectura | M |
| 12 | `atlas run --estimate` y presupuesto de tiempo o peticiones por ejecución | modelo | M |
| 13 | Cachés compartidas entre puestos: la ingesta de horas la paga un solo puesto | pipeline | M |
| 14 | Fuentes que no salen al gateway (`model = "none"` por fuente) y registro de lo enviado | seguridad | M |

Las secciones 3 y 4 (contexto para MCP y `atlas develop`) son las que pediste; van primero y
con más detalle. Las demás áreas están en las secciones 5 a 9; lo que ningún análisis
cubría, en la 10; las decisiones que conviene tomar antes de empezar, en la 11; la hoja de
ruta, en la 12, y lo descartado, en la 13.

## 2. Cómo se ha hecho

Siete análisis independientes, uno por área (servidor MCP y contexto; agentes de código y
`atlas develop`; índice y respuestas; grafo, documentos y DOORS; capa de modelo;
arquitectura y proceso; adopción y aceptación), cada uno leyendo el código, los README, el
CHANGELOG y el plan, y devolviendo 12 hallazgos con evidencia `fichero:línea`. Cada
hallazgo pasó después por dos verificaciones independientes: una que comprueba que el
problema existe hoy en el código y que la evidencia y las cifras son correctas (con sondas
ejecutadas donde hacía falta: tree-sitter, FTS5, `_values`, `extract_requirements`, el
grafo con 20 000 requisitos en memoria, `MCPServer.add_tool`), y otra que juzga si la
propuesta encaja con los principios del plan y si vale lo que cuesta. De 84 hallazgos, 83
sobreviven, casi todos con la propuesta corregida por los verificadores; esas correcciones
están incorporadas aquí. Uno se descarta (sección 13). Un último análisis, con todos los
hallazgos delante, buscó lo que nadie había propuesto (sección 10), los duplicados (ya
fundidos aquí) y las contradicciones entre propuestas (sección 11). Yo comprobé además a
mano las afirmaciones que sostienen las catorce de arriba.

Lo que este análisis no cubre: no se ha ejecutado nada contra el gateway ni contra el
proyecto real, así que las cifras son las de los README (muestras sintéticas, `gemma4:12b`
en local y `gemma-4-26b` en OpenRouter), y los esfuerzos son estimaciones.

## 3. Extracción de contexto para clientes MCP

**Estado.** `atlas serve` es una capa fina y bien asentada: un servidor stdio que expone
como herramientas los mismos plugins `tool` que usan los agentes ADK (45 registradas, 27
por defecto con documentos y código), con las de escritura y las que ejecutan o llaman al
modelo solo bajo petición, anotaciones `readOnly`/`destructive`, instrucciones por grupo, y
una prueba de extremo a extremo con un cliente MCP real que incluye un `atlas run` con el
servidor levantado. Las consultas del grafo son deterministas y de milisegundos, los errores
vuelven como respuestas con `error` y sugerencias, y `build_context` da en una llamada un
paquete de 4000 tokens. Con `gemma4:12b` sin razonamiento, el agente por MCP acertó 12 de
12 preguntas.

Lo que falta se ve desde el editor, no desde atlas. El agente de OpenCode o VS Code tiene
abierto un fichero, un cursor y un diff; atlas le pide identificadores. Por orden de
prioridad:

### 3.1 Contexto por fichero y línea (S, alta)

**Problema.** `build_context`, `trace_code`, `impact`, `read_source` y `get_node` toman un
identificador `unit.name` o un nombre simple ([knowledge.py:95-108](../atlas/graph/knowledge.py)
`resolve()`). Para pedir contexto sobre la línea 42 de `src/tracks.adb` el agente tiene que
adivinar el subprograma, llamar a `find_symbol` y después a `build_context`: dos o tres
vueltas, cada una de unos 30 s con el gateway. Las únicas herramientas por línea son las del
servidor de lenguaje, y exigen además el nombre. El dato ya está en el grafo: cada nodo
`subprogram`/`test` guarda `file`, `start_line` y `end_line` ([build.py:107-110](../atlas/graph/build.py)).

**Propuesta.** `KnowledgeGraph.at(file, line)`: normaliza la ruta como `CodeTools._resolve`
(absoluta o relativa a la raíz, insensible a mayúsculas en Windows), devuelve el
subprograma o prueba cuyo rango contiene la línea, si no el tipo, variable o constante con
esa `line` (solo guardan una línea), y si no la unidad del fichero. `resolve()` acepta la
forma `ruta:línea` cuando lo que sigue al último `:` es un entero (para que `C:\src\x.adb:42`
funcione), con lo que las cinco herramientas la admiten sin añadir ninguna. Un alias
explícito `context_at(path, line)` en `AgentTools`, con una descripción que diga «para el
fichero y la línea que tienes abiertos», ayuda al modelo pequeño a elegirla. Para no
depender de líneas desfasadas tras una edición, el subprograma en la línea se localiza en
el fichero actual con `symbols.subprograms` (ya existe, parsea el texto real) y después se
casa su nombre cualificado con el nodo del grafo. Es la misma consulta que `outline` y
`read_subprogram` de 4.4, ofrecida a dos clientes.

**Medir.** Hoy `build_context('src/tracks.adb:12')` devuelve «is not in the graph»; después,
`procedure Delete_Track` en el Markdown. Repetir las 4 preguntas × 3 del test MCP con
Ollama contando llamadas por respuesta.

### 3.2 Instrucciones del servidor: el grupo «build» no se emite nunca (S, alta)

**Problema.** `instructions()` recorre los grupos `docs, graph, context, agents, files,
edit` y omite `build` ([mcp/__init__.py:138](../atlas/mcp/__init__.py)), así que el texto
que dice «compila con `check_code` después de cada cambio» no llega a ningún cliente,
aunque `check_code` se ofrezca por defecto en cuanto hay `check_command`. `run_program`,
`search_reference` y las tres herramientas del servidor de lenguaje no pertenecen a ningún
grupo. Y nada cuenta al editor cómo se etiqueta un requisito en este proyecto (`-- Req:`),
qué forma tienen los IDs ni qué módulos de DOORS hay.

**Propuesta.** Corregir el bucle, añadir un grupo `lsp` y meter `run_program` y
`search_reference` en `build`. Construir desde `cfg` un párrafo «Traceability of this
project» con `req_tag`, `id_pattern`, los módulos (`Store.modules()`) y el flujo fijo de un
cambio: contexto → editar → `check_code` → `run_test` de las pruebas que da `trace`. Una
prueba que, para cada herramienta de `plugins.available('tool')`, compruebe que las
instrucciones la nombran a ella o a su grupo (habría cazado esto).

### 3.3 El grafo queda viejo tras cada edición (M, alta)

**Problema.** El editor (o `edit_file`/`replace_subprogram` por MCP) cambia ficheros; el
grafo, las líneas de sus nodos y el índice de código siguen siendo los del último `atlas
run`. `after_change` solo compila ([build.py:290-301](../hephestolm/hephesto/build.py)),
`GraphTools.graph()` recarga solo cuando cambia `graph_builds`
([graph/tools.py:67-74](../atlas/graph/tools.py)) y `_lines` lee el fichero actual con
rangos antiguos. Tras añadir un subprograma con su `Req:`, `callers`, `impact` y
`trace_code` no lo ven, `read_source` devuelve líneas desplazadas y ninguna respuesta
avisa. Es el núcleo de «el editor edita y atlas aporta conocimiento»: tras la primera
edición, el conocimiento es falso.

**Propuesta.** Tres piezas en la librería. (0) Sacar la construcción del `Runner` de
`cli.run` a una función `runner(ctx, only=None)` (ver 9.3) y dar al contexto del servidor
un `stopping`. (1) `stale` en toda respuesta que cite código: comparar el `mtime` del fichero
con el `built_at` de `graph_builds['code']` y listar los ficheros más nuevos. (2)
Herramienta `refresh(paths=[])` en `GraphTools` que ejecute `code.parse` y `graph.build`
sobre el mismo contexto (sin `index.code`, que pide embeddings) y devuelva las unidades
reanalizadas, los segundos y `index_stale`. (3) Opcional, `[mcp] refresh_on_write`: las
herramientas de escritura lo hacen solas tras cada cambio. Coste: décimas de segundo por
fichero en Ada. Condición (sección 11): la reconstrucción pasa por el `Runner` con el
bloqueo del workspace, y cede si un `atlas run` lo tiene; si no, el servidor solo marca
`stale`.

### 3.4 Ejemplos del propio proyecto: requisitos parecidos ya implementados (S, alta)

**Problema.** Para implementar un requisito nuevo, el agente necesita ver cómo se
implementaron los parecidos en este proyecto: en qué paquete, con qué estilo, dónde va la
etiqueta. `build_context` de un requisito sin código no trae ningún ejemplo
([context.py:107-128](../atlas/agents/context.py)), y `link_requirement` necesita el modelo y
solo trata requisitos sin código. En la tanda 4, unos 25 de 100 errores de compilación
eran nombres inventados.

**Propuesta.** Bloque «Similar requirements already implemented» en `_requirement_blocks` y
herramienta `similar_requirements(req_id, k=3)`: candidatos deterministas, primero los
requisitos enlazados por DOORS y los que citan las mismas secciones (reutilizando las
fuentes sin modelo de `atlas.agents.link.candidates`), después `Index.search(texto,
kinds=['requirement'], dense=False)` si hay índice; filtrados a los que tienen código. Para
cada uno, su texto, la firma del subprograma, la línea de la etiqueta `Req:` y, solo del
primero, las 20 primeras líneas del cuerpo. Dentro del presupuesto de `build_context`, como
el primer bloque que se recorta. Es lo mismo que 4.3 antepone a cada encargo de `develop`.

### 3.5 Impacto de un conjunto de ficheros cambiados y qué tests correr (M, media)

**Problema.** `impact` toma un nombre y `run_test` un fichero. Antes de un commit, el agente
quiere saber qué requisitos y qué tests toca lo que ha cambiado y qué ejecutar; hoy tendría
que cruzar listas a mano, que un modelo pequeño no hace de forma fiable.

**Propuesta.** Primera versión sin parsear diffs (el editor ya sabe qué ficheros tocó):
`changes(paths)` en `GraphTools`: por fichero, sus elementos del grafo; la unión de `impact`
sin repetir; `tests_to_run` agrupados por fichero de prueba, listos para encadenar
`run_test`; `untagged` (elementos cambiados sin requisito) y `requirements_without_tests`
entre los afectados; y `stale` si existe. Esto cubre también «qué requisitos toca este
fichero», que pedía el análisis del grafo. En `atlas develop`, el mismo cálculo elegiría
qué tests correr tras cada encargo en vez de todos los del plan.

### 3.6 `build_context` con foco y con menos texto (S, media)

**Problema.** Los bloques van en orden fijo y, al agotar el presupuesto, el bloque en curso
se corta por líneas y los siguientes se omiten ([context.py:180-201](../atlas/agents/context.py)).
Para una unidad grande, «Spec» son 200 líneas de la `.ads`. Con 16k de contexto en local,
cada token del paquete compite con el fichero abierto.

**Propuesta.** Lo barato primero: recortar elementos enteros de menor interés antes que
cortar un bloque a mitad; `detail='signatures'` sin bloques `Source` ni `Spec`; y la `Spec`
de una unidad a partir de `Library.spec(unit)` (las declaraciones públicas que ya extrae
`library_spec`) en vez de las 200 primeras líneas. Después, `focus=''`: ordenar los
elementos de cada bloque por solapamiento de términos con la tarea, sin modelo. `omitted`
pasa a decir cuántos elementos quedaron fuera por bloque.

### 3.7 Perfiles de herramientas y anotaciones (S, media)

**Problema.** 45 herramientas registradas y 27 por defecto, frente a las «5-8 bien
descritas» del principio 6 del plan. Para cambiar código con trazabilidad hacen falta una
docena; `graph_stats`, `communities`, `matrix`, `type_users` o `call_chain` son para
informes. Las anotaciones solo llevan `readOnly`/`destructive` cuando `mcp` 2.3 admite
`title`, `idempotent` y `open_world`. Las 27 descripciones suman unos 1300 tokens por
`list_tools` (no es mucho, pero lo reenvían algunos clientes en cada vuelta). Y este
informe propone una docena de herramientas nuevas: sin perfiles, se contradice a sí mismo.

**Propuesta.** `[mcp] profile = "editor" | "trace" | "all"` (por defecto `editor` cuando hay
código): `editor` = `search`, `get_requirement`, `get_section`, `trace`, `trace_code`,
`impact`, `build_context`/`context_at`, `changes`, `refresh`, `find_symbol`, `read_source`,
`read_file`, `check_code`; `trace` = las de trazabilidad sin código; `all` = lo de hoy.
`[mcp] tools` sigue mandando si se da. Pasar `title` a `add_tool` y anotar `idempotent` y
`open_world=false` en todo lo que no escribe ni ejecuta. No tocar los docstrings: son
también la descripción de los agentes ADK. Regla para lo que sigue: toda herramienta nueva
nace dentro de un perfil o sustituye a otra.

### 3.8 Forma de las salidas (M, media)

**Problema.** Todas las herramientas devuelven `dict` sin tipos, y en `mcp` 2.3 eso se
serializa como un único `TextContent` con JSON: el Markdown de `build_context`, el código
de `read_source` y los diffs llegan con los saltos escapados, que un modelo pequeño lee
peor y copia mal (comprobado con una sonda sobre `MCPServer.add_tool`). Un `{error}` llega
con `isError` falso. Los tamaños son por herramienta y grandes para 16k de contexto
(`get_section` hasta 20 000 caracteres; `read_file` 300 líneas) y solo `matrix` pagina.

**Propuesta.** Un adaptador en `create_server` (solo MCP; ADK sigue recibiendo dicts) que
envuelva `tool.function()` con `functools.wraps`: si el dict tiene un único campo largo de
texto (`markdown`, `source`, `diff`, `mermaid`), ese texto va como `TextContent` y el dict
entero como `structured_content`; `{error}` → `is_error=True` conservando el JSON (para que
el modelo vea `similar`/`sections` y se corrija). `[mcp] max_chars` (12 000) que recorta los
campos de texto con `truncated`, y `offset` en `trace`, `search`, `list_requirements`,
`grep` y `list_files` con `next_offset`.

### 3.9 Recursos y prompts MCP (M, media)

**Problema.** El servidor solo registra herramientas. Un usuario de VS Code no puede
adjuntar un requisito o una sección como recurso sin gastar una vuelta del modelo, ni
invocar «implementa SRS-0004» como un comando. Los flujos fijos del plan viven en `atlas
develop`, no en el editor.

**Propuesta.** Sin lógica nueva: plantillas de recurso `atlas://requirement/{id}`
(`get_requirement` como Markdown), `atlas://document/{name}/section/{id}` (el fichero que
`get_section` ya localiza) y `atlas://context/{target}` (`build_context`); el listado de
recursos solo con documentos y módulos (enumerar 5000 requisitos hace inusable
`list_resources`). Prompts `implement_requirement(req_id)` (contexto + instrucciones de
trazabilidad + «compila y ejecuta los tests que da `trace`») y `explain(target)`. Se
registran solo cuando su herramienta está entre las ofrecidas, porque el esquema de `[mcp]`
rechaza claves desconocidas. El valor depende del cliente: OpenCode y VS Code son los
documentados; conviene comprobar qué hace cada uno con recursos antes de invertir.

### 3.10 Herramientas lentas: progreso y `cached` (M, baja)

`ask_atlas`, `verify_requirement`, `link_requirement` y `check_compliance` tardan de 35 a
50 s con `gemma4:12b` y el cliente no ve nada (algunos cortan a los 60 s). `verify`,
`comply` y `link` ya guardan sus juicios en caché, así que la segunda llamada es
instantánea; lo que falta es decirlo (`cached: true`). El progreso se puede emitir con
`ctx.report_progress` desde una envoltura `async` que ejecute `run_agent` en un hilo y
pase un `record` que notifique cada llamada; solo llega si el cliente pidió
`progressToken`. Una caché de `ask_atlas` aporta poco (las preguntas no se repiten
literalmente).

### 3.11 Transporte: documentar la decisión y medir antes (baja)

Un proceso stdio por cliente es el diseño, y conviene dejarlo escrito en `docs/seguridad.md`
junto con por qué un servidor de equipo en la intranet queda fuera (sin autenticación
corporativa ni TLS, y `trace`/`impact`/`read_file` hablarían del código del servidor, no del
editado en cada puesto). Lo que sí se puede compartir es el workspace procesado en un
recurso de red de solo lectura, y las cachés (10.1). Un servidor local por HTTP para varios
editores del mismo puesto solo tiene sentido si se mide que tres `atlas serve` sobre
`examples/large` triplican una memoria que duela; mientras, la garantía «no abre ningún
puerto» se mantiene.

### 3.12 Pruebas del servidor (S, media)

`tests/test_mcp.py` tiene 4 pruebas y un flujo lineal. Faltan: grupos e instrucciones para
cada herramienta (3.2), anotaciones coherentes con `Tool.writes`, dos llamadas concurrentes
con el servidor falso lento (`trace` responde antes que `ask_docs`), una llamada a `callers`
mientras `atlas run` escribe (ver WAL, 9.4), y el presupuesto de las salidas (3.8).

### 3.13 Lo que otras áreas aportan al editor

- Un enrutador determinista en `ask_docs` para preguntas de trazabilidad con ID (5.1).
- `trace.ancestors` (la cadena hasta el requisito de sistema) y `gaps` con huérfanos (6.5).
- WAL en `atlas.db`, para que un `atlas run` no bloquee al servidor (9.4), y el cupo
  compartido entre `atlas serve` y `atlas run` (7.3).
- `status` explícito en cada respuesta (`answered`, `partial`, `not_found`) y citas
  comprobadas contra el texto (5.7 y 8.7).
- Un registro de uso del servidor: qué herramientas llama el editor, cuánto tardan y qué
  preguntas no tienen respuesta (8.8).
- El juez de `atlas develop` como herramientas MCP de solo lectura (`run_tests`,
  `judge_changes`), para que el agente del editor también sepa qué rompió (10.3).

## 4. Agentes de código y `atlas develop`

**Estado.** `atlas develop` tiene ya las piezas que los experimentos del 13 al 15 de octubre
demostraron necesarias: encargos que se deshacen si rompen algo, `advances` que devuelve
el encargo al agente con la salida del test, rondas con escalado a una persona, reinicios
limpios por `stall_steps`, intentos en paralelo por encargo, planificador de solo lectura;
y en `atlas.code`, subprogramas enteros, sintaxis antes de escribir y compilación tras cada
cambio, pistas, `library_spec`, `search_reference` y el servidor de lenguaje. Con
`gemma-4-26b` las ediciones fallidas bajaron del 53 % al 11 %. El informe JSON/Markdown y
el `.calls.jsonl` dan datos reales de cada tanda, y `tests/test_develop.py` cubre el bucle
sin modelo. El enrutamiento de modelos por papel ya es posible (campo `model` por fichero
de agente). Lo que cojea:

### 4.1 Reanudar una tanda interrumpida y confirmar cada encargo (M, alta)

**Problema.** Una tanda dura horas (5 h y media la tanda 3; 4 h 52 min la tanda 4).
`run_plan` borra el `.calls.jsonl` y empieza desde el primer encargo
([develop.py:619-621](../hephestolm/hephesto/develop.py)); no hay `--resume`. El informe JSON sí
se escribe tras cada paso, pero nadie lo lee al relanzar. Un fallo del servidor del modelo
no tira la tanda: `_run_once` captura `Exception`, el paso queda con `error` y el encargo
se conserva como `kept` sin haberse hecho, sin avisar. Lo que sí se pierde del todo es el
Ctrl+C: `KeyboardInterrupt` no es `Exception`, `attempt()` no tiene `try/finally` y la
carpeta queda con el encargo a medias. Nada se confirma en git.

**Propuesta.** `atlas develop PLAN --resume` (`resume=True` en `Developer`): leer
`reports/develop-<plan>.json`, dar por hechos los encargos cuyo último paso es `kept` sin
`error`, repetir los que tienen `error` o quedaron `interrupted`, reevaluar el estado real
con `self.evaluate()` (los tests mandan, no el informe) y seguir; el `.calls.jsonl` se abre
en `append`. En `attempt`, un `except BaseException` que restaure el `snapshot`, escriba el
paso como `interrupted` y relance. Y un parche por encargo conservado (4.9), que es lo que
permite revisar el resultado encargo a encargo. Los commits automáticos, solo como opción
(`commit = true`) y en una rama propia del plan, nunca en `develop` (sección 11).

### 4.2 Comprobación previa del plan y presupuesto por encargo (S, alta)

**Problema.** Peor de lo que parece: `Developer.__init__` solo valida `planner`
([develop.py:285-287](../hephestolm/hephesto/develop.py)). Con `agent = "nadie"`, el `ConfigError`
lo atrapa el `except Exception` de `_run_once`, cada encargo queda como paso con `error`,
no rompe nada y se conserva: el plan recorre todos los encargos y las rondas sin ninguna
llamada al modelo (gastando una ejecución del planificador por encargo, si lo hay) y
escribe «0 of N tests pass». Nada comprueba que los tests casen con `protected` (el
docstring dice «should be») ni que los agentes tengan una herramienta que escriba. No hay
presupuesto: con `max_steps` 40, `restarts` 1, `attempts` 2 y `build_fixes` 2, un encargo
puede costar cientos de llamadas.

**Propuesta.** En `__init__`, sin opción: validar `agent` y `fix_agent` con `definition()`,
que tengan alguna herramienta de `WRITES` y `check_code` (`fix_agent` además `run_test`), y
que todo test de `self.tests` cumpla `protected_file()`; `ConfigError` si no. Un
`Developer.check()` que además pase `check_code` sobre los ficheros de `check`, ejecute los
tests una vez (el estado inicial) y avise de encargos largos (más de 1200 caracteres);
`atlas develop PLAN --check` lo imprime. Presupuesto en el plan: `task_calls`,
`task_seconds` y `task_cost` (0 = sin límite), comprobados antes de cada ejecución extra
del mismo encargo; al superarlo, no se lanza más y el informe lo dice. Conviene que sea la
misma noción de presupuesto (peticiones y minutos) que la de `atlas run` (8.3), servida
desde `atlas.llm` a los dos.

### 4.3 Encargos anclados a requisitos (M, alta)

**Problema.** La trazabilidad entra en `develop` solo por instrucción: `implementer.md`
pide poner `-- Req:` y nada comprueba que se puso; el encargo nombra los requisitos en
texto libre y el agente no recibe el texto oficial ni su sección; `advances` se escribe a
mano aunque el grafo sabe qué tests verifican cada requisito. `load_plan` solo admite
`id`, `text` y `advances` por encargo.

**Propuesta.** Clave `requirements = ["SRS-0113"]` por encargo, validada al arrancar
(un ID que no está en el grafo es `ConfigError` antes de empezar). Tres efectos: (1) el
prompt lleva delante, por requisito, su texto oficial, la sección que lo declara y el
código que ya lo cita (de `graph.trace`, recortado a un máximo de caracteres: no el
`build_context` entero), más los ejemplos de 3.4; (2) sin `advances`, se rellenan con los
tests del plan que verifican esos requisitos, siguiendo los enlaces `verifies` de DOORS
cuando los tests generados por `atlas tests` citan el STP (para eso hace falta 6.1); (3)
tras un encargo conservado, se analizan los ficheros cambiados con el analizador (sin
pipeline) y el paso dice qué requisitos del encargo no cita ningún subprograma cambiado
(`untagged`); con `require_tags = true`, el encargo vuelve al agente una vez.

### 4.4 `outline` y `read_subprogram` (S, alta)

**Problema.** Toda edición exige haber leído el fichero con `read_file`, también
`replace_subprogram` y `add_subprogram`, que no copian nada del fichero
([tools.py:252-258](../hephestolm/hephesto/edit.py)). El agente lee ficheros enteros (300 líneas,
unos 4000 tokens) y los relee tras cada cambio: en la tanda 4, las vueltas de un arreglo
llevaban 55 000 tokens de entrada, casi todo lecturas viejas. En el proyecto real, con
cuerpos de cientos o miles de líneas, `read_file` trunca a 300 y el modelo no sabe las
líneas.

**Propuesta.** Dos herramientas de solo lectura en `CodeTools`: `outline(path)` devuelve
los subprogramas del fichero con tipo, líneas, nombre cualificado, firma y los IDs de su
comentario de cabecera, a partir de `symbols.subprograms`; `read_subprogram(path, name,
line=0)` resuelve el subprograma como `replace_subprogram` (misma desambiguación) y
devuelve solo ese bloque numerado con su comentario, marcando el fichero como leído
(`_seen`) para que las herramientas de subprograma acepten el cambio. Las instrucciones de
`implementer` y `fixer` pasan a «`outline` → `read_subprogram` → `replace_subprogram`».

### 4.5 Procesos huérfanos, tiempos por clase y el `.exe` de Windows (S, alta)

**Problema.** `_run` usa `subprocess.run(timeout=...)` ([build.py:322-332](../hephestolm/hephesto/build.py));
al expirar, Python mata solo al hijo directo (reproducido: un envoltorio que lanza `sleep
20` deja el `sleep` vivo). Con un envoltorio como `with_server`, el servidor sobrevive y
los tests siguientes fallan por una causa que no es del código, que el agente intenta
arreglar en el código. Un solo `command_timeout` (120 s) vale para compilar un fichero,
ejecutar un test y correr el programa. Y `run_test` busca el ejecutable como `build_dir /
stem` sin extensión: en Windows, `built: False` sin errores.

**Propuesta.** `Popen` con `start_new_session=True` (POSIX) o
`CREATE_NEW_PROCESS_GROUP` (Windows); al expirar, matar el grupo (`os.killpg` con un
segundo de gracia, o `taskkill /T /F`) y vaciar las tuberías; el error dice que se mató el
grupo. `check_timeout`, `test_timeout` y `program_timeout` en `[sources.code]`, con
`command_timeout` como valor por defecto. En `run_test`, el ejecutable es el primero que
exista de `stem` y `stem.exe`, y `{exe}` en `run_command` recibe ese valor.

### 4.6 Los agentes medidos dentro del repositorio y un banco de encargos (M, alta)

**Problema.** Las cifras del README (53 % → 11 %, 23 min, 0,23 $) salen de
`atlas-socket-demo`, otro repositorio. En este no hay plan, ni `fixer` ni `planner`, y el
`implementer` de `examples/agents/` es el de la fase 5: `edit_file`/`write_file` sin
`replace_subprogram`, `library_spec`, `search_reference`, `keep_tool_results` ni
`stall_steps`. `docs/seguridad.md` los presenta como «ejemplos para copiar». Y `atlas eval`
no sabe medir una tanda: cada versión del bucle se juzga a mano.

**Propuesta.** (A) En horas: `examples/agents` gana `fixer.md` y `planner.md`, e
`implementer.md` pasa a ser el medido (herramientas, `stall_steps` 15, `keep_tool_results`
10), con los parámetros de muestreo de `gemma-4-26b` en `[agents.<nombre>]` de
`atlas.openrouter.toml` y un comentario de con qué modelo se midió; `tests/test_examples.py`
comprueba que cargan y que sus herramientas existen. (B) Después: casos `develop:` en un
conjunto aparte (`examples/eval/develop.yaml`, `make eval-develop`) con un plan pequeño
sobre una carpeta de partida copiada a un temporal, `expect_tests_passed` y `runs`; el
informe guarda tests pasados, ediciones fallidas, llamadas, tiempo y coste por modelo.

### 4.7 Reglas de código deterministas y un revisor opcional (M, media)

**Problema.** Nadie mira el código salvo el compilador y los tests. Lo que ni uno ni otros
ven se queda: un `when others => null` que se traga una excepción en una ruta sin test,
ramas muertas, cambios fuera del encargo. El caso de HELLO (ocho encargos sin avanzar) ya
lo resolvió `advances`; lo que queda no tiene medida todavía.

**Propuesta.** Primero sin modelo: `[sources.code] rules`, ficheros TOML como `hints.toml`
con `[[rule]]` {`match`, `message`, `level`}, aplicados por `_store` al texto nuevo también
en ficheros nuevos: `error` no se escribe y devuelve la línea; `warning` va en `notes`. De
serie para Ada, como aviso: `when others\s*=>\s*null;` y `pragma Suppress`. Después,
opcional en el plan: `reviewer = "reviewer"` (solo lectura) con `review_fixes = 1`, que
recibe el encargo y el diff tras un encargo que compila y no rompe nada; su veredicto
vuelve al agente una vez. Medir con el banco de 4.6 antes de activarlo por defecto.

### 4.8 Borradores de pistas a partir del registro de llamadas (S, media)

**Problema.** El informe lista los errores sin pista como «las lecciones que añadir», pero
el paso lo hace una persona leyendo el `.calls.jsonl`: las dos pistas de la prueba corta
fueron un commit a mano. En el proyecto real, la cola larga será propia del proyecto y
`hints.toml` de serie no puede cubrirla.

**Propuesta.** `draft_hints(calls_path)` en la librería: agrupa por forma los errores sin
pista de `check`, de la sintaxis y de `check_code`, y para cada uno busca en la misma
ejecución la primera escritura posterior al mismo fichero tras la cual el error ya no está,
guardando ese diff y el mensaje con sus `details`. Escribe `reports/hints-<plan>.toml` con
un `[[hint]]` por forma, el `match` derivado y `hint` vacío, y el diff como comentario. La
persona revisa, redacta y copia a `[sources.code] hints`; nunca se activa solo. Como
sección del informe o como `atlas hints PLAN`.

### 4.9 Parche por encargo y modo propuesta (S, después M, media)

**Problema.** `develop` escribe en la carpeta de trabajo, y el plan deja fuera de alcance
que atlas edite código (`atlas link` propone la etiqueta y no la escribe). No hay forma de
recibir «cómo lo haría» como un diff por encargo, aunque `attempt()` ya toma un `snapshot`
antes y después.

**Propuesta.** (1) Parche por encargo conservado: `reports/develop-<plan>/<id>.patch`
(unificado, `n=3`, rutas relativas con `a/` y `b/`) y `all.patch` acumulado; el Markdown
enlaza cada paso con su parche. Sirve igual con `attempts` (el diff del ganador). (2)
Opcional, `propose = true` en el plan: `Developer.main` trabaja sobre una copia y la
carpeta de trabajo no se toca; la persona aplica los parches que quiera. En el proyecto
real, copiar la carpeta de trabajo entera puede no ser viable: (1) ya da casi todo.

### 4.10 Rondas con intentos, y encargos independientes a la vez (S + L, media)

`rounds()` llama a `attempt` y nunca a `attempts`: los intentos en paralelo no se usan en
las rondas (S: una línea). Los encargos van siempre en serie aunque una ejecución use unas
2 de las 28 peticiones por minuto del gateway: la tanda 4 tardó 4 h 52 min de agente, que
coincide con el tiempo de pared solo porque todo va en serie (el informe debería guardar
`wall_seconds`). Un `after = ["T01"]` por encargo y `parallel = 3` en el plan convertirían
`tasks()` en un planificador de grafo con la maquinaria de `attempts` (copia por encargo,
sus arreglos, sus `advances`), copiando a la carpeta de trabajo solo los ficheros que cambió
cada uno y deshaciendo el segundo si dos tocan el mismo fichero. Es L, el beneficio no está
medido, depende de que haya cadenas independientes y multiplica las peticiones
concurrentes contra un cupo compartido: rumbo, después de 4.2 y 7.3.

### 4.11 Ficheros que el agente deja en `src` (S, baja)

En la tanda 4 el agente dejó en `src` un fichero suyo de prueba, que entró en `check`, en
cada copia de `attempts` y en el siguiente `atlas run`. Tras cada encargo conservado,
comparar los snapshots y listar los ficheros creados (`created`, sección «New files» en el
informe); `stray = "delete"` borra solo los nuevos que casan con `test_patterns` y no con
`protected`. Un ámbito positivo de escritura por agente no hace falta: `protected` ya se
aplica a ficheros nuevos.

### 4.12 Lo que la capa de modelo aporta a `develop`

Los tres hallazgos de la sección 7 que más tocan a los agentes: el presupuesto de contexto
(`[llm] context_tokens` y un aviso cuando una vuelta no cabe; hoy `keep_tool_results` es 0
por defecto y nada cuenta tokens), la contabilidad de tokens y coste por encargo, y el
streaming con tiempo de inactividad (una vuelta colgada cuesta hoy 600 s y además se
duplica en el gateway, porque el hilo abandonado sigue vivo). Y el puente con el editor de
10.3: el juez de `develop` con un agente externo.

## 5. Índice, búsqueda y respuestas

**Estado.** Todo el índice vive en `atlas.db` (registros, vectores en BLOB, FTS5 y caché de
vectores por modelo, prefijo y texto); la búsqueda funde con RRF tres listas y
`per_document` evita que una unidad de código ocupe todos los puestos; `atlas ask`
reescribe la pregunta, decide si es amplia y entonces lee documentos enteros por partes.
Sobre las muestras: híbrida acierto@1 75 %, @3 90 %, MRR 0,84; léxica 55 %/70 %. Lo que
falta está en tres frentes: la confianza en la respuesta, el coste y la escala, y el
léxico.

### 5.1 Enrutar antes del índice (M, alta)

«¿Qué prueba verifica SRS-0004?» va por `atlas ask` y `ask_docs` al índice, que contesta
con pasajes de documentos, y gasta dos peticiones para no encontrarlo, mientras `trace` lo
da en milisegundos. El código sí está en el índice (cada `code_doc` lleva sus requisitos),
así que a veces sale por BM25; no es fiable. Propuesta: un enrutador determinista en
`DocTools.route(question)`: si la pregunta trae IDs (regex de `by_ref` o `id_pattern`) o
nombres de unidad y una palabra de trazabilidad (test, verif*, implement*, impact*, parent,
satisf*, call), construye fuentes numeradas deterministas desde `store.requirement(id)`
(texto oficial, padre, enlaces) y, cuando hay grafo, desde `trace`/`trace_code`/`impact`,
y `answer` las recibe como `extra_sources`. El editor ya tiene `trace` por defecto; el
beneficio es para `atlas ask`, `ask_docs` y el curso.

### 5.2 Evaluación por tipo de pregunta, con `ask`, y comparable entre versiones (M, alta)

`_summary` es global, los tipos de caso están solo en comentarios del YAML, el informe se
sobrescribe en cada ejecución y no existe ningún caso que llame a `Index.answer` (lo más
caro y lo más nuevo). Propuesta: `tags` por caso y métricas por tag; `reports/eval-<set>-
<version>-<fecha>.json` y `--compare` con el anterior por caso; tipo `ask:` con
`document`, `scope`, `expect` y `cited: true` (cada texto esperado en el contexto de una
fuente citada), guardando peticiones, `scope` y segundos; y una caché de los vectores de
consulta en `search.search` (hoy cada búsqueda híbrida embebe la pregunta: 20 peticiones
por pasada). Con 10.4, el conjunto del proyecto real se genera solo.

### 5.3 Presupuesto y plan previo de las preguntas amplias (M, alta)

Una pregunta amplia lee documentos enteros en partes de `scope_chars`, cuatro a la vez
(constante fija), y una última petición junta todas las notas de golpe sin límite
([search.py:276-277](../atlas/index/search.py)), aunque `_overview` sí combina por niveles.
Con un módulo real de miles de requisitos son decenas de partes y minutos del cupo de
todos, y nadie lo sabe antes. Propuesta: `[search] max_parts` y `scope_documents`;
`answer_whole` calcula `plan = {documents, sections, chars, parts, requests}` antes de
lanzar nada y, si `parts > max_parts`, devuelve el plan y pide acotar; `section` en
`ask_docs` y `--section` en `atlas ask` (filtrando `whole_sources` por `path`);
combinación jerárquica cuando las notas superan `scope_chars`; y elegir los documentos de
una pregunta que no los nombra buscando solo en `overview` y `section_summary`. Y una
parte que falla no tira la respuesta entera (7.6).

### 5.4 Léxico: stemming, sinónimos y siglas (S, alta)

FTS5 usa `unicode61` sin stemming y cada token de la consulta va como frase exacta:
comprobado con el SQLite de Python que «deletion» no encuentra «Deleted tracks» ni «log»
«logs», y que con `porter unicode61` sí (no «verifies»↔«verification»). Es la única
búsqueda que funciona cuando el gateway cae. Propuesta: `[index] fts_tokenizer` (por
defecto el actual, para no reconstruir a nadie sin querer); al abrir, `IndexStore` compara
el `sql` de `index_fts` con el pedido y, si difiere, recrea la tabla desde `index_records`
en segundos, sin reembeber. `[search] synonyms = "synonyms.toml"` con grupos de
equivalencias del proyecto, aplicados como una consulta léxica expandida más en RRF. Siglas:
los tokens de 2 a 6 mayúsculas se contrastan con las `definition` del glosario. Cuidado
con el español (sección 11): los resúmenes siguen `lang` y `porter` sobre texto español
empeora; stemming solo en los fragmentos de documentos en inglés, o tokenizador por idioma,
medido con 5.2.

### 5.5 Tablas de los PDF y troceado por estructura (M, alta, condicionada)

`reflow` une todas las líneas de un párrafo (una lista de pasos queda en una frase) y las
tablas llegan de pymupdf como bloques planos; el prompt de análisis lo admite y
`regex_requirements` toma el primer token como ID, así que una fila `SRS-0010 | Título |
texto | Test` da el título como texto, o nada. En especificaciones reales los valores viven
en tablas. Propuesta: `[[sources.docs]] tables = true` (apagado por defecto):
`page.find_tables()` (pymupdf 1.28 lo trae), cada tabla como filas Markdown entre
marcadores, quitando sus bloques de la página; `reflow` conserva el salto antes de viñetas
y filas; `chunk_text` no parte dentro de una tabla y repite la cabecera; `kind = "table"`
con la ruta de sección y el pie; `table_requirements = {id, text}` para un requisito por
fila sin modelo; y `path` en el `embed_text` del requisito de documento. Condición: añadir
antes una tabla a las muestras y comprobar con un PDF del proyecto real que los requisitos
van en tablas; es M con riesgo (tablas sin bordes).

### 5.6 Filtros por metadatos y `[sources.doors] attributes` con efecto (M, alta)

La búsqueda filtra solo por documento y por clase; los atributos de DOORS (estado, método
de verificación, criticidad) no se indexan ni se pueden pedir («requisitos aprobados sobre
borrado», «solo el capítulo 3»), aunque llegan al `Store`. `attributes` lleva dos versiones
«reservada: todavía sin efecto». Propuesta: columna `meta` (JSON) en `index_records`
(`INDEX_VERSION` a `index-2`), con los atributos de `attributes` y el módulo en los
requisitos, el capítulo en los fragmentos y la clase en `code_doc`; `where` en
`Index.search` y `attribute = "status=Approved"` en la herramienta `search`, `atlas search` y
`atlas ask`; los atributos elegidos entran en el `embed_text`. Con 6.4 (columnas de la
matriz), `attributes` pasa a significar «los atributos que atlas enseña y por los que
filtra», no una lista blanca de importación (eso ya lo hacen `attribute_columns` y el
`mapping.toml` del exportador): una sola semántica, en un solo cambio (sección 11).

### 5.7 Citas comprobadas contra el texto (S, media)

En modo pasajes, `sources` son todas las enviadas, cite el modelo cada una o no; en modo
entero solo se filtran las `[n]` que aparecen; nada comprueba que un `[n]` esté en rango ni
que el ID o la cifra atribuidos a `[n]` estén en ese pasaje. Propuesta: `check_citations`
al final de `answer` y `answer_whole`, sin modelo: `[n]` fuera de rango → `out_of_range`;
por cada frase con cita, los IDs y los números con unidad se buscan (canonizados) en el
contexto de alguna fuente citada en esa frase; `unsupported: [{claim, cited}]` en el
resultado, y una línea en `atlas ask` y `ask_docs`. En pasajes, `sources` solo las citadas
y el resto en `unused`. De aquí sale también el `status` de 8.7, y es la métrica de
fidelidad para el caso `ask:` de 5.2.

### 5.8 Lo demás, en orden

- **Contexto alrededor de lo encontrado** (S, media): `neighbours` solo amplía los `chunk`;
  un requisito de DOORS llega como su frase a secas y un `section_summary` sin texto. Para
  DOORS, los registros contiguos del mismo módulo (el orden de inserción es el del módulo);
  para los resúmenes, los primeros fragmentos de la sección.
- **IDs con variantes** (S, media): `by_ref` exige `UPPER(ref)` exacto; «SRS 0004» sí sale
  por BM25, pero «srs0004», «SRS_0004» y «srs-4» no. `ids_in(query)` con los prefijos
  indexados y su anchura de dígitos, rellenando ceros y conservando solo lo que existe. La
  misma normalización que 6.3.
- **Prefijos de embeddinggemma explícitos** (S, media): se eligen por subcadena del nombre
  del modelo y, si el gateway lo publica con otro nombre, no se aplica ninguno y nadie lo
  dice. `[embeddings] family` y prefijos literales, en las huellas del índice, y una línea
  con los prefijos efectivos (o el aviso) en `atlas check`, que ya imprime modelo y
  dimensión.
- **Escala de la búsqueda densa** (M, media): la matriz de vectores se recarga fila a fila
  cuando cambia `PRAGMA data_version`, que cambia con cualquier commit de otra conexión a
  `atlas.db`, incluido el libro de registro de un `atlas run` en marcha; `dense` ordena todo
  y filtra en un bucle Python. Primero medir sobre `make large` (no hay cifras); después un
  contador propio del índice (`user_version` incrementado solo al reemplazar documentos),
  `argpartition` y máscara numpy; el BLOB contiguo solo si la medida lo justifica.
- **Pesos de RRF y reordenado** (S, media): `rrf` tiene `k` y peso fijos; el único caso
  documentado (el código desplazando a un documento) se midió antes de `per_document`.
  Comprobar primero con `atlas eval` si sigue ocurriendo; después `[search] rrf_k` y
  `weights` por lista, ajustados solo con un conjunto del proyecto real.

## 6. Grafo, trazabilidad, documentos y DOORS

**Estado.** El grafo unificado está sólido y medido: construcción incremental por origen,
consultas sin modelo con 10 de 10 en `atlas eval`, y en una sonda con 20 000 SRS, 2000 SSS,
3000 subprogramas y 2000 secciones carga en 0,28 s, `trace` e `impact` en milisegundos y
unos 30 MB: networkx en memoria no es un límite. La precisión la limitan el analizador Ada
y la falta de semántica en los tipos de enlace de DOORS; para una revisión formal faltan la
fecha de la exportación y la procedencia de la matriz.

### 6.1 Semántica de los tipos de enlace de DOORS (M, alta)

Comprobado con un grafo en memoria: con `STP-0101 -verifies-> SRS-0004`, `matrix` lista el
STP como `derived` y `impact` lo sube como requisito derivado; y, como la arista llega con
tipo `verifies`, `_tests_of` la toma por una prueba: **el procedimiento de prueba aparece
en la columna de tests como si fuera una prueba de código**, que es lo peor para un
revisor. `_derived` trata cualquier enlace entre módulos como derivación
([knowledge.py:180-187](../atlas/graph/knowledge.py)), también `references`. Propuesta:
`[sources.doors.convention] link_semantics = {derives = [...], verifies = ["verifies"],
ignore = ["references"]}` (por defecto todo deriva salvo `verifies` y `references`);
`_tests_of` solo cuenta nodos `test`, `_procedures_of(r)` devuelve los requisitos que lo
verifican; `_derived`, `_through` y los ascensos de `impact` solo siguen `derives`; la fila
de `matrix` gana `procedures`, el resumen distingue pruebas de código y procedimientos, y
`gaps` separa `without_tests` de `without_test_procedure`. Es también lo que necesita 4.3
para rellenar `advances` desde los STP.

### 6.2 Analizador Ada: tareas, operadores, sobrecargas, `use` (M, alta)

Comprobado con tree-sitter-ada sobre un fichero de sonda: `task body`, `entry body` y
`function "+"` no generan subprograma (sus llamadas y sus `Req:` desaparecen en silencio;
en un servidor Ada el bucle principal suele ser una tarea); dos `Run` del mismo paquete
quedan como un nodo con las líneas y los `Req:` del último; `use`, `renames` y los genéricos
no se capturan; `T'Image` mete `t` como llamada. Propuesta en dos entregas: (a) S: patrones
para `task_body`, `entry_body`, `protected_body` y operadores, `kind` por subprograma, y las
sobrecargas fundidas en un nodo con la unión de requisitos y llamadas y una lista de
`bodies` que `read_source` recorre (sin identificadores nuevos: cambiarían las etiquetas de
todo el mundo); versiones `ada-2` y `graph-3`. (b) M: `use_clause` como lista `uses` que la
resolución consulta tras la propia unidad, `renames` e instancias genéricas como alias.

### 6.3 Extracción de IDs en las etiquetas (S, alta)

Sonda sobre el código actual: `-- Req: SRS-0004 and SRS-0005` da solo `SRS-0004`; `see
SRS-0004` no da nada; `SRS-0004 / SRS-0005` pierde el segundo; `SRS 0004` no da nada y
`normalize_id` lo haría `SRS0004`; `srs-4` es un nodo distinto de `SRS-0004`. El mismo
defecto está en `regex_requirements` de los documentos. La convención de IDs es una pregunta
abierta del plan desde la fase 4. Propuesta: `[sources.doors.convention] id_regex`
(derivado de `id_pattern` sin anclas si no se da) con `findall` sobre el resto de la línea,
pasado a los analizadores en su configuración y a la huella de `code.parse`;
`id_canonical = {regex, format}` opcional para casar variantes (`SRS-4` → `SRS-0004`) sin
reescribir los IDs oficiales de DOORS; y una sección `id_shapes` en `atlas graph` (las
formas de ID encontradas por origen), que es la respuesta empírica a la pregunta abierta en
cuanto se procese el proyecto real.

### 6.4 La matriz para una revisión formal y la fecha de la exportación (S + M, media)

`exchange_modules.filled_at` y `source` (cuándo y de dónde se exportó cada módulo) no los
lee nadie fuera de `exchange.py`; `trace`, `matrix`, `status` y los informes no dicen de
qué exportación salen, aunque el plan pedía «fecha de exportación visible» como mitigación
del riesgo de datos desfasados. `_row` saca solo `status` y `verification`; el CSV no tiene
cabecera de procedencia. Propuesta: (a) S: `RequirementsAdapter.exported_at(module)` (no
abstracto, vacío por defecto; el adaptador `exchange` lo lee de `exchange_modules`); una
tabla `modules` en `Store` que `doors_step` rellena; lo enseñan `atlas status` («DOORS: 3
módulos, exportación 2026-10-14, hace 9 días»), `atlas graph`, la cabecera de `matrix-*`
y un campo `exported_at` en `trace`, `get_requirement` y `build_context`; `[sources.doors]
max_age_days` avisa en `run` y `serve`, y `doctor` lo marca. (b) S: `attributes` como lista
de atributos que van a las filas de `matrix`, `list_requirements` y el CSV (por defecto los
dos de hoy); `meta` con `atlas_version`, `built_at` y, si hay git, la revisión del código.
(c) M, para después: guardar los IDs añadidos, cambiados y quitados de cada ejecución (hoy
solo cuentas en el log) en una tabla pequeña, y `atlas graph --since` que liste qué cambió
en la trazabilidad; una tabla de historial completa por requisito no hace falta todavía.

### 6.5 Consultas que faltan en `gaps` y `trace` (S, alta)

`gaps` no lista los requisitos sin enlace de derivación hacia el módulo superior ni los
destinos de enlaces a módulos no leídos (el fixture `unified` tiene ambos casos y `gaps`
no los nombra); `trace` da un solo nivel. Propuesta, sin herramientas nuevas:
`requirements_without_derivation` por módulo (con `top_modules` para excluir la raíz) y
`linked_to_modules_not_read` (ID destino, quién lo enlaza, tipo) en `gaps`; `ancestors` en
`trace` (la cadena hasta el requisito de sistema, 5 niveles, con el tipo de cada salto). Son
las preguntas estándar de una revisión de asignación y de la exportación de DOORS.

### 6.6 Lo demás, en orden

- **`values_differ`** (S, media): reproducido con `_values`, 6 de 7 casos dan veredicto
  erróneo («300 ms» frente a «0.3 s», «1,000» frente a «1000», «section 3.2», «Table 3»,
  «1.5» frente a «1.50», «two» frente a «2»). `quantities(text)` con unidades canónicas,
  exclusión de referencias y comparación numérica; `values_differ` con `kind`
  (`value`/`unit_only`/`format_only`) y `gaps` listando solo `value`. No ahorra peticiones
  (el verificador pregunta igual), pero quita ruido al revisor y al contexto de los agentes.
- **Versión y fecha de cada documento, y cuál es el normativo** (M, media): el nodo
  `document` solo tiene título y ruta; nadie extrae versión, fecha ni código, ni los
  metadatos del PDF; dos documentos que se contradicen no se detectan. `doc_id_regex`,
  `version_regex`, `date_regex` por fuente, `authority = N` por grupo, y
  `documents_disagree` en `gaps`; la detección automática de documentos sustituidos, solo
  como aviso.
- **Cachés en `matrix` y `gaps`** (S, baja): construyen todas las filas antes de paginar
  (medido: 0,72 s con 20 000 requisitos, tanto con `limit=50` como con `offset=100`).
  Cachés por instancia para `_derived`, `_through`, `_tests_for` y `_documents`, y paginar
  antes de construir filas.
- **La interfaz `CodeUnit`** (M, baja): `build.py` importa `PREDEFINED_TYPES` de
  `atlas.code.ada` (acoplamiento), las llamadas no llevan línea, `tests` no lo usa nadie.
  Cambios aditivos (`kind`, llamadas con `line` y `column`, `signature`,
  `CodeAnalyzer.predefined`), sin un analizador de C++ de ejemplo hasta que haya quien lo
  use. Las llamadas con posición son el prerrequisito del siguiente.
- **Referencias exactas con el Ada Language Server en la construcción del grafo** (L,
  rumbo): un paso `code.resolve` que pida `textDocument/definition` por cada llamada y guarde
  las aristas exactas, con la resolución por nombres como respaldo. El plan lo reserva al
  compilador. Antes de empezar: medir `ambiguous_calls` y `unresolved_calls` del proyecto
  real; con `with` y nombres cualificados, la resolución por nombre acierta casi siempre.

## 7. Capa de modelo

**Estado.** La conexión está bien resuelta para un puesto corporativo: un cliente con
proxy, CA y certificado de cliente, limitador de peticiones compartido por destino,
espera por `Retry-After` y cabeceras `x-ratelimit`, descenso adaptativo, reintentos y
diagnóstico de los errores típicos; `JsonLlm` recupera JSON envuelto o cortado;
`AtlasLlm` devuelve el razonamiento al modelo y acota cada vuelta; `atlas check` y `atlas
probe` comprueban el gateway. Lo que falta está en la contabilidad, en el cupo visto desde
fuera del proceso, en el contexto y en las vueltas colgadas.

### 7.1 Salida estructurada por escalones y `finish_reason` (S, alta)

Ante 400/422/501, `_call` quita `response_format` del todo, cuando casi todos los
servidores que rechazan `json_schema` aceptan `json_object`, que ya evita la prosa y los
bloques de código; `ask_json` solo comprueba que haya alguna clave esperada (los tipos los
parchean `normalize` y `_clean` cada uno a su manera); el escalón aprendido es por instancia
y hay siete creaciones de `JsonLlm` (cada llamada MCP de `verify`/`link` abre un `Index`
nuevo y paga otra petición rechazada); `ask_text` devuelve el texto sin mirar
`finish_reason`, así que una respuesta cortada por `max_tokens` llega al usuario sin aviso.
Propuesta: tres escalones `json_schema → json_object → ninguno`, aprendidos por
(`base_url`, `model`) en un diccionario de módulo con lock y mostrados por `atlas check`;
`validate(obj, schema)` para el subconjunto que atlas usa, citando la primera violación en
la vuelta de corrección; `ask_text` que repita con el doble de `max_tokens` cuando
`finish_reason == "length"` y marque `truncated`.

### 7.2 Tokens, razonamiento y coste por petición, comando y paso (S núcleo, media)

Solo los agentes cuentan tokens, y porque ADK los devuelve; `JsonLlm` y `Embedder` no leen
`usage`, los contadores de proceso no tienen tokens, `atlas ask` imprime solo peticiones y
`atlas agent` nada. El «55 000 tokens de entrada por vuelta» de la tanda 4 se supo mirando
a mano. Propuesta: `note_usage(response, endpoint)` en `connection.py` que sume
`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens` y `cost` a los
contadores, llamada desde los cinco puntos que reciben respuestas; con eso los perfiles de
`atlas run` y `atlas status` los enseñan sin tocar el motor, y `ask`, `agent` y `eval` los
imprimen. Una etiqueta de comando y paso permite un registro de uso por línea en el
workspace (8.8); conviene que sea un solo registro con el de ejecución (9.7), no tres. Sin
precios configurables: el gateway no tiene precio público.

### 7.3 El cupo compartido entre procesos (M, media)

El limitador es un diccionario del proceso. En el puesto conviven `atlas serve` (cuyo
`ask_docs` y `verify_requirement` llaman al modelo), un `atlas run` de horas y `atlas
develop`: cada uno cree tener las 28 peticiones por minuto, y el gateway rechaza al que
toque. Propuesta: una ventana compartida en un fichero por destino (`~/.atlas/limits/<host>.json`,
en Windows `%LOCALAPPDATA%`), con las marcas de tiempo de los últimos 60 s y el ritmo
adaptado, leída y escrita bajo un bloqueo de fichero como el de `_WorkspaceLock`, con el
limitador en memoria como respaldo; `[network] shared_limit`. La clave del fichero no debe
llevar un hash de la clave de API (contradice `docs/seguridad.md`). Antes, medir la
contención real: no está demostrado que una pregunta del editor tire un lote nocturno.

### 7.4 `atlas run --estimate` (M, media)

Con el gateway, una ingesta son horas; el Runner sabe las unidades pendientes tras el
escaneo (y lo escribe por paso a nivel INFO), pero no cuántas peticiones implican ni cuánto
tardarán. Propuesta: `Runner(dry_run=True)` recorre unidades y huellas sin tomar el cerrojo
ni ejecutar nada y devuelve `pending` por paso; `atlas estimate` (o `run --estimate`)
traduce: para `docs.process`, `preview()` más la existencia de la caché por sección da las
secciones que irán al modelo (+1 por documento, +10 % de margen por divisiones); para el
índice, los textos sin vector entre `batch`. Tiempo = máx(peticiones / cupo, peticiones ×
latencia / `workers`), con la latencia del último perfil en la tabla `runs` (`api_seconds`
entre peticiones) o `--latency N`. Una línea tipo «docs.process: 1240 chat requests, about
45-55 min at 28/min» al empezar `atlas run` y en `atlas status`. Las cifras del README
del proyecto grande (540 peticiones de chat = 20 PDF × 27; 237 de embeddings = 15 120
textos / 64) confirman la fórmula.

### 7.5 Presupuesto de contexto por petición (M, media)

Todos los límites son caracteres (`max_chars` 12 000 por sección, `scope_chars`,
`CHARS_PER_TOKEN = 4`, el limitador por tokens «estimado como caracteres/4»); `AtlasLlm`
envía todos los mensajes sin recuento ni tope, `keep_tool_results` es 0 por defecto, y
nada sabe cuánto contexto tiene el modelo. Lo valioso primero: `[llm] context_tokens` (0 =
desconocido), que `atlas check` pide al servidor cuando lo publica (OpenRouter, vLLM,
Ollama) y contrasta; con él se derivan `scope_chars` y `context_chars` cuando no están, y
`AtlasLlm` estima los tokens de cada vuelta y, si no caben, aplica `masked` con un `keep`
menor y avisa. La calibración de caracteres por token con lo que devuelve `usage` es la
segunda pieza.

### 7.6 Lo demás, verificado

- **Streaming con tiempo de inactividad** (L en total, alta; partido en tres). Hoy no hay
  streaming: `AtlasLlm._create` acota la vuelta abandonando un hilo demonio y repitiendo
  hasta dos veces, así que una vuelta colgada puede dejar tres peticiones vivas en el
  gateway y el hueco de `max_concurrent` ocupado; `JsonLlm` no acota nada (el `timeout` de
  httpx es entre lecturas: la petición colgada una hora); y `answer_whole` pierde las cuatro
  partes si una falla. (a) S: `answer_whole` recoge cada parte por separado y una que falla
  deja una nota y sigue con las demás. (b) `[llm] stream` en `JsonLlm` y `AtlasLlm` con
  `include_usage`, ensamblando `content`, `reasoning` y `tool_calls` por índice; si el
  servidor lo rechaza, se apaga para el resto del proceso como hace `schema_ok`, y `atlas
  probe` imprime «streaming: ok | not supported». El valor por defecto depende de lo que
  mida `probe`, no al revés (sección 11). (c) `idle_timeout` (segundos sin recibir un trozo)
  y `turn_timeout` (total) en `[llm.network]`, que sustituyen al hilo abandonado.
- **El servidor falso** (S, media): solo rechaza con 409 en embeddings; no hay 429 ni 5xx en
  chat, ni `Retry-After`, ni `tool_calls`, y `usage` es siempre 1/1 tokens; los agentes se
  prueban con un cliente de pega en memoria. Cada mejora de esta sección necesita poder
  probarse en `make test`, no en una tanda de horas.
- **Concurrencia dimensionada con la latencia medida** (S, media): el limitador solo
  espacia envíos, la concurrencia del modelo en `docs` es la de los documentos y la guía
  pide adivinar `workers` con una latencia supuesta de 10 s; con 20-30 s por sección, el
  cupo se llena a medias. Primero `atlas status` por paso: «chat: 540 en 31 min = 17/min de
  28; latencia 21 s; 6 en vuelo; 8 llenarían el cupo» (ley de Little, con lo que el perfil
  ya guarda); después `[llm] concurrency` separado de los hilos de documentos y `[search]
  part_workers`; `auto` solo más tarde.
- **`compare_models` como proceso repetible** (S, media): la tabla solo tiene aciertos,
  mediana y errores, y descarta los tokens y el coste que `ask` devuelve; nada guarda
  modelo, `reasoning_effort`, proveedor, versión ni fecha. Añadir eso al informe de `eval`,
  columnas de coste y de recuperación a la tabla, `reports/compare-<fecha>` acumulables, y
  una tanda corta de `develop` por modelo con un plan de cuatro encargos.
- **Razonamiento y modelo por tarea** (S, baja): más estrecho de lo que parecía. Los agentes
  y `verify`/`link`/`comply` ya eligen modelo y razonamiento por su cuenta; lo que no se
  puede es distinguir análisis de documentos, reescritura y respuesta entre sí. Sobrecargas
  por petición en `JsonLlm`, `[search.rewrite]` y `[search.answer]`, y `model` y
  `reasoning_effort` en `[[sources.docs]]` entrando en la huella.
- **Caché de reescrituras de preguntas** (S, baja): el beneficio estaba sobrestimado (la
  herramienta `search` del editor no reescribe y `eval` no repite reescrituras). Si se hace,
  una tabla en `IndexStore` con clave por modelo, versión del prompt y pregunta normalizada.
- **Qué aporta ADK y qué cuesta** (L, baja, rumbo): se usa en dos ficheros para cuatro
  cosas (esquemas desde funciones, bucle, sesión con `max_llm_calls`, `adk web`) a cambio de
  unas 150 líneas de traducción y varios apaños. Pero el streaming, la cancelación y el tope
  por vuelta viven en `AtlasLlm`, no en el bucle, así que no exigen otro runtime. Lo que
  queda es el riesgo del pin a 2.11 y un conversor de esquemas que podría ser único para el
  bucle, el servidor MCP y ADK. `[agents] runtime = "adk" | "native"` como segundo runtime
  seleccionable, probado con la misma suite, solo cuando el resto esté hecho.

## 8. Adopción y aceptación en el proyecto real

**Estado.** El camino está cubierto de punta a punta, pero solo en macOS y con proyectos
sintéticos: wheelhouse sin red, `atlas init`, `check` y `probe`, `run` reanudable con
perfiles, `serve` que diagnostica el entorno del editor, y DOORS por COM y DXL validado en
un DOORS real salvo las primitivas de enlaces. Lo que el plan deja sin marcar sigue sin
hacer: ninguna instalación en Windows y ninguna ejecución sobre el proyecto real.

### 8.1 `atlas doctor` (M, alta)

`atlas check` hace una petición al chat y otra a los embeddings y nada más (comprobado).
Todo lo demás que puede fallar en un puesto nuevo se descubre horas después: una raíz de
código mal escrita da 0 ficheros sin aviso (`rglob` sobre una carpeta inexistente,
[cli.py:110-117](../atlas/cli.py)), mientras que los documentos sí avisan; la base de
intercambio solo falla al abrirse en el paso `doors`; los ejecutables de `tool_path` y los
comandos del compilador, al ejecutarlos; los extras que faltan, en `sys.exit` al usar la
acción. Paso 0, hoy mismo: un `warning` por cada raíz de código que no exista. Paso 1:
`atlas.doctor.diagnose(cfg)` en la librería, de solo lectura, y la acción `atlas doctor`:
Python y plataforma; extras importables según los pasos y herramientas configurados; cada
ruta de las fuentes con cuántos ficheros procesaría (0 = aviso); la base de intercambio con
su versión, módulos y fecha de exportación; `check_command`, `test_command`,
`language_server` y `tool_path` resueltos con `which`; las variables del entorno que el
TOML nombra; el workspace y su esquema; la conexión. Tabla OK/WARN/FAIL y código de salida.
Los avisos que otras propuestas añaden a `atlas check` (prefijos de embeddings, contexto del
modelo, razonamiento) cuelgan de aquí.

### 8.2 Guion de aceptación: las dos primeras semanas (S, alta)

No hay un documento que diga en qué orden hacer la aceptación ni qué medir, y lo que hay
que usar existe ya (no depende de `doctor` ni de `estimate`). Propuesta:
`docs/aceptacion.md` con casillas y una cifra por paso. Semana 1: (1) puesto Windows
limpio: wheelhouse, `atlas --version`, `init`, `check` y `probe` por el proxy con `ca_cert
= "system"`, y `tests/test_security.py` desde el wheelhouse; lo que falle, a `docs/faq.md`.
(2) En el PC con DOORS: `--check` sobre un módulo, descarga, `publish.py`, `atlas run` con
`steps = ["doors"]` y la primera matriz real (día 2). (3) `atlas sections` sobre diez PDF
reales (ajustar `structure`/`heading_regex`); `run` con `analysis = "regex"` sobre
documentos y código: grafo, `gaps` y `matrix` sin gastar cupo, las formas de ID (6.3),
`ambiguous_calls`; segunda ejecución sin cambios: tiempo y 0 peticiones (meta 1). Semana 2:
(4) la ingesta con `analysis = "llm"` de noche, por fuentes, con el perfil de `status`
después; (5) `atlas eval` con 20 preguntas escritas por dos ingenieros del proyecto más el
conjunto generado de 10.4; (6) el servidor en el editor de dos personas una semana, con el
registro de uso de 8.8; (7) las cifras de las metas 1 a 7 rellenadas en el plan.

### 8.3 Presupuesto, progreso y ejecución nocturna (S, alta)

`atlas run` solo para por Ctrl+C, error fatal o al acabar; `stopping` solo lo activa la
interrupción (comprobado) y no hay «hasta las 7:00» ni «como mucho 1000 peticiones», aunque
el motor es reanudable. Primero, documentación: el Programador de tareas de Windows puede
parar la tarea pasado un tiempo y el motor sobrevive a un `kill` (está probado). Después,
`[pipeline] max_minutes` y `max_requests` y `--until HH:MM` en `atlas run`, sin hilo
temporizador: `process()` comprueba antes de cada unidad si se pasó el plazo o el contador
de peticiones y activa `stopping`; las unidades en vuelo acaban su `Interrupted`, que se
absorbe por unidad (quedan `running` como tras un `kill` y se reprocesan la próxima vez),
los pasos siguientes no empiezan, `stopped_by = "budget"` en el informe y código de salida
propio. Más una línea de progreso cada 30 s o cada 25 unidades con la estimación de lo que
queda (7.4).

### 8.4 Entregables que Excel abra bien (S, alta)

Comprobado: `atlas matrix` escribe el CSV con coma y UTF-8 sin BOM
([cli.py:716-724](../atlas/cli.py)); en un Windows con Excel en español se abre en una sola
columna y los acentos salen mal. Paso 1, sin dependencias: mover la escritura de la matriz
de la CLI a la librería (`atlas/graph/export.py`), `utf-8-sig` y `[graph] csv_delimiter =
";"` por defecto (no hay sección `[reports]`; la matriz ya cuelga de `[graph]`), listas con
salto de línea dentro de la celda; el cambio del valor por defecto va una sola vez en «Al
actualizar». Paso 2, extra opcional con `openpyxl` (wheel puro): `"xlsx"` en `[graph]
matrix_formats` con filtros, hoja de huecos y la fecha de la exportación de DOORS. Sin
`.docx`: `python-docx` arrastra `lxml` (nativo, el riesgo de Windows del plan) y el
Markdown de los informes ya se pega en Word.

### 8.5 `atlas trace` legible y `--json` en todas las acciones (S, alta)

Comprobado: `atlas trace`, el comando diario, imprime el diccionario de `trace` como JSON
crudo ([cli.py:698-703](../atlas/cli.py)), mientras `build_context` ya compone esa
información en Markdown sin modelo, con las cifras que difieren de DOORS incluidas, y sin
necesitar el extra `[agents]`. Propuesta: `atlas trace ID` imprime `build_context(...,
budget_tokens=20000)` (más holgado que la herramienta, para no cortar en pantalla) y
`--json` el diccionario de hoy; una opción `--json` para las acciones de texto (`status`,
`graph`, `matrix`, `verify`, `comply`, `link`, `coverage`, `impact`, `sections`), que
escriba por stdout solo el diccionario que ya devuelven las funciones de librería. Lo
necesitan el Programador de tareas, el curso y cualquier script. Un «Al actualizar» para
el cambio de salida de `trace`.

### 8.6 La configuración de producción coherente (S, media)

El `atlas.toml` de producción del repositorio no pone `reasoning_effort` (gemma 4 razona por
defecto; con `gemma4:12b` en local es la diferencia entre 15 s y minutos por sección), usa
otra variable de entorno que la plantilla y la guía (`GW_KEY` frente a `ATLAS_API_KEY`) y
no trae `[[sources.docs]]` ni `[search]`. Dos plantillas en `atlas/templates`
(`atlas.toml` para Ollama, `atlas-gateway.toml`) y `atlas init --profile gateway|ollama`;
la de gateway con los valores que ya se saben buenos (`reasoning_effort = "none"` y el
comentario de que los agentes lo encienden en su fichero, `requests_per_minute = 28`,
`max_concurrent = 6`, `ca_cert = "system"`, `scope_chars` para 200k); alinear el
`atlas.toml` del repositorio y una sola variable en plantilla, ejemplo y guía. `atlas check`
avisa cuando detecta que el modelo razona y `reasoning_effort` está vacío.

### 8.7 Estado explícito de cada respuesta (S, alta)

Cuando no hay nada indexado, la respuesta es la frase «Nothing indexed is related to the
question», y cuando las fuentes no bastan, el prompt pide decirlo «claramente»: ni
`ask_docs`, ni `ask_atlas` ni la CLI tienen un campo que lo diga, y un agente o un script no
distinguen una respuesta de una no-respuesta. Sin tocar los prompts ni pedir JSON al modelo
(meter una respuesta larga dentro de un JSON en un gateway que a veces no admite esquema es
peor): `status` determinista en `answer` y `answer_whole`: `not_found` (sin fuentes o la
cadena fija), `answered` (al menos una cita y ninguna frase sin cita que contenga una cifra
o un ID), `partial` (lo demás); `uncited` con esas frases; en pasajes, `sources` solo las
citadas más `consulted`. `atlas ask` imprime «status: partial (2 sentences without a
source)» y sale con código 1 en `not_found`; los agentes de serie reciben la instrucción de
decirlo. Es la salvaguarda barata que falta frente al modelo pequeño, y sale de 5.7.

### 8.8 Registro de uso del servidor y de las preguntas (S, media)

No queda rastro de qué herramientas llama el editor, cuánto tardan, cuántas fallan ni qué
preguntas no tienen respuesta; solo `develop` guarda sus llamadas. Un único envoltorio
`instrumented()` en `atlas.plugins` (generalizando el `recorded()` que ya envuelve las
herramientas de los agentes con `functools.wraps`) usado por `create_server` y por los
agentes, y la misma anotación en `Index.answer`; `[mcp] usage_log = "counts" | "full" |
"off"` (por defecto `counts`, sin texto de argumentos); una línea por llamada (fecha,
origen, herramienta, ms, resultado, `status` de la respuesta) en el workspace, mejor como
tabla de `atlas.db` que como `.jsonl` (dos procesos sobre el mismo fichero en Windows), y
junto a los tokens de 7.2. `atlas usage --since 30d` resume. Son las cifras para justificar
la herramienta al cabo de un mes.

### 8.9 Lo demás

- **Python 3.13 y la verificación estricta de certificados** (S, media): `ssl_context`
  construye el contexto con `create_default_context()` y no toca `verify_flags`
  ([connection.py:287](../atlas/llm/connection.py)); desde 3.13 ese contexto activa
  `VERIFY_X509_STRICT`, que rechaza certificados sin Authority Key Identifier, como los de
  algunos proxies que inspeccionan TLS, y `diagnose()` lo convierte en «pon la CA
  corporativa», que despista. Afecta a toda la rama distinta de `"system"`, incluido el
  valor por defecto en Windows. Quitar esa bandera en 3.13 en la rama del contexto propio
  (la cadena y el nombre se siguen verificando, como en 3.12), y una rama en `diagnose()`
  para ese mensaje que recomiende `ca_cert = "system"`. El incidente viene de un cliente
  hermano, no se ha reproducido con atlas.
- **`atlas chat`** (M, media): `run_agent` crea un `InMemoryRunner` y una sesión nuevos por
  pregunta, así que la segunda no sabe nada de la primera. Una clase `Conversation` en
  `atlas.agents.runtime` que construya el agente y la sesión una vez y exponga
  `ask(question)` (y `run_agent` pasa a usarla), y `atlas chat [--agent]` como bucle fino
  por stdin con órdenes sin modelo (`/trace`, `/matrix`, `/gaps`, `/sources`, `/save`). Para
  demostraciones ya existe `adk web examples/adk`. Solo encaja como bucle sin estado propio
  (sección 11).

## 9. Arquitectura, calidad y proceso

**Estado.** La base sigue el plan: paquetes por fuente con plugins, motor incremental
probado contra `kill -9`, secretos del entorno con prueba de red por ganchos de auditoría,
documentación en español vigilada por las pruebas, servidor falso con fallos inyectables,
`constraints.txt` y wheelhouse. Lo que no acompaña es la infraestructura del repositorio.

### 9.1 CI con Windows (M, alta)

No hay `.github`, ni `tox`, ni `pre-commit`; las pruebas se pasan a mano en el Mac, y los
usuarios son de Windows. Partes de la suite no pasarían allí: los compiladores falsos son
scripts con shebang y `chmod` (`test_build_tools.py`), hay un `symlink` y un `chmod(0o750)`
en `test_code_tools.py`. Propuesta: `.github/workflows/ci.yml` en cada push a `develop` y
`feature/*` y en cada tag: matriz `ubuntu` × {3.10, 3.13} y `windows` × {3.12} (la versión
de los puestos; tres celdas, no ocho): instalar con `constraints.txt`, generar las muestras,
`pytest -q`, `ruff`; en los tags, en Windows, el wheelhouse y `tools/check_install.py`, con
el wheelhouse como artefacto. Arreglar los tres puntos de la suite (variante `.cmd` para
los compiladores falsos, `skipif` para el symlink).

### 9.2 Rutas relativas al TOML (S, alta)

Incoherencia anotada en el plan: `workspace`, `agents dirs`, `eval sets`, `hints` y
`language_server_project` son relativas al TOML; `paths`, `roots`, `tool_path`,
`exchange.path`, `library` y `reference` son relativas a la carpeta de lanzamiento. Es la
clase de fallo silencioso («no procesa nada», 0 ficheros) que encontrará el editor que
arranca el servidor en otra carpeta. `Config.resolve(path)` usada en todos esos sitios,
conservando los identificadores de unidad como cadena relativa al TOML para no reprocesar
los workspaces existentes. Choca con una decisión recién documentada en la 0.2.0 (ver
sección 11): exige «Al actualizar» y reescribir la plantilla, la guía y el README de MCP.

### 9.3 Los pasos del pipeline fuera de `cli.py` (M, media)

`cli.py` (1114 líneas) construye los cinco pasos del motor (líneas 65 a 550) además de las
22 acciones; las tres constantes de versión viven allí; los tres pasos de índice crean cada
uno su `Index`, su `IndexStore` y su cliente de embeddings; y los ejemplos de librería que
acreditan la meta «importable» hacen `from atlas.cli import run`. Propuesta: `atlas/steps.py`
con `build_steps(ctx)` y `run(cfg) -> RunReport` (el `Runner(cfg).run()` del plan, expuesto
como `atlas.run`), las versiones en sus paquetes con los mismos valores, un `Index` perezoso
compartido en el contexto, y `atlas.cli.run` como alias que solo imprime. Es también el
prerrequisito de `refresh` (3.3) y de `dry_run` (7.4).

### 9.4 WAL y una sola función de conexión (S, media)

Cuatro clases abren `atlas.db` con cuatro conexiones y los mismos pragmas, ninguna activa
`journal_mode=WAL` (comprobado), y el servidor promete que `atlas run` puede ir con él
levantado. En modo rollback cada escritura excluye a los lectores durante el commit; las
transacciones medidas son cortas, así que el daño hoy es de décimas de segundo, pero crece
con el tamaño. `atlas/db.py` con `connect(path)`: WAL, `synchronous=NORMAL`,
`busy_timeout`, `row_factory`, y el sitio de `user_version` (9.5); la usan `Store`,
`Ledger`, `IndexStore` (también para `reference.db`) y `GraphStore`; en una ruta UNC,
`DELETE` con aviso; `atlas.db-wal` y `-shm` documentados en seguridad.

### 9.5 Versión de esquema y migraciones (M, media)

`atlas.db` no tiene versión: cada módulo hace `CREATE TABLE IF NOT EXISTS` y la única
migración es un `ALTER TABLE` ad hoc. Hoy no se rompe nada (entre 0.1.0 y 0.2.0 ninguna
tabla cambió), pero la guía promete que atlas añade lo que falte y nada lo garantiza.
`SCHEMA_VERSION`, `PRAGMA user_version`, `MIGRATIONS` aplicadas en una transacción con
copia previa, error claro si el workspace es de un atlas más nuevo, la versión en `atlas
status`, y un `atlas.db` pequeño de la 0.1.0 en `tests/fixtures` que debe abrir y migrar.

### 9.6 `ruff` y `mypy` (M, media)

Reproducido: `ruff --select F` da 8 errores (imports sin usar, en su mayoría) y `mypy`
239 en 33 de 53 ficheros (`code/tools.py` 48, `graph/build.py` 33, `graph/knowledge.py`
24; por código, 73 `union-attr` y 64 `arg-type`). Las anotaciones están en todo el código,
pero nadie las comprueba. Configuración en `pyproject` con los `ignore` del estilo del
proyecto (nombres de una letra, lambdas asignadas, fakes en una línea), `overrides` que
ignoran los módulos aún sucios para ir limpiándolos paquete a paquete, `make lint` y
`make typecheck`, y los dos en CI.

### 9.7 Lo demás

- **API pública y la capa `llm` sin `print` ni `sys.exit`** (M, media): 10 ficheros sin
  `__all__`, 7 `sys.exit` en funciones de librería de `connection.py` (más el modo
  independiente con su propio `connection.toml`, unas 250 líneas que nadie usa:
  `legacy/conexion.py` lo conserva), y los avisos del limitador por `print`, que
  `log_level` no gobierna. `ConfigError` en lugar de `sys.exit`,
  `logging.getLogger("atlas.llm")`, `__all__` en los diez, y `docs/api.md` con la superficie
  que se promete.
- **Registro de cada ejecución en el workspace** (S, media): `logging.basicConfig` a
  stderr y nada se guarda; tras una ingesta nocturna no hay dónde leer qué falló más allá de
  los 500 caracteres del libro de registro. `[project] log_dir` con un fichero por día
  (no `RotatingFileHandler`: `run` y `serve` coinciden sobre el workspace y en Windows no se
  puede renombrar un fichero abierto por otro proceso), borrado por antigüedad. Junto con
  el registro de uso (8.8) y los tokens (7.2): un solo registro con vistas, no tres.
- **Consola UTF-8 y saltos de línea** (S, media): con la salida redirigida en Windows, un
  texto de requisito con `→` o `≤` mata la acción (`cp1252`); `reconfigure(encoding="utf-8",
  errors="replace")` en `main` y `setx PYTHONUTF8 1` en la guía para lo que no pasa por
  ahí; `writer.py` escribe 19 ficheros con `write_text` sin `newline`: con `newline="\n"`,
  un workspace copiado entre sistemas no reprocesa todo.
- **Pruebas** (S, baja): medir primero (`--durations=15`; no hay cifra); marcador `slow`
  para los ejemplos, el proyecto grande y el `kill -9`; el servidor falso como fixture de
  sesión con `/reset` (hay pruebas que usan contadores absolutos). El README dice 203
  pruebas; hay 212 sin modelo y 219 con las de Ollama.
- **Claves muertas** (S, baja): `[sources.code] summaries` se quita del esquema. Para
  `[sources.doors] attributes`, una sola decisión: o se quita (hoy `attribute_columns` y el
  `mapping.toml` del exportador ya eligen qué atributos entran), o pasa a significar «los
  atributos que atlas enseña y por los que filtra» (5.6 y 6.4), en el mismo cambio.
- **Guía de contribución y limpieza** (S, baja): gitflow, convención de mensajes
  (`tipo(ámbito): resumen`), lista de release (`__version__`, «Al actualizar», cifras del
  README, `constraints.txt`, `check-install`, tag, borrar ramas). Hoy: 36 ramas fusionadas
  sin borrar, 8 worktrees cerrados, `firebase-debug.log` (ignorado) y un zip sin seguimiento
  en `legacy/`, `constraints.txt` con cabecera 0.1.0.

## 10. Lo que ningún análisis cubría

Del análisis de completitud, con todos los hallazgos delante. Por orden de valor.

### 10.1 Cachés compartidas entre puestos (M, alta)

«Procesar una vez» se cumple por workspace, no por equipo: cada desarrollador que haga
`atlas run` sobre el proyecto real repite las ~2000 secciones con `analysis = "llm"` y los
~20 000 embeddings contra el gateway (horas por puesto y N veces el coste). Las dos cachés
ya son compartibles por diseño: la de análisis de secciones se indexa por texto, modelo,
idioma y versión del prompt (sin rutas), y la de vectores por modelo, prefijo y texto; pero
viven dentro de un workspace con cerrojo de un solo proceso. Propuesta: `atlas cache export
FICHERO` e `import` (un SQLite con las filas de `embed_cache` y los JSON de
`docs/.cache`, con cabecera de versión, modelos y fecha) y, opcional, `[project]
shared_cache = "//servidor/atlas/tms-cache"` como caché secundaria de solo lectura
(lectura en `cache_get` y `_read_cache`, escritura siempre local); nunca un `atlas.db`
compartido. Medir: `run` → `export` → workspace nuevo → `import` → `run` con 0 peticiones
de chat y 0 de embeddings.

### 10.2 Fuentes que no pueden salir al gateway, y registro de lo enviado (M, alta)

La política es «solo al gateway», pero en un proyecto con DOORS y Ada hay documentos (del
cliente, con control de exportación) que tampoco pueden ir al gateway, cuya retención
«depende de la política del gateway, no de atlas». Hoy `[[sources.docs]]` solo controla
`analysis`; los embeddings son de todos los registros y las preguntas amplias leen
documentos enteros: no hay forma de tener un documento en el grafo y en la búsqueda léxica
sin que ningún texto suyo salga del puesto. Propuesta: `model = "full" | "none"` por fuente
de documentos, por módulo de DOORS y en `[sources.code]`: con `none`, `analysis` se fuerza
a `regex`/`none`, los registros se indexan sin vector (`replace_document` ya admite
registros sin vector), la lista densa los omite y la léxica y la de IDs los dan igual;
`answer` y las herramientas con modelo excluyen esas secciones del prompt y las devuelven
como `local_only` en `sources`; las herramientas MCP y la CLI, que no salen del puesto, las
dan enteras. Y un apartado en `atlas status` con lo enviado por modelo (secciones y textos,
por fuente), para poder responder «qué ha ido al gateway».

### 10.3 El puente entre `atlas develop` y el editor (M, alta; rumbo)

Lo que hizo funcionar a gemma no es el agente (ADK más instrucciones) sino el juez
determinista: tests protegidos, `check_code` tras cada cambio, deshacer, `advances`, rondas
e informe. Pero ese juez solo funciona con agentes ADK propios (`_run_once` → `run_agent`),
y el servidor MCP no lo ofrece: desde OpenCode o VS Code el ingeniero tiene las
herramientas de conocimiento pero no «qué tests pasan, cuál avanzó y qué rompí». Choca con
el no-objetivo del plan de no sustituir al editor. Propuesta: (a) en el plan de `develop`,
`agent_command = ["opencode", "run", ...]` (o `claude -p`) como alternativa a `agent`:
`Developer` lanza ese comando en la carpeta de trabajo (o en la copia del intento) con
`atlas serve` como MCP del editor, y conserva `check`, tests, `protected` (comprobado por
hash tras cada ejecución, con deshacer), `advances`, rondas, reinicios e informe; (b) dos
herramientas MCP de solo lectura, `run_tests(plan)` (por test: pasa, progreso, salida) y
`judge_changes()` (qué cambió, qué compila, qué test avanzó o retrocedió), dentro del perfil
`editor` y solo si se piden, porque ejecutan código. Es el movimiento de rumbo más
coherente con los principios: atlas aporta el juez y el conocimiento; el agente lo pone
quien quiera.

### 10.4 La aceptación del nivel de modelo: pruebas contra el gateway y un conjunto de evaluación generado (M, alta)

Las siete pruebas con modelo leen `atlas.dev.toml` a fuego y solo existe el marcador
`ollama`: no hay manera de pasarlas contra el gateway, que es lo que probará si
`gemma-4-26b` llama a las herramientas con proxy, CA y cupo reales. Y `atlas eval` solo
acepta casos escritos a mano: para el proyecto real nadie ha escrito ninguno. Propuesta:
`ATLAS_TEST_CONFIG` (por defecto `atlas.dev.toml`) y marcador `model` (con `ollama` como
alias), `make test-gateway`, y el fixture salta si `atlas check` falla. Y `atlas eval
--generate examples/eval/<proyecto>.yaml --n 50`: casos deterministas desde `atlas.db` y el
grafo (búsqueda por el ID exacto de requisitos al azar y por el título de secciones, `trace`
de cada requisito con código esperando su código y sus tests, `impact` de subprogramas con
llamadores), solo recuperación y grafo, sin casos de agente; es la medida de regresión del
proyecto real desde el primer día.

### 10.5 El proyecto GPR como fuente de verdad del código Ada (M, media)

Un proyecto Ada real tiene uno o varios GPR (`Source_Dirs`, `Object_Dir`, variables de
escenario, `with` de otros proyectos); atlas pide `roots`, `exclude`, `check_command`,
`test_command` y `tool_path` a mano, documenta que «un proyecto con GPR usaría gprbuild»
sin ayudar a hacerlo, y para el servidor de lenguaje escribe su propio GPR mínimo.
Propuesta: `[sources.code] project = "tms.gpr"` y `project_vars`: un lector sin
dependencias devuelve `source_dirs`, `object_dirs` y los proyectos `with`; `code_step` toma
de ahí `roots` (si no se dan) y añade los `object_dirs` a `exclude`; el servidor de lenguaje
usa ese fichero; los comandos por defecto pasan a `gprbuild -P`. `doctor` imprime las raíces
derivadas y cuántos ficheros ve.

### 10.6 ReqIF como segundo camino a la base de intercambio (M, media)

El exportador validado necesita Windows, el cliente de DOORS, pywin32 y permiso para
ejecutar DXL por COM; en muchos proyectos la administración de DOORS solo entrega ReqIF, o
un ReqIF viene del cliente. El README de `atlas.doors` dibuja «ReqIF, hoja de cálculo →
requirements.sqlite» pero no existe ningún relleno distinto de `tools/doors_export`.
`tools/reqif_import/reqif_to_exchange.py` con `xml.etree`: SPEC-OBJECT → requisito (con la
correspondencia de atributos de `mapping.toml`), SPEC-HIERARCHY → padre y orden,
SPEC-RELATION → enlace tipado, SPECIFICATION → módulo; escribe con `ExchangeWriter` y
reutiliza los avisos de `publish.py`.

### 10.7 Gobierno de dependencias: la licencia de pymupdf, aviso de terceros y `pip-audit` en el proceso (S, media)

El extractor de PDF por defecto es pymupdf, AGPL-3.0; el CHANGELOG lo deja en «conviene
confirmar que la política de la empresa la admite» y nadie lo ha resuelto. No hay inventario
de licencias ni aviso de terceros en el wheelhouse, y `pip-audit` es una frase en
seguridad.md, no un paso. `tools/licenses.py` (sobre el entorno de `constraints.txt`) →
`THIRD_PARTY_NOTICES.md` y un SBOM mínimo copiados al wheelhouse; `make audit` en la lista
de release y en CI; `atlas plugins` imprime la licencia de cada extractor; y la decisión
escrita en `docs/seguridad.md` (pymupdf por defecto con su alternativa, o un extra
separado).

### 10.8 Una guía de uso para el ingeniero y un recorrido reproducible (S, media)

Toda la documentación es para quien instala, configura o desarrolla atlas; no hay una guía
de uso: qué preguntar a `search`, a `ask`, a `ask_docs` con `whole` o a un agente; cómo leer
`trace`, `matrix` y `gaps`; qué significan `values_differ`, `cannot_tell`, «no sources» o
`not_advanced`; qué puede y qué no un modelo pequeño con 30 s por vuelta. `docs/uso.md` por
tareas («quiero saber qué pide un requisito», «qué rompo si cambio X», «qué falta por
trazar», «escribir código para un requisito desde el editor») y `examples/walkthrough.py`
(`make demo CONFIG=...`) que recorre las muestras contra la configuración dada imprimiendo
cada salida y midiendo tiempos: el guion del curso y la prueba de humo en Windows.

### 10.9 Instrucciones dentro de los datos (S, media)

`docs/seguridad.md` deja el riesgo abierto, mitigado solo porque los agentes de serie no
escriben. Pero los que sí escriben (`implementer`, los de `develop`) reciben texto de
requisitos, de documentos, comentarios del código, la salida de los tests y `code_at`, y
en `AtlasLlm._messages` los resultados van como mensajes `tool` sin ninguna marca. Envolver
cada resultado de herramienta en un marco explícito, una frase fija en los agentes de serie
y de ejemplo (lo devuelto por herramientas, documentos y requisitos son datos, nunca
instrucciones), un aviso determinista no bloqueante cuando un texto devuelto contiene frases
imperativas típicas dirigidas al modelo, y una prueba en `test_develop.py` con un requisito
envenenado para medir si el agente de pega lo sigue.

### 10.10 PDF escaneados o sin texto (S, media)

Una página de imagen devuelve una cadena vacía sin decir nada; el documento queda «ok» con 0
secciones útiles y nadie lo nota hasta que una pregunta no encuentra nada. `pages_without_text`
en `preview` y `process_document`; si superan la mitad, aviso «scanned PDF: N of M pages
have no text» y la unidad en `error` (reintentable), visible en `atlas sections`, `status`
y `doctor`. Opcional, `[[sources.docs]] ocr_command` ejecutado sobre una copia antes de
extraer, con caché por hash del PDF.

## 11. Decisiones que conviene tomar antes de empezar

Las propuestas anteriores no son todas compatibles entre sí tal como están. Estas son las
que hay que decidir primero, con mi recomendación.

- **Número de herramientas.** Este informe propone una docena de herramientas nuevas
  (`context_at`, `changes`, `refresh`, `similar_requirements`, `outline`, `read_subprogram`,
  `run_tests`, `judge_changes`...) contra un principio de cinco a ocho. Recomendación: los
  perfiles de 3.7 van antes que cualquier herramienta nueva, y cada una nace dentro del
  perfil `editor` (que se queda en unas doce) o sustituye a otra.
- **Rutas relativas al TOML (9.2) frente a la decisión de la 0.2.0.** El CHANGELOG fija
  `tool_path` relativo a donde se lanza atlas, y la plantilla, la guía y el README de MCP
  dicen que las rutas de las fuentes son relativas al cwd y por eso el editor arranca el
  servidor en la carpeta del proyecto. Recomendación: cambiarlo igualmente en la 0.3, antes
  de la aceptación en Windows, con «Al actualizar» y los tres textos reescritos; el fallo
  silencioso de 0 ficheros es peor que el cambio.
- **`refresh` (3.3) frente a «un proceso por workspace».** Un servidor que reanaliza y
  escribe en `atlas.db` rompe el modelo de escritor único salvo que pase por el `Runner`
  con el cerrojo del workspace y ceda si un `atlas run` lo tiene. Recomendación: así, y con
  WAL (9.4) antes; sin cerrojo, el servidor solo marca `stale`.
- **El runtime de los agentes antes que el streaming.** El streaming y los tiempos viven en
  `AtlasLlm`, así que se pueden hacer bajo ADK; un bucle propio (7.6) es otra decisión.
  Recomendación: streaming sobre `AtlasLlm` y `JsonLlm` primero; el bucle propio queda como
  rumbo, y solo si el pin de ADK empieza a doler.
- **`stream` por defecto.** El historial del gateway (501 sin esquema, plantillas de chat que
  cerraban el turno) desaconseja asumirlo. Recomendación: lo que mida `atlas probe`.
- **Stemming y el español (5.4).** `porter` es solo inglés; los resúmenes siguen `lang`
  (español por defecto en la plantilla) y la evaluación tiene preguntas en español.
  Recomendación: stemming solo sobre los fragmentos de documentos en inglés, o tokenizador
  por idioma, y medir por tag con 5.2 antes de cambiar el valor por defecto.
- **La política de escritura de `develop`.** Parches (4.9) y commits (4.1) son dos
  políticas; los commits del agente chocan con el gitflow del proyecto (un commit por parte,
  mensajes del desarrollador). Recomendación: parches siempre; commits solo opcionales y en
  una rama propia del plan.
- **Paralelismo y cupo.** Encargos en paralelo (4.10) más `attempts` y planificador suben a
  más de 12 peticiones por minuto de las 28, compartidas con `run` y `serve`.
  Recomendación: después de los presupuestos (4.2, 8.3) y del cupo compartido (7.3).
- **Una sola noción de presupuesto** (peticiones y minutos) en `atlas.llm`, servida a `run`
  (8.3), `develop` (4.2) y las preguntas amplias (5.3).
- **Un solo registro en el workspace** para uso, tokens y ejecución (7.2, 8.8, 9.7), con
  vistas, en vez de tres ficheros.
- **`attributes`: una semántica.** O se quita, o significa «lo que atlas enseña y por lo que
  filtra» (5.6, 6.4), en un solo cambio.
- **El CSV de la matriz.** Cambiar el delimitador y el BOM por defecto rompe lo que hoy lo
  lea (`tests/test_cli.py`): una sola entrada en «Al actualizar».
- **`atlas chat`.** Solo como bucle fino sobre `Conversation`, sin estado propio; roza el
  no-objetivo de sustituir al editor.
- **Transporte.** Stdio se queda, y con él la garantía «no abre ningún puerto» de
  `docs/seguridad.md`.

## 12. Hoja de ruta propuesta

**0.3, «el editor»** (dos a tres semanas de trabajo): lo que cambia lo que un agente de
edición puede hacer con atlas, y lo que impide perder una tanda.

- MCP: 3.7 (perfiles, primero), 3.1 (fichero y línea), 3.2 (instrucciones), 3.4
  (ejemplos), 3.6 (recortar entero, `signatures`, Spec de `Library`), 3.12 (pruebas); 9.3,
  9.4 y 3.3 (los pasos fuera de la CLI, WAL, `stale` y `refresh`).
- develop: 4.2 (validación y presupuesto), 4.5 (procesos y `.exe`), 4.4 (`outline`,
  `read_subprogram`), 4.1 (`--resume` y Ctrl+C), 4.9 (1) (parche por encargo), 4.6 (A) (los
  agentes medidos), 4.10 (a) (rondas con intentos).
- Base: 9.2 (rutas), 9.1 (CI), 7.1 (salida estructurada), 7.6 (a) (una parte que falla),
  5.1 (enrutador), 8.1 paso 0 (el aviso de las raíces).

**0.4, «el proyecto real»**: lo que la aceptación va a pedir. Si la aceptación es lo
inmediato, este bloque va antes que el anterior: el análisis de completitud recomienda
`doctor`, `estimate` y presupuestos, cachés compartidas y las pruebas contra el gateway
antes que el crecimiento del servidor MCP y de `develop`.

- 8.1 (`doctor`), 8.2 (guion), 7.4 y 8.3 (`estimate`, presupuesto y progreso), 8.4 (CSV),
  8.5 (`trace` legible y `--json`), 8.6 (configuración de producción), 10.1 (cachés
  compartidas), 10.4 (pruebas contra el gateway y evaluación generada), 10.8 (guía de uso y
  recorrido).
- 6.1 (semántica de enlaces), 6.3 (IDs), 6.5 (`gaps` y `ancestors`), 6.4 (a) y (b) (fecha
  de exportación y columnas de la matriz), 6.2 (a) (tareas y operadores), 10.5 (GPR).
- 5.2 (evaluación por tipo), 5.4 (léxico), 5.6 (filtros), 5.7 y 8.7 (citas y `status`),
  10.2 (fuentes que no salen al gateway), 7.2 y 8.8 (uso), 4.3 (encargos anclados a
  requisitos), 4.6 (B) (banco de encargos), 10.9 (instrucciones en los datos).
- 9.5 (esquema), 9.6 (`ruff`, `mypy`), 10.7 (licencias y `pip-audit`), el resto de 9.7.

**Rumbo**, cuando haya medida que lo justifique: 10.3 (el juez de `develop` con un agente
externo y por MCP), 7.6 (b) y (c) (streaming), 4.10 (b) (encargos en paralelo), 6.6
(referencias exactas con el ALS), 5.5 (tablas), 10.6 (ReqIF), 4.9 (2) (modo propuesta), 4.7
(revisor con modelo), 8.9 (`atlas chat`), 10.10 (OCR), 7.6 (un bucle propio).

Un solo desarrollador puede sostenerlo si cada tema deja algo medible en `atlas eval` o en
una tanda de `develop`, como hasta ahora.

## 13. Lo descartado o aplazado, y por qué

- **Los diagnósticos del servidor de lenguaje en el bucle de edición.** Cada edición lanza
  dos `gcc` (sintaxis y `check_code`), pero nadie ha medido lo que cuestan frente a la
  vuelta del modelo (20 s a 10 min); en la demo son décimas de segundo. Antes de nada,
  sumar en el informe los segundos de compilación por paso; si superan el 10 %, entonces
  `check_on_edit = "lsp"`.
- **Un servidor MCP compartido por HTTP.** Sin autenticación corporativa ni TLS, y
  hablaría del código del servidor, no del de cada puesto. Se documenta la decisión (3.11).
- **Ámbito positivo de escritura por agente.** `protected` ya cubre los ficheros nuevos.
- **Borradores de pistas escritos por el modelo.** Lo que falta es «qué cambio quitó el
  error», que sale sin modelo del `.calls.jsonl`; la redacción la hace la persona.
- **Pedir al modelo `answered`/`missing` en JSON.** El estado de la respuesta sale de las
  citas sin ninguna petición; meter la respuesta larga en un JSON empeora.
- **Un hilo temporizador para el presupuesto de `atlas run`.** No termina limpio; la
  comprobación va en `process()` antes de cada unidad.
- **Una tabla de historial completo de la trazabilidad por ejecución.** Guardar los IDs que
  cambian en cada ejecución da casi lo mismo con una fracción del tamaño.
- **`python-docx` ahora, y `openpyxl` antes de arreglar el CSV.** `python-docx` arrastra
  `lxml`; el Markdown ya se pega en Word.
- **Un analizador de C++ de ejemplo.** No hay quien lo use; sí los cambios aditivos de la
  interfaz que el analizador Ada necesita igualmente.
- **Caché de respuestas de `ask_atlas` y de reescrituras.** Las preguntas no se repiten
  literalmente; el ahorro medido es una petición de pocos segundos.
- **Pesos de RRF y escala de la búsqueda densa.** Primero medir con `atlas eval` y sobre
  `make large`; las cifras del análisis no estaban en los README.
- **`writes` por agente, `--tasks` en `develop`, un tipo de plugin para escribir informes.**
  Mecanismo de más para lo que hay: `protected`, un plan recortado y una función de tabla.

## 14. Tareas

Las unidades de trabajo de la hoja de ruta (sección 12), una por rama `feature/` desde
`develop`, en el orden en que conviene hacerlas. Cada una está descrita con detalle en las
secciones que cita; «depende de» dice qué tiene que estar ya en `develop`. El 17 de octubre
de 2026 estaban en marcha las dos primeras.

| # | Tarea | Secciones | Depende de |
|---|---|---|---|
| 1 | 0.3 MCP: contexto por fichero y línea, instrucciones y perfiles | 3.7, 3.1, 3.2, 3.4 | — |
| 2 | 0.3 develop: validar el plan, procesos y reanudación | 4.2, 4.5, 4.4, 4.1, 4.9 (1), 4.10 (a) | — |
| 3 | Base 0.3: pasos fuera de cli.py, WAL y conexión única | 9.3, 9.4 | — |
| 4 | MCP: grafo fresco (`stale`, `refresh`) y `changes(paths)` | 3.3, 3.5 | 1, 3 |
| 5 | MCP: forma de las salidas y `build_context` con foco | 3.8, 3.6 | 1 |
| 6 | MCP: recursos, prompts, progreso y pruebas del servidor | 3.9, 3.10, 3.12 | 1 |
| 7 | Rutas de atlas.toml relativas al fichero | 9.2, 11 | — |
| 8 | CI con Windows, ruff y mypy | 9.1, 9.6 | — |
| 9 | Capa de modelo: JSON por escalones, `finish_reason` y servidor falso | 7.1, 7.6 | — |
| 10 | Confianza: enrutador al grafo, `status` y citas comprobadas | 5.1, 5.7, 8.7 | — |
| 11 | `atlas doctor`, aviso de raíces, perfil gateway y TLS 3.13 | 8.1, 8.6, 8.9 | — |
| 12 | `atlas estimate` y presupuesto de `atlas run` | 7.4, 8.3 | 3 |
| 13 | Entregables: CSV para Excel, `trace` legible y `--json` | 8.4, 8.5 | — |
| 14 | DOORS: semántica de enlaces, `gaps` y `ancestors` | 6.1, 6.5 | — |
| 15 | DOORS: fecha de exportación, matriz con atributos y `values_differ` | 6.4, 6.6, 5.6, 9.7 | — |
| 16 | IDs de requisito y analizador Ada: tareas, operadores, sobrecargas | 6.3, 6.2 | — |
| 17 | Índice: stemming, sinónimos, IDs con variantes y prefijos | 5.4, 5.8, 11 | — |
| 18 | Índice: filtros por metadatos y atributos de DOORS | 5.6 | 15 |
| 19 | Preguntas amplias: presupuesto, plan previo y por secciones | 5.3 | 9 |
| 20 | Evaluación por tipo, caso `ask`, generación y pruebas contra el gateway | 5.2, 10.4 | — |
| 21 | Cachés compartidas entre puestos | 10.1 | — |
| 22 | Fuentes que no salen al gateway y registro de lo enviado | 10.2 | — |
| 23 | Uso, tokens y registro de ejecución en el workspace | 7.2, 8.8, 9.7 | — |
| 24 | Cupo entre procesos, concurrencia y contexto del modelo | 7.3, 7.5, 7.6 | 9, 23 |
| 25 | develop: encargos anclados a requisitos y agentes medidos | 4.3, 4.6 | 2, 14 |
| 26 | develop: reglas de código, borradores de pistas y ficheros sueltos | 4.7, 4.8, 4.11 | 2 |
| 27 | Esquema de atlas.db versionado y migraciones | 9.5 | 3 |
| 28 | API pública, logging sin `print`, consola UTF-8 y guía de contribución | 9.7 | — |
| 29 | Guion de aceptación, guía de uso, recorrido reproducible y FAQ | 8.2, 10.8 | — |
| 30 | Gobierno de dependencias: licencias, SBOM y `pip-audit` | 10.7 | 8 |
| 31 | Documentos: PDF escaneados, versión y autoridad, tablas (condicionada) | 10.10, 6.6, 5.5 | — |
| 32 | GPR como fuente de verdad del código Ada, y ReqIF | 10.5, 10.6 | — |
| 33 | Instrucciones dentro de los datos | 10.9 | — |
| 34 | Medidas: `matrix`/`gaps`, búsqueda densa, RRF, `compare_models` | 6.6, 5.8, 7.6 | 20 |
| 35 | Rumbo: el juez de `develop` para un agente externo y por MCP | 10.3 | 2, 4 |
| 36 | Rumbo: streaming con tiempo de inactividad | 7.6, 11 | 9 |
| 37 | Rumbo: encargos de `develop` en paralelo | 4.10 (b) | 2, 24 |
| 38 | Rumbo: referencias exactas con el ALS y la interfaz `CodeUnit` | 6.6 | 16 |
| 39 | Rumbo: modo propuesta, revisor con modelo y `atlas chat` | 4.9 (2), 4.7, 8.9 | 2, 26 |
