# atlas.index — índice y búsqueda

Indexa los documentos procesados y los requisitos de DOORS, busca combinando IDs, vectores
y texto, y responde con citas. Viene de `legacy/doc_rag.py`.

| Fichero | Qué tiene |
|---|---|
| `records.py` | los registros de un documento (resumen, secciones, fragmentos, requisitos...) y de un módulo de DOORS |
| `embeddings.py` | `Embedder`: vectores por la conexión de `[embeddings]` |
| `store.py` | `IndexStore`: registros, vectores, FTS5 y caché de vectores en `atlas.db` |
| `search.py` | búsqueda híbrida y respuesta citada |
| `tools.py` | `DocTools`: las herramientas de documentos y requisitos para agentes y MCP |
| `__init__.py` | `Index`, `index_document`, `index_module` y `embed_missing` |

## Qué se indexa

El paso `index.documents` indexa cada documento procesado a varias granularidades:
- resumen global;
- resúmenes de sección;
- fragmentos de unos `chunk_chars` caracteres con solape, cada uno con la ruta y el
  resumen de su sección;
- requisitos;
- definiciones;
- pendientes.

Los requisitos de DOORS también se indexan (paso `index.requirements`): cada módulo es un
documento más del índice, con el nombre del módulo. Cada requisito es un registro de tipo
`requirement` con los encabezados que tiene encima como ruta, y cada objeto de información,
un fragmento. Los atributos de `[sources.doors] attributes` (`status` y `verification` por
defecto) van en una última línea del texto del requisito (`status: Approved; verification:
Test`): pesan en la búsqueda y el modelo los ve con el requisito. Así, `search` encuentra un requisito aunque no aparezca en ningún documento, y
su cita queda como `SRS_TMS › 1.1 Deletion (requirement)`. `get_requirement` sigue dando el
texto oficial aparte, y en sus documentos no repite el de DOORS.

El código también se indexa (paso `index.code`, tipo `code_doc`). Cada unidad de código es
un documento más: un paquete de Ada, con su especificación y su cuerpo. Cada subprograma,
prueba, tipo, variable y constante es un registro con su nombre completo en `ref`
(`tracks.delete_track`) y, como texto, el comentario de cabecera (el del `.ads` si lo hay),
los requisitos que cita y su código (los primeros `chunk_chars` caracteres). Su cita queda
como `tracks › tracks.delete_track · tracks.adb:11-15 (code_doc)`. Así, «which subprogram
removes a track from the database» encuentra `tracks.delete_track` aunque no use sus
palabras. Un fichero cambiado solo reindexa su unidad, y de ella solo se embeben los textos
que cambian.

`[index] kinds` elige qué se indexa; sin `code_doc`, el código no entra en el índice.

Cada registro lleva además su `meta` (JSON en la columna `meta` de `index_records`): lo que
permite acotar una búsqueda.

| Registros | `meta` |
|---|---|
| requisitos y objetos de información de DOORS | `module`, `chapter` (el primer encabezado de su ruta) y, en los requisitos, los atributos de `attributes` que tengan |
| fragmentos, resúmenes de sección y requisitos de un documento | `chapter`: el primer elemento de su ruta (`2 Alarms`) |
| `code_doc` | `kind` (`subprogram`, `test`, `type`, `variable` o `constant`) y `file` (`tracks.adb`) |

Las claves van en minúsculas y los valores, como texto. El resumen global, las definiciones
y los pendientes no tienen `meta`.

## Embeddings

Todo va en `atlas.db`: registros, vectores, índice de texto y una caché de vectores por
modelo, prefijo y texto, que evita embeber dos veces el mismo texto. La clave de la caché es
un hash del modelo, su prefijo (con el título del documento, si el prefijo lo lleva) y el
texto, y cada fila guarda además su modelo, para exportarla a otros puestos (`atlas cache
export`, ver [atlas/cache.py](../cache.py)). Con `[project] shared_cache`, lo que la caché
local no tiene se busca en la carpeta compartida y se copia a la local. Un documento reanalizado solo
embebe sus textos nuevos. Cambiar `[embeddings] model` reindexa todo sin volver al LLM, y
la búsqueda nunca mezcla vectores de dos modelos.

