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

`[index] kinds` elige qué se indexa.

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

`atlas ask` reescribe antes la pregunta en consultas de búsqueda. Después pasa al modelo
los mejores resultados, con los fragmentos vecinos, y le pide responder solo con esas
fuentes y citándolas como `[n]`.

Un proceso que vive mucho (el servidor MCP) ve lo que indexa otro: los vectores se vuelven
a leer cuando cambia la base.

`Index.search(..., dense=False)` busca solo con los IDs y BM25: no hace ninguna petición a
ningún modelo. `atlas eval` compara las dos formas (ver [atlas.agents](../agents/README.md)).
Sobre las muestras, con `embeddinggemma`, la híbrida pone lo buscado en primer lugar en el
76 % de los casos y entre los tres primeros en el 94 %; la léxica, en el 65 % y el 82 %.

Con Ollama (`atlas.dev.toml`), indexar los tres documentos de ejemplo con `embeddinggemma`
lleva segundos. Una respuesta de `gemma4:12b` tarda de 50 s a 2 min, según la longitud del
contexto y de la respuesta.

## Como librería

```python
from atlas.config import load_config
from atlas.index import Index
idx = Index.from_config(load_config("atlas.dev.toml"))
hits = idx.search("alarm latency", k=8, kinds=["requirement"])
res = idx.answer("When does the HMI raise an alarm?")      # {"answer": ..., "sources": [...]}
idx.embed_missing(["text one", "text two"], workers=4)    # a la caché, en lotes llenos
```

`examples/library/search.py` lo hace sobre las muestras.

## Pruebas

`tests/test_index.py` (registros, búsqueda, caché, lotes fallidos y lotes entre
documentos, cambio de modelo, herramientas, DOORS), `tests/test_cli.py` (pasos de `atlas
run`, `search`, `ask`) y `tests/test_index_legacy.py` (mismo resultado que `doc_rag.py`).
