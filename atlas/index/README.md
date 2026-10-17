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
un fragmento. Así, `search` encuentra un requisito aunque no aparezca en ningún documento, y
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

## Embeddings

Todo va en `atlas.db`: registros, vectores, índice de texto y una caché de vectores por
modelo y texto, que evita embeber dos veces el mismo texto. Un documento reanalizado solo
embebe sus textos nuevos. Cambiar `[embeddings] model` reindexa todo sin volver al LLM, y
la búsqueda nunca mezcla vectores de dos modelos.

Antes de indexar, cada paso junta los textos que faltan en la caché de todos sus
documentos (o módulos) y los embebe en lotes llenos de `[embeddings] batch`, varios a la
vez (`embed_missing`). Con un límite de peticiones por minuto, eso son menos peticiones que
embeber documento a documento: en el proyecto grande, 189 en vez de 207 para 12 060 textos
con lotes de 64. Un lote que falla no para los demás; lo que falta se embebe en la
siguiente ejecución.

## Búsqueda

Una búsqueda combina tres listas con Reciprocal Rank Fusion:
- los IDs exactos que aparecen en la consulta;
- la similitud de los vectores;
- BM25 sobre el mismo texto (FTS5 de SQLite).

Una búsqueda en todos los documentos da como mucho `[search] per_document` resultados (3)
de cada documento, módulo de DOORS o unidad de código mientras otros tengan alguno; si no
hay bastantes, completa con los demás. Cada elemento de una unidad de código repite su
nombre (`ada unit tracks > constant tracks.max_tracks`), y sin ese límite una unidad cuyo
nombre está en la pregunta ocupaba todos los puestos: en las muestras, «how are tracks
deleted» daba 8 elementos de `tracks` y ningún requisito. Con `document` no hay límite.
`[search] results` dice cuántos resultados (8).

`atlas ask` reescribe antes la pregunta en consultas de búsqueda. Después pasa al modelo
los mejores resultados, con los fragmentos vecinos (hasta `[search] context_chars`), y le
pide responder solo con esas fuentes y citándolas como `[n]`.

## Preguntas amplias

Ocho pasajes no bastan para «resume los requisitos de SRS_TMS» o «explica cómo funciona el
registro»: en las muestras, la primera pasaba al modelo 8 de los 40 requisitos del módulo, y
la respuesta parecía completa sin serlo. Por eso, al reescribir la pregunta, el modelo dice
también si es amplia (un resumen, una función o un capítulo enteros, todos los requisitos o
valores de algo) y de qué documentos o módulos trata, de la lista de los indexados. Si es
amplia, `atlas ask` lee esos documentos enteros. Si no nombra ninguno, lee los que más
salen en su búsqueda: hasta tres, con al menos la mitad de resultados que el primero, y
nunca unidades de código:

- cada sección es una fuente: el texto de sus fragmentos sin el solape; en un módulo de
  DOORS, sus requisitos y objetos de información en su orden;
- si caben en `[search] scope_chars` (100 000 caracteres), una sola petición;
- si no, por partes de ese tamaño, a la vez: de cada una, el modelo toma notas de todo lo
  que importa a la pregunta, con sus citas, y una última petición las junta en la
  respuesta. Así ninguna parte se queda fuera, a cambio de más peticiones.

Las fuentes de la respuesta son las secciones que cita, y `scope` dice cómo se leyó
(`passages` o `whole`). `--whole` (o `whole` en `ask_docs`) lee enteros los documentos aunque
la pregunta no parezca amplia, y `[search] scope = "passages"` no los lee nunca.

Un proceso que vive mucho (el servidor MCP) ve lo que indexa otro: los vectores se vuelven
a leer cuando cambia el índice. Lo dice un contador propio (`index_meta`), que solo sube al
reemplazar o borrar un documento; el resto de escrituras en `atlas.db` (el libro de
registro de un `atlas run`, la caché de vectores) no obligan a releerlos.

`Index.search(..., dense=False)` busca solo con los IDs y BM25: no hace ninguna petición a
ningún modelo. `atlas eval` compara las dos formas (ver [atlas.agents](../agents/README.md)).
Sobre las muestras, con `embeddinggemma`, la híbrida pone lo buscado en primer lugar en el
75 % de los casos y entre los tres primeros en el 90 %; la léxica, en el 55 % y el 70 %.