Los prefijos de consulta y de documento salen de `[embeddings] family` (`embeddinggemma`,
`nomic`, `e5` o `none`) o, si no se da, del nombre del modelo; `query_prefix` y
`document_prefix` literales sustituyen a los de la familia. `prefixes(cfg)` los resuelve sin
crear cliente y avisa en el registro si el nombre no dice ninguna familia: un gateway puede
publicar `embeddinggemma` con otro nombre, y entonces los vectores se calculaban sin prefijo
sin que nadie lo supiera. Con `embeddinggemma`, el prefijo de documento lleva el título del
documento (`title: HMI Manual | text: …`) en vez de `title: none`. Los prefijos efectivos
entran en la huella de los tres pasos de índice, y `atlas check` los imprime.

Antes de indexar, cada paso junta los textos que faltan en la caché de todos sus
documentos (o módulos) y los embebe en lotes llenos de `[embeddings] batch`, varios a la
vez (`embed_missing`). Con un límite de peticiones por minuto, eso son menos peticiones que
embeber documento a documento: en el proyecto grande, 189 en vez de 207 para 12 060 textos
con lotes de 64. Un lote que falla no para los demás; lo que falta se embebe en la
siguiente ejecución.

Los vectores de las consultas de una búsqueda van a la misma caché (por el modelo, su
prefijo de consulta y el texto): una consulta repetida no hace ninguna petición a los
embeddings. Una segunda pasada de `atlas eval`, o la misma pregunta de un agente dos veces,
no gasta cupo.

## Fuentes locales

Un documento de una fuente con `model = "none"` (un módulo de DOORS de `local_only`, o el
código con `[sources.code] model = "none"`) se indexa sin vectores: ninguno de sus textos
va al modelo de embeddings, y la tabla `index_local` lo marca como local. La lista
densa de una búsqueda no lo ve; la de IDs y la léxica, sí. Así sigue saliendo en
`atlas search` y en la herramienta `search` por sus IDs y sus palabras.

`answer` lo deja fuera de todo lo que manda al modelo: los pasajes, los documentos que lee
enteros y la lista de documentos que ve la reescritura. Lo encontrado de él vuelve en
`sources` con `local_only` y `n` = 0, sin extracto: la persona sabe que hay más y dónde.
Si todo lo encontrado es local, no hay petición de respuesta (`LOCAL_ONLY_ANSWER`).
`DocTools` lo da entero al editor; en el contexto de un agente (`for_model`), cuyos
resultados van al modelo, lo omite y dice qué omitió. Cambiar `model` de una fuente
reindexa sus documentos.

## Búsqueda

Una búsqueda combina tres listas con Reciprocal Rank Fusion:
- los IDs exactos que aparecen en la consulta, y las definiciones del glosario de sus siglas
  (de 2 a 6 mayúsculas: «TMF»);
- la similitud de los vectores;
- BM25 sobre el mismo texto (FTS5 de SQLite).

Un ID escrito de otra forma se busca como está en el índice: «srs0004», «SRS_0004», «SRS 0004»
y «srs-4» dan lo mismo que «SRS-0004». `ids_in` toma los prefijos de los requisitos indexados
(`IndexStore.id_prefixes`, «SRS» con su anchura de dígitos más común, 4), une el prefijo, un
espacio, nada o un guion bajo y el número, rellena con ceros a esa anchura, aplica
`[sources.doors.convention] id_normalize` y se queda solo con los IDs que existen; la consulta
se busca entonces con el ID canónico en su lugar.

### Léxico

