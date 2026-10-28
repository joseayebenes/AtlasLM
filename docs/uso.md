# Guía de uso

Para el ingeniero del proyecto, con atlas ya instalado y los datos procesados
(`atlas run`, ver la [guía de instalación](instalacion.md)). Está organizada por tareas: qué
usar para cada una, cómo leer lo que sale y hasta dónde llega el modelo.

Hay dos formas de usar atlas:
- **La línea de comandos**: `atlas ACCIÓN [argumento] atlas.toml`, lanzada desde la carpeta
  del proyecto (las rutas de las fuentes son relativas a ella). Imprime en la pantalla y
  deja los ficheros largos en el workspace (`graph/`, `reports/`).
- **El editor** (OpenCode, VS Code): `atlas serve` le da a su modelo las herramientas de
  atlas (`trace`, `search`, `build_context`...). Se le pregunta en lenguaje natural y él
  decide qué herramienta llamar; conviene nombrarla cuando se sabe cuál es.

La regla general: **lo que se puede contar, se cuenta sin modelo**. `trace`, `matrix`,
`gaps`, `impact` y `find_symbol` salen del grafo al instante, sin gastar cupo, y dan siempre
lo mismo. El modelo sirve para leer y escribir: responder con citas, resumir, juzgar si dos
textos dicen lo mismo, redactar un informe.

| Quiero... | Línea de comandos | En el editor | ¿Modelo? |
|---|---|---|---|
| saber qué pide un requisito | `atlas trace ID` | `trace`, `get_requirement`, `build_context` | no |
| contrastarlo con los documentos | `atlas verify ID` | `verify_requirement` | sí |
| saber qué rompo si cambio algo | `atlas impact NOMBRE` | `impact`, `callers`, `trace_code` | solo el informe |
| saber qué falta por trazar | `atlas graph`, `atlas matrix MÓDULO`, `atlas coverage MÓDULO`, `atlas link MÓDULO` | `gaps`, `matrix`, `untraced`, `link_requirement` | `coverage` y `link` |
| preparar la matriz para una revisión | `atlas matrix MÓDULO`, `atlas verify MÓDULO`, `atlas comply MÓDULO` | `matrix`, `check_compliance` | `verify` y `comply` |
| escribir código para un requisito | `hephesto develop PLAN` (por tandas), `hephesto tests MÓDULO` | `build_context`, `read_source`, `find_symbol`; `check_code` y `judge_changes` de `hephesto serve` | el del editor |
| encontrar un dato | `atlas search TEXTO` | `search`, `get_section` | los embeddings |
| una pregunta concreta | `atlas ask PREGUNTA` | `ask_docs` | sí, 2 peticiones |
| una pregunta amplia | `atlas ask PREGUNTA [--whole]` | `ask_docs` con `whole`, `ask_atlas` | sí, varias peticiones |

`verify_requirement`, `link_requirement`, `check_compliance` y `ask_atlas` usan el modelo
dentro de la herramienta: el servidor solo las ofrece si están en `[mcp] tools` (ver
[atlas.mcp](../atlas/mcp/README.md)).

## Quiero saber qué pide un requisito

```bash
atlas trace SRS-0004 atlas.toml
```

Imprime en JSON todo lo que se sabe del requisito, en una llamada. Con las muestras:

```text
"id": "SRS-0004", "in_doors": true, "module": "SRS_TMS",
"text": "The TMF shall process track event 4 within 320 ms.",
"attributes": {"status": "Approved", "verification": "Inspection"},
"parent": "SRS-0900", "links_out": [{"id": "SSS-0005", "type": "satisfies"}], ...
"documents": [{"document": "spec_with_bookmarks.pdf", "title": "1.1 Function group 1.1",
               "text": "... within 401 ms after reception (see SSS-0005).",
               "values_differ": {"doors": ["4", "320"], "document": ["1.1.1", "3", "401"]}}, ...],
"implemented_by": {"items": [{"id": "tracks.delete_track", "file": ".../tracks.adb", "lines": "11-15"}]},
"verified_by": [{"id": "...", "how": "tag"}, {"id": "...", "how": "calls"}]
```

Cómo leerlo:
- `text`, `attributes`, `parent`, `children` y los enlaces son los de DOORS (la base que
  publica `tools/doors_export`). `links_out` son los requisitos que este satisface o
  refina; `links_in`, los que lo citan.
- `in_doors: false`: el ID se cita en el código o en un documento, pero no está en DOORS.
  Suele ser otra forma de escribirlo, o un requisito borrado.
- `documents`: las secciones que lo enuncian, con su texto. **`values_differ`** compara las
  cifras de los dos textos: en `doors`, las que DOORS tiene y la sección no; en `document`,
  al revés. Es una alarma, no un juicio: también salen números de sección o de evento. Si
  entre ellas hay un plazo o un límite (320 frente a 401 ms), uno de los dos textos está
  mal; `atlas verify` lo juzga con el modelo.
