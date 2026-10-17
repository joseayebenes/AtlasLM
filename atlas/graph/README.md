# atlas.graph — grafo y trazabilidad

Junta en un solo grafo el código, los requisitos de DOORS y los documentos, y responde sin
modelo a las preguntas de trazabilidad e impacto. Viene de `legacy/ada_graph.py` y
`legacy/ada_graph_mcp.py`.

| Fichero | Qué tiene |
|---|---|
| `build.py` | construcción de las tres partes del grafo |
| `sources.py` | los nombres de documentos y de elementos de código que se buscan en el texto |
| `store.py` | `GraphStore`: el grafo en `atlas.db`, por origen |
| `knowledge.py` | `KnowledgeGraph`: consultas de código y de trazabilidad, exportación |
| `tools.py` | `GraphTools`: las herramientas del grafo para agentes y MCP |

## Construcción

El paso `graph.build` tiene una unidad por origen, y cada parte se reconstruye solo cuando
cambia aquello de lo que sale:

| Parte | Sale de | Nodos | Aristas |
|---|---|---|---|
| `code` | `code.parse` | unidades, subprogramas, pruebas, tipos, variables, requisitos citados | `defines`, `calls`, `converts`, `uses`, `withs`, `implements`, `verifies` |
| `doors` | `doors.requirements` | módulos (`module:SRS_TMS`), requisitos, encabezados | `contains` (módulo y jerarquía) y un enlace por cada enlace de DOORS, con su tipo (`satisfies`...) |
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
| `trace(req)` | texto y atributos de DOORS; padre, hijos y enlaces; las secciones que lo declaran, con su texto y las cifras que difieren de DOORS; el código que lo implementa; las pruebas del código que lo verifican (lo citan) o lo cubren (llaman, directa o indirectamente, a ese código), en `verified_by`; los procedimientos de prueba de DOORS que lo verifican, en `procedures` |
| `trace_code(nombre)` | requisitos, llamadores, llamados, pruebas y secciones de un elemento de código; de una prueba, el código que ejercita y los requisitos que cubre |
| `impact(nombre)` | de código: lo que depende de él (lo llama, convierte a su tipo, usa su variable), y los requisitos, pruebas, procedimientos de prueba y secciones afectados, incluidos los requisitos de los que esos derivan en DOORS; de un requisito: los que derivan de él, su código, pruebas, procedimientos y secciones |
| `matrix(módulo)` | requisito → código → pruebas → procedimientos de prueba → documentos, con resumen y huecos |
| `gaps(módulo)` | requisitos sin código, sin pruebas o sin procedimiento de prueba; subprogramas sin requisito; IDs del código o de los documentos que no están en DOORS; cifras distintas entre DOORS y los documentos |
| `neighbors(nodo)` | el subgrafo alrededor de un nodo, en JSON o en Mermaid |
| `communities()` | grupos de código que trabajan juntos, con sus unidades y requisitos |

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

Las pruebas son dos cosas distintas: las **pruebas del código** (`tests`, `covering_tests`;
subprogramas de los ficheros de `[sources.code] test_patterns`) y los **procedimientos de
prueba** de DOORS (`procedures`; requisitos de un módulo STP con un enlace de tipo
`verifies`). El resumen de la matriz las cuenta por separado (`with_tests`,
`with_procedures`), y los huecos también (`without_tests`, `without_test_procedure`).

`atlas matrix SRS_TMS` escribe la matriz en `<workspace>/graph/matrix-SRS_TMS.csv` (para una
hoja de cálculo) y `.md`, y muestra el resumen y los huecos; `atlas matrix "*"` la hace con
todos los requisitos. Con las muestras:

```
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

La comparación de cifras es el primer paso de la verificación de requisitos con la
documentación: el texto de DOORS de SRS-0004 dice 320 ms y el del PDF de ejemplo, 401 ms.
`atlas verify` (ver [atlas.agents](../agents/README.md)) juzga el resto con el modelo.

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
```

`examples/library/traceability.py` construye el grafo de las muestras y hace estas
consultas.

## Pruebas

`tests/test_graph.py`: almacenamiento, exportación, consultas, nombres repetidos en dos
paquetes, los tres orígenes juntos, `trace`, `trace_code`, `impact`, `matrix`, `gaps`, los
procedimientos de prueba de DOORS y `link_semantics`,
`neighbors`, comunidades y mismo grafo que `ada_graph.py`. `tests/test_cli.py`: el paso
`graph.build` incremental por origen, `atlas trace` y `atlas matrix`.
