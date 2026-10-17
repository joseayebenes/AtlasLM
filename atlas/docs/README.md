# atlas.docs — documentos

Extrae el texto de cada documento, lo divide en secciones y las analiza, y escribe el
resultado en Markdown y en `structure.json`. Viene de `legacy/pdf_struct.py`.

| Fichero | Qué tiene |
|---|---|
| `__init__.py` | extractores de texto (plugins por extensión): `pymupdf` (PDF), `markitdown` (Office, HTML) y `text` (Markdown y texto) |
| `structure.py` | secciones sin modelo: marcadores del PDF, títulos numerados o bloques de tamaño fijo; limpieza de cabeceras y pies |
| `analysis.py` | análisis de cada sección (`llm`, `regex` o `none`), verificación de los requisitos contra el texto y consolidación |
| `writer.py` | el Markdown y `structure.json` |
| `process.py` | un documento entero: `DocsOptions`, `process_document`, `preview` |

## Extracción del texto

Cada extensión la lee el extractor activo con más prioridad:

| Extractor | Lee | Notas |
|---|---|---|
| `pymupdf` | PDF | por defecto para los PDF. Un bloque de texto es un párrafo, con una línea en blanco entre bloques, y `\f` entre páginas |
| `markitdown` | Office, HTML y, si se apaga `pymupdf`, PDF | los PDF, con pdfminer |
| `text` | Markdown y texto | tal cual |

pymupdf extrae un PDF unas 30 veces más rápido que pdfminer (1,5 ms por página en vez de 45)
y respeta el orden en que se escribió el texto. pdfminer agrupa por posición: separa el
número de un título de su texto, y a veces los pone al revés y pegados al párrafo anterior.
En `spec_without_bookmarks.pdf` de las muestras, con pdfminer salían 9 de las 27
subsecciones, y los requisitos de las otras 18 quedaban en la sección anterior; con pymupdf
salen todas. Con marcadores, los dos dan las mismas secciones y los mismos requisitos.

Una página con imágenes y sin texto es un escaneo: `preview` y `process_document` cuentan
esas páginas (`pages_without_text`, con pymupdf). Si son más de la mitad, `process_document`
lanza `ScannedDocument` («scanned PDF: N of M pages have no text; nothing is sent to the
model») antes de escribir nada, la unidad de `docs.process` queda en error y `atlas sections`
lo avisa. Con `ocr_command` en la fuente, esos PDF se leen de una copia pasada por OCR que se
guarda en `<workspace>/docs/.ocr` por el hash del PDF y de la orden.

Con `tables = true` en la fuente, pymupdf busca las tablas de cada página
(`page.find_tables()`) y los bloques que caen dentro de una (la mayor parte de su área)
se sustituyen por sus filas en Markdown, una por línea y con `|---|` tras la cabecera, en
el lugar del primero. `table_requirements = { id = "ID", text = "Requirement" }` saca un
requisito de cada fila (`analysis.table_requirements`), sin modelo con `regex` y como
complemento de lo que saca el modelo con `llm`. Las tablas sin bordes no siempre se
encuentran: `atlas sections` dice cuántas tablas salieron de cada documento.

Para volver a pdfminer: `[plugins] disabled = ["extractor:pymupdf"]`. El extractor forma
parte de la huella de `docs.process`, así que al cambiarlo los PDF se vuelven a procesar, y
como su texto cambia, sus secciones vuelven al modelo. `atlas sections` dice qué extractor
lee cada documento.

pymupdf tiene licencia AGPL-3.0 (o comercial de Artifex); también se usa para leer los
marcadores. markitdown y pdfminer son MIT.

## Fuentes de documentos

Cada `[[sources.docs]]` es un grupo de documentos (PDF, Markdown, Word...) con su forma de
procesarlos:

| Clave | Valores | Para qué |
|---|---|---|
| `paths`, `patterns` | carpetas o ficheros; `["*.pdf"]` | qué documentos (las carpetas se recorren enteras) |
| `analysis` | `llm` (por defecto), `regex`, `none` | `llm`: el modelo saca resumen, requisitos, glosario, entidades y pendientes por sección. `regex`: solo requisitos, sin modelo. `none`: solo la estructura |
| `req_regex` | `(?i)\bReq\s*:\s*(\S+)` | en `regex`, marca cada requisito; el grupo 1 es su ID y el texto sigue hasta el final de la frase |
| `structure` | `auto`, `bookmarks`, `headings`, `size` | de dónde salen las secciones: marcadores del PDF, títulos numerados del texto o bloques de tamaño fijo |
| `heading_regex` | grupo 1 = número, grupo 2 = título | si los títulos tienen un formato que no se reconoce |
| `max_chars`, `min_chars` | 12000, 300 | tamaño máximo de sección; las más cortas se unen a la anterior |
| `lang` | `"Spanish"` | idioma de los resúmenes (vacío: el del documento) |
| `doc_id_regex`, `version_regex`, `date_regex` | `'Doc\.\s+(\S+)'` | código, versión y fecha del documento, de sus cabeceras, sus dos primeras páginas o los metadatos del PDF |
| `authority` | `1` | autoridad del grupo (1 = normativo); solo la usa el grafo |
| `tables` | `true` | las tablas de los PDF como filas Markdown (apagado por defecto) |
| `table_requirements` | `{ id = "ID", text = "Requirement" }` | un requisito por fila de las tablas con esas columnas |
| `ocr_command` | `["ocrmypdf", "--skip-text", "{file}", "{out}"]` | OCR de los PDF con páginas sin texto, sobre una copia |

La referencia completa está en la [guía de configuración](../../docs/configuracion.md).

## Salida

Cada documento se escribe en `<workspace>/docs/<fuente>/<nombre>_<extensión>/`:
- `index.md`, y una página por sección en `sections/`;
- `requirements.md`, con la verificación de cada requisito contra el texto;
- `glossary.md`, `entities.md` y `open_issues.md`;
- `structure.json`, todo junto, para el índice y el grafo, con `doc_id`, `version` y `date`
  (vacíos si no se encuentran), `pages` y `pages_without_text`.

La carpeta se escribe en `<salida>.tmp` y sustituye a la anterior al final: un corte nunca
la deja a medias. Dos documentos con el mismo nombre y extensión en carpetas distintas
reciben carpetas distintas, también cuando se procesan a la vez.

Los análisis de sección se guardan en `<workspace>/docs/.cache`, por texto de la sección,
modelo, idioma y versión del prompt: un documento modificado solo vuelve al modelo con las
secciones cuyo texto ha cambiado. La caché se escribe de forma atómica, y un fichero dañado
se vuelve a analizar. Si falla alguna sección, el documento se escribe igual y queda en
error para reintentarla en la siguiente ejecución.

## En `atlas run`

El paso `docs.process` tiene una unidad por documento. Procesa varios documentos a la vez,
y las secciones de todos ellos comparten `[pipeline] workers` peticiones al modelo (o las
de `[pipeline.step_workers] docs`): la extracción de un PDF avanza mientras el modelo
contesta sobre otros. Si la ejecución se interrumpe, ningún documento empieza más
secciones.

Antes de procesar documentos nuevos, `atlas sections` muestra qué secciones saldrían, de
dónde sale la estructura y por qué se aceptan o rechazan las líneas que parecen títulos.

## Ollama

Con razonamiento, `gemma4:12b` tardaba entre 1 y 8 minutos en cada sección del manual de
ejemplo. Sin él (`[llm] reasoning_effort = "none"`), unos 15 s, y el manual entero, 48 s,
con los tres requisitos literales. Esa versión de Ollama tampoco acepta `response_format`
con esquema JSON (responde 501): el análisis sigue sin él y saca el JSON de la respuesta.

## Como librería

Sin motor ni CLI:

```python
from atlas.docs import DocsOptions, preview, process_document
print(preview("spec.pdf")["sections"])                           # las secciones, sin modelo
data = process_document("spec.pdf", "out/spec", DocsOptions(analysis="regex"))
data = process_document("design.pdf", "out/design", DocsOptions(lang="Spanish"), llm=conn.llm, workers=4)
print(data["requirements"][0])
```

`examples/library/documents.py` procesa las muestras de esta forma.

## Pruebas

`tests/test_docs_structure.py`, `test_docs_analysis.py`, `test_docs_process.py` (caché,
reintentos, división de secciones cortadas, Ctrl+C, caché dañada, PDF escaneados,
`ocr_command` y tablas) y `test_docs_legacy.py`
(mismo resultado que `pdf_struct.py`).