El código compite con los documentos. En las muestras, «show the tracks on the operator
screen» bajó de la primera a la cuarta posición al indexar el código, porque el código de
`tracks` habla de *tracks*. Para buscar solo en una clase de texto, `--kind` en `atlas
search` y `atlas ask`, `kind` en la herramienta `search` (o `kinds` en `Index.search`).

Con Ollama (`atlas.dev.toml`), indexar los tres documentos de ejemplo con `embeddinggemma`
lleva segundos. Una respuesta de `gemma4:12b` tarda de 50 s a 2 min, según la longitud del
contexto y de la respuesta.

## Rendimiento de la búsqueda densa

`python examples/dense_search.py [DEST] [--dim 768]` cronometra `Index.search(dense=True)`
sobre el índice de `make large` (15 120 registros): en frío (la primera búsqueda de un
proceso, que carga los vectores en una matriz), en caliente, y mientras otro proceso
escribe en el libro de registro de la misma `atlas.db` cada 50 ms, como un `atlas run`
mientras el servidor MCP responde. Los vectores del servidor falso tienen 64 dimensiones;
con `--dim 768` (las de `embeddinggemma`) mide una copia con cada vector repetido hasta
768.

Medido el 17-10-2026, la mediana de tres tandas alternando la versión anterior y la nueva,
en una máquina con una carga media de 15 a 35 (milisegundos de pared):

| | 64 dim. antes | después | 768 dim. antes | después |
|---|---|---|---|---|
| primera búsqueda (carga la matriz) | 113 | 99 | 174 | 127 |
| cargar la matriz | 89 | 51 | 145 | 212 |
| búsqueda en caliente | 19 | 20 | 17 | 20 |
| búsqueda en caliente, un documento | 45 | 43 | 28 | 32 |
| búsqueda sin vectores | 22 | 19 | 18 | 21 |
| solo `dense` | 1,4 | 0,7 | 2,8 | 1,6 |
| solo `dense`, un documento | 1,2 | 0,4 | 2,1 | 1,4 |
| búsqueda con otro proceso escribiendo | 73 | 28 | 165 | 21 |

Antes, cada commit de otra conexión cambiaba `PRAGMA data_version` y la búsqueda siguiente
volvía a cargar la matriz: con un `atlas run` en marcha, cada búsqueda pagaba la carga. Ahora
solo cuenta el contador del índice, y la búsqueda cuesta lo mismo que en caliente. `dense`
toma los mejores con `np.argpartition` (las puntuaciones iguales, en el orden de los
registros) y filtra por documento con una máscara de numpy en vez de recorrer todos los
registros ordenados en Python; es menos del 10 % de una búsqueda,
que se va en BM25 y en leer los registros de SQLite. Las diferencias de las demás filas
están dentro del ruido de la máquina.

Los vectores siguen leyéndose fila a fila: cargar la matriz cuesta de 0,1 a 0,2 s con
15 000 registros de 768 dimensiones y ahora solo ocurre cuando cambia el índice, así que
guardarlos en un BLOB contiguo por documento no compensa (juntar los BLOB con `b"".join` en
vez de `np.vstack` tampoco mejoró la carga).

## Como librería

```python
from atlas.config import load_config
from atlas.index import Index
idx = Index.from_config(load_config("atlas.dev.toml"))
hits = idx.search("alarm latency", k=8, kinds=["requirement"])
res = idx.answer("When does the HMI raise an alarm?")      # {"answer", "sources", "scope", ...}
res = idx.answer("Summarise SRS_TMS", scope="whole")       # el módulo entero, por partes si no cabe
idx.embed_missing(["text one", "text two"], workers=4)    # a la caché, en lotes llenos
```

`examples/library/search.py` lo hace sobre las muestras.

## Pruebas

`tests/test_index.py` (registros, búsqueda, caché, lotes fallidos y lotes entre
documentos, cambio de modelo, herramientas, DOORS, límite por documento, preguntas amplias
en una petición y por partes), `tests/test_cli.py` (pasos de `atlas
run`, `search`, `ask`) y `tests/test_index_legacy.py` (mismo resultado que `doc_rag.py`).
