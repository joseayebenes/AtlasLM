# atlas.graph — grafo y trazabilidad

Junta en un solo grafo el código, los requisitos de DOORS y los documentos, y responde sin
modelo a las preguntas de trazabilidad e impacto. Viene de `legacy/ada_graph.py` y
`legacy/ada_graph_mcp.py`.

| Fichero | Qué tiene |
|---|---|
| `build.py` | construcción de las tres partes del grafo |
| `sources.py` | los nombres de documentos y de elementos de código que se buscan en el texto |
| `store.py` | `GraphStore`: el grafo en `atlas.db`, por origen |
| `export.py` | `write_matrix`: la matriz en CSV, Markdown y XLSX |
| `knowledge.py` | `KnowledgeGraph`: consultas de código y de trazabilidad, exportación |
| `values.py` | `quantities` y `values_differ`: las cantidades de un texto y cómo difieren las de DOORS y las de un documento |
| `tools.py` | `GraphTools`: las herramientas del grafo para agentes y MCP |

## Construcción

El paso `graph.build` tiene una unidad por origen, y cada parte se reconstruye solo cuando
cambia aquello de lo que sale:

| Parte | Sale de | Nodos | Aristas |
|---|---|---|---|
| `code` | `code.parse` | unidades, subprogramas, pruebas, tipos, variables, requisitos citados | `defines`, `calls`, `converts`, `uses`, `withs`, `implements`, `verifies` |
| `doors` | `doors.requirements` | módulos (`module:SRS_TMS`, con `exported_at` y `source`: cuándo y de dónde se exportaron), requisitos, encabezados | `contains` (módulo y jerarquía) y un enlace por cada enlace de DOORS, con su tipo (`satisfies`...) |
| `docs` | `docs.process` | documentos (`doc:hmi_manual.md`) y secciones (`doc:hmi_manual.md#003`) | `contains`, `declares` (la sección declara el requisito) y `mentions` (la sección nombra un elemento de código) |

**Código.** Los elementos se identifican como `unidad.nombre` (`tracks.update`), así que dos
paquetes pueden tener un `Update` cada uno. Cada `Nombre (...)` de un cuerpo se clasifica
según lo que es `Nombre`:
- un subprograma: es una llamada;
- un tipo: es una conversión;
- una variable de paquete: es un uso;
- un tipo predefinido: se ignora;
- algo desconocido: es una llamada externa.

Un nombre sin paquete se busca primero en la propia unidad y luego en las demás,
prefiriendo las que se importan con `with`. Si aún hay varias, la arista va a todas, para
que el análisis de impacto no se deje ninguna; `atlas graph` las cuenta como llamadas
ambiguas.

Los ficheros que casan con `[sources.code] test_patterns` son pruebas: sus subprogramas son
nodos `test`. Un elemento que cita un requisito lo implementa (`implements`); una prueba
que lo cita lo verifica (`verifies`).

**Requisitos.** Un requisito es el mismo nodo en las tres partes. Sus IDs se normalizan
igual que los de DOORS (`[sources.doors.convention] id_normalize`; mayúsculas por
defecto), así que `Req: srs-0004` en el código y `SRS-0004` en DOORS se encuentran.

**Documentos.** Una sección nombra un elemento de código cuando escribe su nombre completo
(`Tracks.Delete_Track`) o un nombre con guion bajo que solo tiene un elemento
(`Delete_Track`). Las palabras sueltas, como `Update`, son demasiado comunes para contar.

El grafo completo se exporta a `<workspace>/graph/atlas.graphml` (Gephi, yEd) y
`atlas.json`. Con `[graph] communities = true`, cada elemento de código lleva su grupo
(Louvain), para colorearlo en Gephi.

## Consultas de trazabilidad

Sin modelo:

| Consulta | Devuelve |
|---|---|
| `trace(req)` | texto y atributos de DOORS, y la fecha de la exportación de su módulo (`exported_at`); padre, hijos y enlaces; las secciones que lo declaran, con su texto y las cifras que difieren de DOORS (solo `kind` `value`); los requisitos de los que deriva en otros módulos, hasta el de sistema (`ancestors`, 5 niveles, con el tipo de cada salto); el código que lo implementa; las pruebas del código que lo verifican (lo citan) o lo cubren (llaman, directa o indirectamente, a ese código), en `verified_by`; los procedimientos de prueba de DOORS que lo verifican, en `procedures` |
| `trace_code(nombre)` | requisitos, llamadores, llamados, pruebas y secciones de un elemento de código; de una prueba, el código que ejercita y los requisitos que cubre |
| `impact(nombre)` | de código: lo que depende de él (lo llama, convierte a su tipo, usa su variable), y los requisitos, pruebas, procedimientos de prueba y secciones afectados, incluidos los requisitos de los que esos derivan en DOORS; de un requisito: los que derivan de él, su código, pruebas, procedimientos y secciones |
| `matrix(módulo)` | requisito → código → pruebas → procedimientos de prueba → documentos, con resumen, huecos y `meta`: de qué exportación de DOORS sale |
| `gaps(módulo)` | requisitos sin código, sin pruebas o sin procedimiento de prueba; requisitos sin enlace de derivación hacia otro módulo, por módulo; subprogramas sin requisito; IDs del código o de los documentos que no están en DOORS; IDs que DOORS enlaza en módulos que no se han leído, con quién los enlaza y con qué tipo; cifras distintas entre DOORS y los documentos (solo `kind` `value`) |
| `neighbors(nodo)` | el subgrafo alrededor de un nodo, en JSON o en Mermaid |
| `communities()` | grupos de código que trabajan juntos, con sus unidades y requisitos |
| `at(fichero, línea)` | el elemento de código que contiene esa línea: el subprograma o la prueba más interior, si no el tipo, la variable o la constante declarados en ella, si no la unidad del fichero |

Un elemento de código se puede dar por su identificador (`tracks.update`), por su nombre
simple si solo un elemento lo tiene (`Update`) o por un fichero y una línea
(`src/tracks.adb:12`, también `C:\src\tracks.adb:12`): así `trace_code`, `impact`,
`read_source`, `get_node` y `build_context` aceptan lo que el editor tiene abierto. El
fichero es relativo a la carpeta del proyecto, o absoluto, y en Windows no distingue
mayúsculas. El subprograma se busca en el fichero tal como está ahora (con tree-sitter),
por su nombre, para que las líneas añadidas desde el último `atlas run` no lo muevan; si el
fichero no se puede leer, valen las líneas del grafo. Un fichero que no está en el grafo
responde con los del grafo que se llaman igual.