- `implemented_by`: el código que lo cita con su etiqueta (`-- Req: SRS-0004`), con fichero
  y líneas.
- `verified_by`: las pruebas. `how: "tag"` es una prueba que lo cita; `how: "calls"`, una
  que llama al código que lo implementa: lo ejercita, pero nadie ha dicho que lo verifique.
- Un ID que no existe da `error` y los parecidos (`similar`).

Para contrastarlo con la documentación, `atlas verify SRS-0004 atlas.toml` pregunta al
modelo si DOORS y las secciones dicen lo mismo: `consistent`, `inconsistent` (con cada
incongruencia: `value`, `condition`, `contradiction`, `scope` o `undefined`, las palabras de
cada texto y su fuente) o `no_documentation`. Una petición por requisito, guardada en caché:
verificar otra vez solo pregunta por lo que ha cambiado.

En el editor: «¿Qué pide SRS-0004 y quién lo implementa? Usa trace». `get_requirement` da
el texto oficial y las secciones; `build_context`, un paquete en Markdown con todo lo
anterior más las firmas, los llamadores y el código fuente, pensado para trabajar sobre él.

## Quiero saber qué rompo si cambio algo

```bash
atlas impact Logger.Log atlas.toml          # un subprograma, un tipo, una variable o un paquete
atlas impact SRS-0004 atlas.toml            # o un requisito
```

El grafo encuentra lo afectado y el agente `impact_analyst` lo redacta: el informe sale en
la pantalla y en `<workspace>/reports/`, con los datos del grafo al final. Los datos, sin el
modelo, los da la herramienta `impact` del editor:
- `items`: lo que depende del elemento (lo llama, convierte a su tipo, usa su variable),
  con su `distance`: 1 son los llamadores directos, y se llega hasta 3 niveles;
- `requirements`: los requisitos que implementan esos elementos (y los que estos
  satisfacen en DOORS), con `via`, el elemento por el que se llega;
- `tests`: las pruebas que conviene volver a pasar;
- `documents`: las secciones que enuncian esos requisitos o mencionan ese código.

De un requisito: los que derivan de él, su código, sus pruebas y sus secciones.

Lo que el grafo no ve: las llamadas a través de punteros a subprograma y las que se
resuelven en ejecución (despacho), y las que no pudo resolver (`atlas graph` cuenta las
`ambiguous calls` y las `unresolved calls`). Un impacto vacío no es una garantía.

## Quiero saber qué falta por trazar

```bash
atlas graph atlas.toml                      # los huecos de todo el proyecto, contados
atlas matrix SRS_TMS atlas.toml             # los de un módulo: sin código y sin pruebas
```

`atlas matrix` imprime el resumen (con las muestras, `40 requirements: 4 with code, 1 with
tests, 40 in documents.`) y las listas `without code` y `without tests`. Un requisito de sistema (SSS)
cuenta como implementado si lo están los SRS que derivan de él.

La herramienta `gaps` (en el editor o desde Python) da las listas de todo:
- `requirements_without_code`, `requirements_without_tests`;
- `code_without_requirement`: subprogramas que no citan ningún requisito;
- `cited_in_code_not_in_doors`, `stated_in_documents_not_in_doors`: IDs que no están en
  DOORS (otra forma de escribirlos, o requisitos borrados);
- `values_differ`: requisitos con cifras distintas en DOORS y en un documento.

Cada lista lleva `count` y, si no cabe, `truncated: true`.

Con el modelo:
- `atlas coverage SRS_TMS atlas.toml`: un informe de los huecos, ordenado y comentado por
  el agente `coverage_auditor`;
- `atlas link SRS_TMS atlas.toml`: para cada requisito sin código, el código que puede
  implementarlo, con confianza (`high`, `medium`, `low`), el motivo y la etiqueta que habría
  que añadir y dónde (`add ETIQUETA at FICHERO:LÍNEA`). **No toca el código**: es una
  propuesta para que alguien la revise.

## Quiero preparar la matriz para una revisión

```bash
atlas matrix SRS_TMS atlas.toml             # * para todos los módulos
```

Escribe `<workspace>/graph/matrix-SRS_TMS.csv` y `.md`, una fila por requisito:

| Columna | Qué es |
|---|---|
| `id`, `text` | el requisito y los primeros 120 caracteres de su texto |
| `status`, `verification` | esos atributos de DOORS, si los tiene |
| `derived` | los requisitos de otros módulos enlazados a él (los SRS de un SSS) |
| `code` | el código que lo cita |
| `tests` | las pruebas que lo citan |
| `covering_tests` | las pruebas que llaman a su código sin citarlo: evidencia más débil |
| `documents` | las secciones que lo enuncian (`documento §título`) |