BM25 busca cada palabra tal cual. Con el tokenizador de serie, «deleting» no encuentra
«Deleted tracks» ni «log», «logs». `[index] fts_tokenizer = "porter unicode61
remove_diacritics 2"` reduce cada palabra inglesa a su raíz (no casa «verifies» con
«verification»). Al abrir el índice, `IndexStore` compara el tokenizador de la tabla
`index_fts` con el pedido y, si difiere, la rehace desde `index_records` en una transacción:
segundos, sin embeber nada. `porter` es solo inglés y los resúmenes de sección siguen el
`lang` de su fuente: conviene con documentos en inglés, medido con `atlas eval` antes y
después (en las muestras, la léxica pasa del 60 % al 75 % de acierto@1, y la híbrida del 80 %
al 90 %; ver la [guía](../../docs/configuracion.md#index)).

`[search] synonyms` añade grupos de términos equivalentes del proyecto (`examples/synonyms.toml`):
una consulta con un término de un grupo hace una búsqueda léxica más, con los demás términos
del grupo, que entra en la fusión como una lista más. Es la búsqueda que queda cuando el
servidor de embeddings no responde.

Una búsqueda en todos los documentos da como mucho `[search] per_document` resultados (3)
de cada documento, módulo de DOORS o unidad de código mientras otros tengan alguno; si no
hay bastantes, completa con los demás. Cada elemento de una unidad de código repite su
nombre (`ada unit tracks > constant tracks.max_tracks`), y sin ese límite una unidad cuyo
nombre está en la pregunta ocupaba todos los puestos: en las muestras, «how are tracks
deleted» daba 8 elementos de `tracks` y ningún requisito. Con `document` no hay límite.
`[search] results` dice cuántos resultados (8).

`atlas ask` reescribe antes la pregunta en consultas de búsqueda. Después pasa al modelo
los mejores resultados, cada uno con su contexto (hasta `[search] context_chars`), y le pide
responder solo con esas fuentes y citándolas como `[n]`. El contexto (`IndexStore.neighbours`,
`[search] context_radius` registros a cada lado, 1) depende de lo encontrado:
- un fragmento: sus vecinos de la misma sección;
- un requisito o un objeto de información de DOORS: los registros de su sección que lo
  rodean en el módulo (se indexan en el orden del módulo, no en el de los IDs) y, si un
  documento dice el mismo ID, su pasaje con su cita. Antes llegaba la frase a secas;
- un resumen de sección: los primeros fragmentos de la sección. Antes, solo el resumen;
- un requisito o una definición de un documento: el fragmento de su sección que contiene su
  texto, con sus vecinos;
- lo demás (resumen del documento, código), él mismo.

## Citas comprobadas

Al final de cada respuesta, `check_citations` comprueba sus citas sin modelo: un `[n]` de
una fuente que no existe va a `out_of_range`; de cada frase con cita, los IDs y los números
con unidad (320 ms, 30 días, 95 %) se buscan, canonizados (minúsculas, sin puntuación, la
unidad con un solo nombre), en las fuentes que cita esa frase, y si alguno no está la frase
va a `unsupported` (`{claim, cited, missing}`). `sources` son solo las citadas; de pasajes, el
resto queda en `unused`. `atlas ask` imprime «n claims not supported by their sources» con
cada frase, y `ask_docs` lo dice en `warnings`.

## Estado de la respuesta

Un agente o un script tiene que distinguir una respuesta de una no-respuesta sin leerla.
Cada respuesta lleva, calculado sin modelo y sin pedirle JSON (`answer_status`):

- `status`: `not_found` si no había fuentes o la respuesta es la frase fija «Nothing indexed
  is related to the question.»; `answered` si cita al menos una fuente y ninguna frase sin
  cita tiene una cifra o un ID; `partial` en lo demás (una respuesta que dice lo que falta,
  o datos sin su fuente);
- `uncited`: las frases sin `[n]`;
- `consulted`: cuántas fuentes se le pasaron.

`atlas ask` imprime al final «status: partial (2 sentences without a source)» y sale con
código 1 en `not_found`. `ask_docs` devuelve los tres campos, y `ask_atlas` (el asistente,
que cita por ID y no por `[n]`) un `status` y un `uncited` frente a lo que devolvieron sus
herramientas. El asistente y los informes de impacto y cobertura tienen la instrucción de
decir que su respuesta es parcial cuando lo es.

## Preguntas de trazabilidad

«¿Qué prueba verifica SRS-0004?» no se contesta bien con pasajes: el índice gastaba dos
peticiones y solo lo encontraba si BM25 sacaba por casualidad el código que lo cita, mientras
`trace` lo da en milisegundos. Antes de buscar, `DocTools.route(pregunta)` mira, sin modelo,
si la pregunta nombra IDs de requisito (los de `[sources.doors.convention] id_pattern`, o
cualquiera que esté en DOORS o en el grafo) o elementos de código (una unidad, o un nombre
con `.` o `_`) y pregunta por su trazabilidad: pruebas (*test*, *prueba*, *verif…*),
implementación, padre, enlaces (*satisf…*, *link*, *enlace*), llamadas o impacto de un
cambio (*impact…*, *afecta*, *cambi…*). Entonces da fuentes deterministas:

- de cada requisito, su `trace` del grafo; sin grafo (o si no está en él), su texto oficial,
  padre, atributos y enlaces de DOORS; y su `impact` si la pregunta habla de cambios;
- de cada elemento de código, su `trace_code` y, si se pregunta por cambios, su `impact`.

`atlas ask` y `ask_docs` las pasan a la respuesta como las primeras fuentes numeradas,
delante de los pasajes (`extra_sources` en `Index.answer`): el modelo las cita como `[1]`,
`[2]`. Una pregunta con ellas no se reescribe (sus IDs se encuentran sin hacerlo), así que
cuesta una petición en vez de dos, y no se lee entera salvo con `--whole`. El requisito de
DOORS que ya va en una fuente de trazabilidad no se repite entre los pasajes.

## Preguntas amplias

Ocho pasajes no bastan para «resume los requisitos de SRS_TMS» o «explica cómo funciona el
registro»: en las muestras, la primera pasaba al modelo 8 de los 40 requisitos del módulo, y
la respuesta parecía completa sin serlo. Por eso, al reescribir la pregunta, el modelo dice
también si es amplia (un resumen, una función o un capítulo enteros, todos los requisitos o
valores de algo) y de qué documentos o módulos trata, de la lista de los indexados. Si es
amplia, `atlas ask` lee esos documentos enteros. Si no nombra ninguno, busca la pregunta
solo en los resúmenes de los documentos y de sus secciones (`overview` y `section_summary`),
que dicen de qué trata un documento o un capítulo entero mejor que unos pocos pasajes, y lee
los que más salen: hasta `[search] scope_documents` (3), con al menos la mitad de resultados
que el primero, y nunca unidades de código. Un módulo de DOORS no tiene resúmenes: solo se
lee entero si la pregunta lo nombra, con `--document`, o si ningún documento tiene
resúmenes (entonces deciden los resultados de la búsqueda de la pregunta):

- cada sección es una fuente: el texto de sus fragmentos sin el solape; en un módulo de
  DOORS, sus requisitos y objetos de información en su orden;
- si caben en `[search] scope_chars` (100 000 caracteres), una sola petición;
- si no, por partes de ese tamaño, `[search] part_workers` (4) a la vez: de cada una, el modelo toma notas de todo lo
  que importa a la pregunta, con sus citas, y una última petición las junta en la
  respuesta. Así ninguna parte se queda fuera, a cambio de más peticiones;
- si las notas tampoco caben en `scope_chars`, se juntan antes por grupos que caben, nivel a
  nivel, como el resumen de un documento: las citas de las notas son las de los documentos
  enteros, así que pasan los niveles sin cambiar, y al final se numeran 1, 2, 3… en el orden
  de las fuentes. La última petición siempre cabe: si dos notas no caben juntas, se recortan;
- si una parte falla (el servidor no contesta, se acaba el cupo), la respuesta se escribe con
  las demás y termina con la nota «part 2 of 4 failed: …», que también va en
  `failed_parts`; solo si fallan todas falla la pregunta.

Antes de pedir nada se calcula el plan: documentos, secciones, caracteres, partes y
peticiones, como mínimo (juntar las notas por niveles añade alguna), y minutos del cupo si hay `[network] requests_per_minute`. Con más de
`[search] max_parts` partes (12) no se lee nada: la respuesta es un error con el plan y los
capítulos de cada documento, para acotar la pregunta. `atlas ask --dry-run` imprime el plan
sin ninguna petición: elige los documentos sin el modelo (`--document`, los que la pregunta
nombra o una búsqueda léxica).

`--section` (o `section` en `ask_docs`) lee entera solo una sección y lo que cuelga de ella:
por su número o su título, con o sin número («2.1», «Logging»), por su ruta, título a título
(«Track management > Deletion»), o por su ID, el de un resultado de búsqueda («004») o, en un
módulo de DOORS, el del título («SRS-0910»: solo lo que está justo debajo). Con `--section`
se lee entera aunque la pregunta no parezca amplia. Si la sección no está, la respuesta es un
error con los capítulos de los documentos.

Las fuentes de la respuesta son las secciones que cita, y `scope` dice cómo se leyó
(`passages` o `whole`). `--whole` (o `whole` en `ask_docs`) lee enteros los documentos aunque
la pregunta no parezca amplia, y `[search] scope = "passages"` no los lee nunca.

Un proceso que vive mucho (el servidor MCP) ve lo que indexa otro: los vectores se vuelven
a leer cuando cambia la base.

`Index.search(..., dense=False)` busca solo con los IDs y BM25: no hace ninguna petición a
ningún modelo. `atlas eval` compara las dos formas (ver [atlas.agents](../agents/README.md)).
Sobre las muestras, con `embeddinggemma`, la híbrida pone lo buscado en primer lugar en el
80 % de los casos y entre los tres primeros en el 90 %; la léxica, en el 60 % y el 60 % (con
`porter`, la híbrida en el 90 % y el 90 %, y la léxica en el 75 % y el 75 %).

El código compite con los documentos. En las muestras, «show the tracks on the operator
screen» bajó de la primera a la cuarta posición al indexar el código, porque el código de
`tracks` habla de *tracks*. Para buscar solo en una clase de texto, `--kind` en `atlas
search` y `atlas ask`, `kind` en la herramienta `search` (o `kinds` en `Index.search`).

Con Ollama (`atlas.dev.toml`), indexar los tres documentos de ejemplo con `embeddinggemma`
lleva segundos. Una respuesta de `gemma4:12b` tarda de 50 s a 2 min, según la longitud del
contexto y de la respuesta.

## Filtros

Lo primero que pide una revisión es acotar: los requisitos aprobados, los que se verifican
por prueba, un módulo, un capítulo. Las búsquedas y las respuestas se acotan por la `meta`
de los registros:

| Filtro | Herramienta `search` | `atlas search` y `atlas ask` | Qué deja |
|---|---|---|---|
| atributo | `attribute = "status=Approved"` | `--attribute status=Approved` | los registros con ese valor; varios pares con «;» (`status=Approved;verification=Test`) o, en la línea de comandos, repitiendo la opción. Vale cualquier clave de la tabla de arriba: `kind=test`, `file=tracks.adb` |
| módulo | `module = "SRS_TMS"` | `--module SRS_TMS` | los registros de ese módulo de DOORS |
| capítulo | `chapter = "3"` | `--chapter 3` | los de ese capítulo, por su número, su título o los dos (`3`, `Logging`, `3 Logging`) |

Los filtros se suman entre sí y con `document` y `kind`, y no distinguen mayúsculas. En
`Index.search` e `Index.answer` son `where = {"status": "Approved", "chapter": "3"}`
(`filters` lo saca del texto de las opciones).

```bash
atlas search "deletion" atlas.test.toml --attribute status=Approved
atlas ask "¿Qué requisitos aprobados se verifican por prueba?" atlas.dev.toml --attribute "status=Approved;verification=Test"
```

En las muestras, «deletion» sin filtro da ocho pasajes de los documentos y del código, y
ningún requisito de DOORS; con `status=Approved`, ocho requisitos aprobados de SRS_TMS y
SSS_MISSION.

Un filtro que no casa con nada no es un error: la herramienta devuelve, en `filters`, las
claves que hay y sus valores más frecuentes, y `error` si la clave no existe
(`no passage has criticality: the filters are chapter, file, kind, module, status,
verification`); `atlas search` las escribe. Una pregunta amplia con filtros lee enteros
solo los registros que casan: «resume los requisitos de SRS_TMS» con `status=Approved` lee
los aprobados del módulo, no el módulo entero.

El filtro es una condición `json_extract` sobre las filas del índice, sin índice propio: con
20 000 requisitos basta.

## Como librería

```python
from atlas.config import load_config
from atlas.index import Index
idx = Index.from_config(load_config("atlas.dev.toml"))
hits = idx.search("alarm latency", k=8, kinds=["requirement"])
hits = idx.search("deletion", where={"status": "Approved", "module": "SRS_TMS"})   # filtros
res = idx.answer("When does the HMI raise an alarm?")      # {"answer", "sources", "scope", ...}
                                                           # cada fuente: cite, excerpt y context (lo leído)
res = idx.answer("Summarise SRS_TMS", scope="whole")       # el módulo entero, por partes si no cabe
idx.embed_missing(["text one", "text two"], workers=4)    # a la caché, en lotes llenos
```

`examples/library/search.py` lo hace sobre las muestras.

## Pruebas

`tests/test_index.py` (registros, búsqueda, caché, lotes fallidos y lotes entre
documentos, cambio de modelo, herramientas, DOORS, filtros por `meta`, límite por documento,
preguntas de trazabilidad, citas comprobadas, estado de la respuesta, preguntas amplias en
una petición y por partes, documentos locales), `tests/test_cli.py` (pasos de `atlas run`,
`search`, `ask` y sus filtros) y `tests/test_index_legacy.py` (mismo resultado que `doc_rag.py`).