**Tipos de enlace.** Lo que significa cada tipo de enlace de DOORS lo dice
`[sources.doors.convention] link_semantics` (ver la
[guía de configuración](../../docs/configuracion.md#sourcesdoorsconvention)):

| Clase | Por defecto | Qué hace en las consultas |
|---|---|---|
| `derives` | todos los tipos que no están en las otras dos | el origen deriva del destino: un SRS que satisface un SSS. Columna `derived`, cobertura a través de los derivados, subida de `impact` |
| `verifies` | `verifies` | el origen es un procedimiento de prueba del destino: un STP que verifica un SRS. Columna `procedures` y su hueco; nunca entre las pruebas del código |
| `ignore` | `references` y el `inline_reference_type` | solo referencias: se ven en `trace` (`links_out`, `links_in`) y no cuentan |

La convención va en las estadísticas de la construcción de la parte de DOORS, así que todas
las consultas (`atlas`, el servidor MCP, los agentes) usan la misma; cambiarla reconstruye
esa parte.

Un requisito de un módulo cuyos derivados de otro módulo tienen código cuenta como
implementado a través de ellos: un SSS lo está si lo están los SRS que lo satisfacen. Lo
mismo con las pruebas y los procedimientos de prueba. Los enlaces dentro de un mismo módulo
(refinamientos) no cuentan como derivación.

**Revisión de la asignación.** `gaps` lista en `requirements_without_derivation` los
requisitos que no tienen ningún enlace de un tipo de `derives` a un requisito de otro
módulo (un SRS que no satisface ningún SSS), con su recuento por módulo en `by_module`.
Deja fuera los módulos de `[sources.doors.convention] top_modules` (los de sistema, que no
derivan de nada) y los de procedimientos de prueba (aquellos cuyos enlaces a otros módulos
son todos de un tipo de `verifies`). `linked_to_modules_not_read` lista los destinos de
enlaces a módulos que no se han leído (`[sources.doors] modules` los deja fuera), con
`linked_by`: el ID que los enlaza y el tipo. `trace` da en `ancestors` la cadena hacia
arriba: cada salto con su `id`, `type`, `level`, el requisito desde el que se llega
(`via`) y el texto corto. Con las muestras, `atlas trace SRS-0004`:

```
"ancestors": [{"id": "SSS-0005", "type": "satisfies", "level": 1, "via": "SRS-0004",
               "module": "SSS_MISSION", "text": "The mission system shall provide capability 5."}]
```

Las pruebas son dos cosas distintas: las **pruebas del código** (`tests`, `covering_tests`;
subprogramas de los ficheros de `[sources.code] test_patterns`) y los **procedimientos de
prueba** de DOORS (`procedures`; requisitos de un módulo STP con un enlace de tipo
`verifies`). El resumen de la matriz las cuenta por separado (`with_tests`,
`with_procedures`), y los huecos también (`without_tests`, `without_test_procedure`).

Cada fila de la matriz lleva los atributos de DOORS de `[sources.doors] attributes` (por
defecto `status` y `verification`), con el nombre que se escribe allí y sin distinguir
mayúsculas al buscarlos; `list_requirements` también. `meta` dice de dónde sale la matriz:
`module`, `exported_at` y `source` (la exportación de DOORS; con todos los módulos, la más
antigua, y cada una en `modules`), `atlas_version`, `built_at` por origen del grafo,
`attributes` y, con `[graph] code_revision = true` y git, `code_revision`.

`atlas matrix SRS_TMS` escribe la matriz en `<workspace>/graph/matrix-SRS_TMS.*`, en los
formatos de `[graph] matrix_formats` (`export.py`, `write_matrix`): `.csv` para Excel (UTF-8 con
BOM, `;` entre columnas por defecto, `csv_delimiter`; las listas de una celda, una por línea;
una columna por atributo y, la última, `exported_at`, la fecha de la exportación del módulo de
cada fila), `.md` (con `meta` en la cabecera), `.json` (filas, resumen, huecos y `meta`) y,
con el extra `[xlsx]`, `.xlsx` (matriz con filtros, huecos y resumen con la fecha de DOORS).
Muestra de dónde sale, el resumen y los huecos; `atlas matrix "*"` la hace con todos los
requisitos. Con las muestras:

```
DOORS SRS_TMS: exported 2026-10-17 (today), from /Mission/SRS_TMS
atlas 0.2.0; graph built: code 2026-10-17T10:02:11, docs 2026-10-17T10:02:11, doors 2026-10-17T10:02:11
40 requirements: 4 with code, 1 with tests, 3 with test procedures, 40 in documents.
without code (36): SRS-0005, SRS-0006, …
without tests (39): SRS-0001, SRS-0002, SRS-0003, SRS-0005, …
without a test procedure (37): SRS-0001, SRS-0005, …
```

En el CSV, SRS-0004 tiene la prueba del código en `tests` y el procedimiento de prueba de
DOORS en `procedures`:

```
id,text,status,verification,derived,code,tests,covering_tests,procedures,documents
SRS-0004,The TMF shall process track event 4 within 320 ms.,Approved,Inspection,,tracks.delete_track,test_tracks.test_tracks,,STP-0001,…
```

`atlas trace SRS-0004` imprime todo lo del requisito en Markdown, con `build_context` de
[atlas.agents](../agents/README.md) (sin modelo y sin el extra `[agents]`) y un presupuesto de
20 000 tokens, más holgado que el de la herramienta para no cortar en pantalla; `--json` da el
diccionario de `trace`. `atlas matrix --json` da el de `matrix`, con todas las filas.

La comparación de cifras es el primer paso de la verificación de requisitos con la
documentación: el texto de DOORS de SRS-0004 dice 320 ms y el del PDF de ejemplo, 401 ms.
`atlas verify` (ver [atlas.agents](../agents/README.md)) juzga el resto con el modelo.

### Cómo se comparan las cifras

`values.py` saca de cada texto sus cantidades: un número (o un número escrito con letras,
«two», «dos») y su unidad, si es una de las conocidas (ms, s, min, h, m, km, Hz, kHz, %, B,
KB, MB, escritas de varias formas: «milliseconds», «segundos»...). Una coma o un punto seguido
de exactamente tres cifras separa miles («1,000» y «1.000» son 1000, pero «0,125» no); si no,
es decimal. Se comparan como números (1.5 y 1.50 son lo mismo) y se dejan fuera las
referencias («section 3.2», «Table 3», «figura 4», «anexo B.2», «page 12»), las fechas, las
versiones (`v2`), los IDs (`SRS-0001`) y la numeración de secciones («1.1.1»).

`values_differ(oficial, documento)` devuelve `{"doors", "document", "kind"}`, con las cantidades
tal como las escribe cada texto, o nada:

| `kind` | Cuándo | Ejemplo |
|---|---|---|
| `value` | una misma unidad (o ninguna) con un valor en cada texto | 320 ms frente a 300 ms; 30 días frente a 60 |
| `unit_only` | unidades distintas de la misma magnitud, o la unidad solo en un texto; las unidades no se convierten nunca | 300 ms frente a 0,3 s |
| `format_only` | las mismas cantidades, escritas de otra forma | «1,000» y «1000»; «1.5» y «1.50»; «two» y «2» |

Una cantidad que solo está en un texto, sin nada con qué compararla en el otro, no es una
diferencia: el documento puede decir más que el requisito. `trace` y `gaps` solo enseñan
`value`; `atlas verify` le da al modelo también `unit_only`, para que compare las unidades.
Antes, la comparación de los números a secas daba un veredicto erróneo en 6 de estos 7
casos: «300 ms» y «0.3 s», «1,000» y «1000», «see section 3.2», «Table 3», «1.5» y «1.50»,
«two» y «2» (acertaba en «320» y «300»).

## Rendimiento

Con un proyecto sintético de 5000 SRS, 500 SSS, 3000 subprogramas, 300 pruebas y 1000
secciones (10 422 nodos y 36 324 aristas):
- construir, guardar y cargar el grafo: menos de 0,3 s;
- `trace` o `impact`: milisegundos (la primera consulta que necesita saber qué pruebas
  cubren cada elemento lo calcula una vez, 0,3 s);
- la matriz de los 5000 SRS: 0,35 s; `gaps`: 0,2 s; las comunidades: 0,3 s.

## Como librería

Las consultas son las del antiguo `ada_graph_mcp.py`, más las de trazabilidad:

```python
from atlas.config import load_config
from atlas.graph import KnowledgeGraph
g = KnowledgeGraph.from_config(load_config("atlas.dev.toml"))
g.impact("Delete_Track", depth=3)          # lo que depende de él, y los requisitos y pruebas afectados
g.trace("SRS-0004")                       # DOORS, documentos, código y pruebas del requisito
g.read_source("tracks.update")            # el código, con números de línea
g.at("src/tracks.adb", 12)                # ("tracks.delete_track", None): lo que hay en esa línea
```

`examples/library/traceability.py` construye el grafo de las muestras y hace estas
consultas.

## Pruebas

`tests/test_graph.py`: almacenamiento, exportación, consultas, nombres repetidos en dos
paquetes, los tres orígenes juntos, `trace`, `trace_code`, `impact`, `matrix`, `gaps`, los
procedimientos de prueba de DOORS y `link_semantics`, `meta` de la matriz y la fecha de la
exportación, `neighbors`, comunidades, mismo grafo que `ada_graph.py` y una tabla de casos de
`values_differ`. `tests/test_cli.py`: el paso `graph.build` incremental por origen, `atlas
trace` (Markdown y `--json`), `atlas matrix` (CSV con BOM y `;`, otro delimitador, cabecera de
procedencia, JSON, atributos, `--json`) y `--json` en `graph` y `status`; `write_matrix` y la
hoja XLSX, en `test_graph.py`.