Las listas van en una celda, separadas por `; `. El CSV está en UTF-8 y separado por comas:
un Excel en español, con doble clic, lo pone todo en una columna y estropea los acentos; hay
que abrirlo con «Datos › Desde texto/CSV», origen UTF-8 y delimitador coma. El Markdown se pega tal cual en un
informe.

Para la revisión, además:
- `atlas verify SRS_TMS atlas.toml`: cada requisito contra los documentos (ver arriba).
- `atlas comply SRS_TMS atlas.toml`: si el código de cada requisito hace todo lo que pide.
  El agente divide el requisito en obligaciones y marca cada una `met`, `not_met`,
  `partial` o **`cannot_tell`**: leyendo el código no se puede decidir (un plazo, el
  rendimiento, código que no está a la vista); eso lo tienen que decidir las pruebas. El
  veredicto sale de las marcas, no del modelo: `does_not_comply` si alguna no se cumple o
  solo en parte, `cannot_tell` si queda alguna sin decidir, `complies` si se cumplen todas,
  y `no_code` sin preguntar si no hay código.

Las dos dejan su informe en `<workspace>/reports/` y guardan los juicios: la segunda vez
solo preguntan por lo que cambió. Son una ayuda para la inspección, no la sustituyen: un
modelo pequeño puede dar por cumplida una obligación que no lo está (con «keep logs for 30
days», dio por cumplido «keep logs» con un `Put_Line` a la salida estándar).

## Quiero escribir código para un requisito desde el editor

Con `atlas serve` en el editor ([atlas en VS Code](vscode.md)), el modelo del editor escribe y
atlas le da el conocimiento; atlas no cambia ni ejecuta nada. Con `hephesto serve` a su lado, el
agente también compila, pasa las pruebas y sabe qué ha roto cada cambio:

1. **El contexto**: «Implementa SRS-0123. Empieza por build_context». `build_context` da en
   una llamada el requisito, las secciones que lo enuncian, el código que ya lo implementa,
   sus pruebas, sus llamadores y el código fuente, en unos 4000 tokens como mucho.
2. **El sitio**: `find_symbol`, `read_source` y `unit_info` para ver qué hay y dónde va el
   cambio.
3. **El cambio**, con las herramientas de edición del editor (o las de `hephesto serve
   --edit`, para un cliente que no tenga las suyas).
4. **La comprobación**, con hephesto: `check_code` compila el fichero (si hay
   `check_command`), `run_test` pasa una prueba y `judge_changes` dice qué pruebas han
   avanzado o retrocedido con el cambio.
5. **La etiqueta**: `-- Req: SRS-0123` en el comentario del subprograma. Tras el siguiente
   `atlas run`, `trace` lo encuentra; el servidor vuelve a leer el grafo sin reiniciarlo.

Encargos pequeños: un requisito, un subprograma. Los cambios de varios ficheros a la vez
salen mal con un modelo pequeño.

Para una tanda de requisitos con pruebas de aceptación, `hephesto develop PLAN atlas.toml`
encarga el código a un agente, encargo a encargo, y deja que las pruebas juzguen: lo que
rompe algo se deshace. En el informe (`<workspace>/reports/develop-<plan>.md`):
- **`not_advanced`** («did not advance»): el encargo compila y no rompe nada, pero las
  pruebas que decía hacer avanzar (`advances`) siguen fallando donde fallaban. Se conserva
  y hay que mirarlo;
- **`for a person`**: pruebas que no avanzaron en varias rondas; el agente no insiste.

`hephesto tests MÓDULO atlas.toml` escribe, sin modelo, las pruebas de los procedimientos de
prueba de un módulo, paso a paso, con los patrones de `[tests]`. Más en
[hephesto develop](../hephestolm/docs/develop.md).

## Quiero encontrar un dato o hacer una pregunta concreta

```bash
atlas search "track deletion latency" atlas.toml
atlas search "deletion" atlas.toml --kind requirement --k 5
atlas ask "¿Cuánto tarda en borrarse una traza?" atlas.toml
```

`atlas search` busca a la vez por identificadores exactos, por palabras y por significado
(vectores), en documentos, requisitos y código. Cada resultado lleva su cita: `documento ›
sección, p. 3-4 (tipo)`. `--kind` elige el tipo (`requirement`, `chunk`,
`section_summary`, `overview`, `definition`, `open_issue`, `code_doc`), `--document` un
documento y `--k` cuántos resultados.

`atlas ask` busca, da los pasajes al modelo y escribe la respuesta citando cada dato con
`[n]`; debajo, `Sources:` con la sección y el fichero de cada cita. Se puede preguntar en
español aunque los documentos estén en inglés: la búsqueda añade la pregunta en inglés.

