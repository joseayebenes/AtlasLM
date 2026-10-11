# atlas.docs — documentos

Extrae el texto de cada documento, lo divide en secciones y las analiza, y escribe el
resultado en Markdown y en `structure.json`. Viene de `legacy/pdf_struct.py`.

| Fichero | Qué tiene |
|---|---|
| `__init__.py` | extractores de texto (plugins por extensión): `markitdown` (PDF, Office, HTML) y `text` (Markdown y texto) |
| `structure.py` | secciones sin modelo: marcadores del PDF, títulos numerados o bloques de tamaño fijo; limpieza de cabeceras y pies |
| `analysis.py` | análisis de cada sección (`llm`, `regex` o `none`), verificación de los requisitos contra el texto y consolidación |
| `writer.py` | el Markdown y `structure.json` |
| `process.py` | un documento entero: `DocsOptions`, `process_document`, `preview` |

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

La referencia completa está en la [guía de configuración](../../docs/configuracion.md).

## Salida

Cada documento se escribe en `<workspace>/docs/<fuente>/<nombre>_<extensión>/`:
- `index.md`, y una página por sección en `sections/`;
- `requirements.md`, con la verificación de cada requisito contra el texto;
- `glossary.md`, `entities.md` y `open_issues.md`;
- `structure.json`, todo junto, para el índice y el grafo.

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
reintentos, división de secciones cortadas, Ctrl+C, caché dañada) y `test_docs_legacy.py`
(mismo resultado que `pdf_struct.py`).
