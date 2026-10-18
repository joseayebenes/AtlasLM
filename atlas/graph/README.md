# atlas.graph — grafo y trazabilidad

Junta en un solo grafo el código, los requisitos de DOORS y los documentos, y responde sin
modelo a las preguntas de trazabilidad e impacto.

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
paquetes pueden tener un `Update` cada uno. Dos subprogramas con el mismo nombre en un paquete
(sobrecargas) son un solo nodo, con los requisitos y las llamadas de todos, desde la primera
línea del primero hasta la última del último, y `bodies`, las líneas `[inicio, fin]` de cada
uno (con su fichero si no es el del nodo); `read_source` los enseña todos, uno tras otro. Cada
subprograma lleva su clase en `subprogram_kind` (`procedure`, `function`, `operator`, `task`,
`entry`, `protected`), y el resumen de un nodo la dice cuando no es un procedimiento ni una
función. Cada `Nombre (...)` de un cuerpo se clasifica según lo que es `Nombre`:
- un subprograma: es una llamada;
- un tipo: es una conversión;
- una variable de paquete: es un uso;
- un nombre del lenguaje (`Integer`, `Float`...): se ignora. Cuáles son lo dice el
  analizador del lenguaje de la unidad que llama (`CodeAnalyzer.predefined`, ver
  [atlas.code](../code/README.md#la-interfaz-codeunit));
- algo desconocido: es una llamada externa.

Un nombre se busca, por este orden:
1. como está escrito (con su paquete) y en la propia unidad;
2. si es un alias de la unidad (un renombramiento o una instancia genérica), como lo que
   nombra: `Log` que renombra `Ada.Text_IO.Put_Line`, `Int_Stack.Push` con `Int_Stack`
   instancia de `Stacks`, o `Store.Write` si `Write` renombra un subprograma de `Store`;
3. sin paquete, en los paquetes de las cláusulas `use` de la unidad (las de su
   especificación también valen en el cuerpo);
4. en todas las unidades, prefiriendo las que se importan con `with`.

Si aún hay varias, la arista va a todas, para que el análisis de impacto no se deje
ninguna; `atlas graph` las cuenta como llamadas ambiguas, y cuenta también las resueltas
por un alias o por un `use`. Un alias de algo de fuera del proyecto es una llamada externa
con su nombre completo (`ada.text_io.put_line`).
Una llamada que solo se encuentra en unidades que el llamador no importa (`calls to units not
imported`, `unimported_calls`) es la otra suposición de la resolución: una sola candidata no
es ambigua, pero puede ser la equivocada.

Un subprograma lleva en su nodo, si el analizador los da, `subprogram_kind` (`procedure`,
`function`, `method`) y `signature` (su perfil en una línea), que `get_node` enseña.

### Precisión de la resolución por nombres

Antes de pedir las referencias al Ada Language Server en la construcción del grafo (un paso
`code.resolve` con `textDocument/definition` por cada llamada) se midió cuánto falla la
resolución por nombres. Con la versión de octubre de 2026 (antes y después de dar posición a
las llamadas, las mismas aristas):

| Código | Ficheros | Sitios de llamada | Aristas `calls` | Ambiguas | A unidades no importadas | Sin resolver |
|---|---|---|---|---|---|---|
| `examples/samples/ada` | 5 | 9 | 4 | 0 | 0 | 1 |
| `make large` (300 paquetes, 3000 subprogramas) | 660 | 6060 | 6059 | 0 | 0 | 0 |
| atlas-socket-demo, `src` y `tests` (el servidor que escribieron los agentes) | 12 | 226 | 67 | 0 | 0 | 46 |
| un proyecto Ada pequeño de prueba | 4 | 28 | 7 | 0 | 0 | 5 |

Las llamadas sin resolver son nombres del runtime que llegan con `use` (`To_Unbounded_String`,
`Receive_Socket`): aristas a un nodo externo, que no cambian el impacto dentro del
proyecto. Con el ALS de la extensión de VS Code (2027.0) sobre atlas-socket-demo, de 226
peticiones, 82 apuntan al proyecto y las 82 dan la misma arista que la resolución por
nombres; 120 apuntan al runtime y 24 no tienen respuesta. Coste: unos 10 s en arrancar, la
primera petición no contesta en 30 s mientras el servidor indexa y después 77 ms de
mediana, en total 96 s para 226 llamadas. En `make large`, 6060 peticiones: 7,6 s en
arrancar, la primera sin respuesta en 30 s, 11 ms de mediana y 364 s en total (unos 6
minutos más en cada construcción del grafo en la que cambie el código); 6059 apuntan al
proyecto, que es lo que ya decía la resolución por nombres.

Por eso el paso no se ha hecho: en estos proyectos no corregiría ninguna arista y añadiría
minutos a cada construcción. Lo que sí está es la posición de cada llamada en el
`CodeUnit`, que es lo que ese paso necesita. Hay que repetir la medida con el proyecto real
(`atlas graph`: `ambiguous calls` y `calls to units not imported`); si salen cientos, el
paso tiene sentido. Las llamadas sin resolver por `use` se arreglan antes y más barato
resolviendo los nombres contra las unidades de los `use` (sección 6.2 del análisis de
mejoras).

Los ficheros que casan con `[sources.code] test_patterns` son pruebas: sus subprogramas son
nodos `test`. Un elemento que cita un requisito lo implementa (`implements`); una prueba
que lo cita lo verifica (`verifies`).

**Requisitos.** Un requisito es el mismo nodo en las tres partes. Sus IDs se normalizan
igual que los de DOORS (`[sources.doors.convention] id_normalize`; mayúsculas por
defecto), así que `Req: srs-0004` en el código y `SRS-0004` en DOORS se encuentran. Con
`id_canonical`, las variantes del código y de los documentos (`SRS-4`, `SRS 0004`) toman la
forma de DOORS; los IDs de DOORS no cambian. El ID que el modelo da a un requisito de un
documento se separa con la expresión de un ID de la convención: «SRS-0004 / SRS-0005» son
dos requisitos.

Las estadísticas de cada parte llevan `id_shapes`, las formas de ID tal como se escriben
(prefijo, separador, cifras y cuántas veces se escribe cada una), y `atlas graph` las
enseña. Con `atlas.test.toml`:

```
ID shapes as written, by origin (N = a digit; how many times each one is written):
  code   6 SRS-NNNN
  docs   336 SRS-NNNN
  doors  40 SRS-NNNN, 12 SSS-NNNN
```

**Documentos.** Una sección nombra un elemento de código cuando escribe su nombre completo
(`Tracks.Delete_Track`) o un nombre con guion bajo que solo tiene un elemento
(`Delete_Track`). Las palabras sueltas, como `Update`, son demasiado comunes para contar.

El grafo completo se exporta a `<workspace>/graph/atlas.graphml` (Gephi, yEd) y
`atlas.json`. Con `[graph] communities = true`, cada elemento de código lleva su grupo
(Louvain), para colorearlo en Gephi.

## Versión y autoridad de los documentos

El nodo `document` lleva `doc_id`, `version` y `date` (de `structure.json`, ver
`[[sources.docs]] doc_id_regex`, `version_regex` y `date_regex`) y la `authority` de su
fuente (1 = normativo, 2..., 0 = sin autoridad). Las secciones que devuelven `trace`,
`trace_code` e `impact` llevan la `version` de su documento y su `page_start`, y la columna
`documents` de `matrix` los escribe así: `spec.pdf v2 §3.1 Timing p.12`.

Un requisito que no está en DOORS se compara con la declaración del documento de mayor
autoridad que lo declara: `values_differ` dice entonces `against` (esa sección), `official`
(sus cifras) y `document`. Sin ninguna autoridad no hay referencia, pero `gaps` lista igual en
`documents_disagree` los requisitos que varios documentos declaran con cifras distintas. Dos
documentos con el mismo `doc_id` y versiones distintas solo dan un aviso en `atlas graph` (y
en `superseded` de las estadísticas de la parte `docs`): los dos siguen en el grafo.

## Consultas de trazabilidad

Sin modelo:

| Consulta | Devuelve |
|---|---|
| `trace(req)` | texto y atributos de DOORS, y la fecha de la exportación de su módulo (`exported_at`); padre, hijos y enlaces; las secciones que lo declaran, con su texto y las cifras que difieren de DOORS (solo `kind` `value`); los requisitos de los que deriva en otros módulos, hasta el de sistema (`ancestors`, 5 niveles, con el tipo de cada salto); el código que lo implementa; las pruebas del código que lo verifican (lo citan) o lo cubren (llaman, directa o indirectamente, a ese código), en `verified_by`; los procedimientos de prueba de DOORS que lo verifican, en `procedures` |
| `trace_code(nombre)` | requisitos, llamadores, llamados, pruebas y secciones de un elemento de código; de una prueba, el código que ejercita y los requisitos que cubre |
| `impact(nombre)` | de código: lo que depende de él (lo llama, convierte a su tipo, usa su variable), y los requisitos, pruebas, procedimientos de prueba y secciones afectados, incluidos los requisitos de los que esos derivan en DOORS; de un requisito: los que derivan de él, su código, pruebas, procedimientos y secciones |
| `matrix(módulo)` | requisito → código → pruebas → procedimientos de prueba → documentos, con resumen, huecos y `meta`: de qué exportación de DOORS sale |
| `gaps(módulo)` | requisitos sin código, sin pruebas o sin procedimiento de prueba; requisitos sin enlace de derivación hacia otro módulo, por módulo; subprogramas sin requisito; IDs del código o de los documentos que no están en DOORS; IDs que DOORS enlaza en módulos que no se han leído, con quién los enlaza y con qué tipo; cifras distintas entre DOORS y los documentos (solo `kind` `value`; o, fuera de DOORS, frente al documento de mayor autoridad); sin módulo, `documents_disagree`: requisitos fuera de DOORS que varios documentos declaran con cifras distintas |
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

`matrix` y `gaps` guardan por instancia lo que calculan de cada requisito (derivados,
pruebas, secciones) y los huecos de cada módulo; `matrix` construye solo las filas de la
página pedida, y el resumen y los huecos salen de los conjuntos de requisitos sin código o
sin pruebas, que se calculan una vez a partir de las aristas. Cada construcción del grafo es
un `KnowledgeGraph` nuevo (`GraphTools.graph`), así que nada queda viejo.

Medido el 17-10-2026 en tiempo de CPU, alternando en el mismo proceso la versión anterior y
la nueva (el mínimo de siete; la máquina estaba muy cargada, con una carga media de 40 a 60,
así que las cifras absolutas son el doble o el triple que en una máquina libre y lo que
cuenta es la proporción), cada consulta en un `KnowledgeGraph` recién creado, con las pruebas
que cubren cada elemento ya calculadas:

| Consulta | `make large` (5000 SRS) antes | después |
|---|---|---|
| `matrix("SRS_LARGE", limit=50)` | 0,33 s | 0,05 s |
| `matrix("SRS_LARGE", limit=50, offset=2500)` | 0,52 s | 0,08 s |
| `gaps("SRS_LARGE")` | 0,49 s | 0,27 s |
| `gaps()` | 0,60 s | 0,28 s |

Con una sonda en memoria de 20 000 SRS, 2000 SSS, 3000 subprogramas en seis capas, 300
pruebas y 2000 secciones, `matrix(limit=50)` pasa de 3,1 s a 0,1-0,4 s (tanto la primera
página como otra) y `gaps` de 2,0-2,7 s a 0,6-0,9 s; otra página de la misma matriz, ya
calculados los huecos, 0,03 s. Lo que queda en `gaps` es comparar las cifras de cada
requisito con las de sus secciones, que necesita el texto de todas.

`python examples/large_project.py --graph` repite la medida sobre un `make large` ya
ejecutado, y `make large` la hace al final.

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
paquetes, sobrecargas, tareas y operadores, `use` y alias, los nombres predefinidos según el
lenguaje del llamador, los tres orígenes juntos, `trace`, `trace_code`, `impact`, `matrix`, `gaps`, los
procedimientos de prueba de DOORS y `link_semantics`, `meta` de la matriz y la fecha de la
exportación, `neighbors`, comunidades, mismo grafo que `ada_graph.py` y una tabla de casos de
`values_differ`. `tests/test_cli.py`: el paso `graph.build` incremental por origen, `atlas
trace` (Markdown y `--json`), `atlas matrix` (CSV con BOM y `;`, otro delimitador, cabecera de
procedencia, JSON, atributos, `--json`) y `--json` en `graph` y `status`; `write_matrix` y la
hoja XLSX, en `test_graph.py`.