Cómo leer lo que no sale:
- **«Nothing indexed is related to the question.»** y ninguna `Sources:`: la búsqueda no
  encontró nada. El índice puede estar vacío (`atlas status`, línea `index`) o la pregunta
  usar otras palabras: prueba `atlas search` con las del documento, o `--document`.
- **Una respuesta que dice que las fuentes no lo contienen**: los pasajes encontrados no lo
  dicen, y el modelo no se lo inventa. Puede estar en otra parte del documento: `--document`
  y `--whole` lo leen entero.
- **Una cita que no cuadra**: hay que abrirla (el fichero de `Sources:`, o `get_section` en
  el editor). La respuesta es tan buena como los pasajes.

## Quiero hacer una pregunta amplia

«Resume el capítulo 3», «todos los requisitos de alarmas», «qué dice el manual de los
registros»: la respuesta necesita mucho de un documento, no ocho pasajes.

```bash
atlas ask "Resume los requisitos de SRS_TMS" atlas.toml
atlas ask "¿Qué dice el manual de los registros?" atlas.toml --document hmi_manual.md --whole
```

Al reescribir la pregunta, el modelo dice si es amplia y de qué documentos trata; entonces
se leen enteros, sección a sección. Al final sale `Read whole: DOCUMENTOS (N requests to
the model)`. `--whole` lo fuerza y `--document` dice qué documento.

Cuesta más: una petición si los documentos caben en `[search] scope_chars` (100 000
caracteres) y, si no, una por parte más una que junta las notas. Un documento de 300 páginas
son varias partes y varios minutos. Con el cupo del gateway compartido, mejor una pregunta
amplia bien dirigida (`--document`) que tres a ciegas.

En el editor, `ask_docs` con `whole`, o `ask_atlas` (el asistente, que lista los documentos
y llama a `ask_docs` con `whole`). Si lo que se quiere es una lista exacta (todos los
requisitos sin pruebas), no es una pregunta para el modelo: es `matrix` o `gaps`.

## Lo que puede y no puede un modelo pequeño

El modelo del gateway es gemma 4 de 26B con 4B activos, con un cupo de 28 peticiones por
minuto que gasta todo lo que lances: un `atlas run`, el editor, cada pregunta. Cada vuelta de un agente tarda unos **30 s** (se han medido de 20 s a varios
minutos), y un agente con seis llamadas a herramientas, tres o cuatro minutos.

Lo hace bien:
- responder con citas a partir de los pasajes que encuentra la búsqueda;
- resumir una sección o un documento, y comparar dos textos (`verify`);
- elegir la herramienta adecuada para una pregunta clara, sobre todo si se la nombras;
- un cambio pequeño en un subprograma con el contexto delante y el compilador al lado.

Lo hace mal:
- listas largas y exactas: omite o repite elementos. Para eso, `matrix` y `gaps`;
- juicios finos: tiende a dar por buena una obligación a medias (`comply` lo compensa
  calculando el veredicto, pero cada marca es suya);
- cambios de varios ficheros o de más de unas decenas de líneas;
- instrucciones largas con varias condiciones: pregunta una cosa cada vez;
- recordar: cada pregunta empieza de cero; lo que importa va en la pregunta.

Y siempre:
- las cifras y los IDs se comprueban en la fuente citada;
- lo que el grafo da al instante no se pregunta al modelo;
- el razonamiento está apagado en el análisis y en los agentes que solo leen
  (`reasoning_effort = "none"`): con él, en las pruebas, gemma 4 tardaba más y acertaba
  menos (ver la [FAQ](faq.md#el-razonamiento-de-gemma-4)).

## Las demás acciones

| Acción | Para qué |
|---|---|
| `atlas run atlas.toml` | procesar lo que haya cambiado; se puede interrumpir y relanzar |
| `atlas status atlas.toml` | qué hay, qué falló y cuánto tardó cada paso, con las peticiones al modelo |
| `atlas sections atlas.toml` | las secciones de cada documento, sin modelo, para ajustar la estructura |
| `atlas eval atlas.toml` | las preguntas con respuesta conocida de `[agents.eval]`, puntuadas |
| `atlas agents atlas.toml`, `atlas agent NOMBRE PREGUNTA atlas.toml` | los agentes, y preguntar a uno |
| `atlas plugins atlas.toml` | los analizadores, adaptadores y extractores disponibles |
| `atlas reference atlas.toml` | los manuales y la biblioteca del compilador para `search_reference` |
| `atlas check atlas.toml`, `atlas probe atlas.toml` | la conexión con el modelo, y el modelo como agente |
| `atlas serve atlas.toml` | el servidor MCP del editor (lo arranca el editor) |
| `atlas init`, `atlas --version` | una plantilla de `atlas.toml`, y la versión |

`python examples/walkthrough.py atlas.toml` recorre las principales con la configuración
dada e imprime cada salida con su tiempo; `make demo` lo hace con las muestras.
