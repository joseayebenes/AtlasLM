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

## Consultas de trazabilidad

Sin modelo:

| Consulta | Devuelve |
|---|---|
| `trace(req)` | texto y atributos de DOORS; padre, hijos y enlaces; las secciones que lo declaran, con su texto y las cifras que difieren de DOORS; el código que lo implementa; las pruebas que lo verifican (lo citan) o lo cubren (llaman, directa o indirectamente, a ese código) |
| `trace_code(nombre)` | requisitos, llamadores, llamados, pruebas y secciones de un elemento de código; de una prueba, el código que ejercita y los requisitos que cubre |
| `impact(nombre)` | de código: lo que depende de él (lo llama, convierte a su tipo, usa su variable), y los requisitos, pruebas y secciones afectados, incluidos los requisitos que esos satisfacen en DOORS; de un requisito: los que derivan de él, su código, pruebas y secciones |
| `matrix(módulo)` | requisito → código → pruebas → documentos, con resumen y huecos |
| `gaps(módulo)` | requisitos sin código o sin pruebas; subprogramas sin requisito; IDs del código o de los documentos que no están en DOORS; cifras distintas entre DOORS y los documentos |
| `neighbors(nodo)` | el subgrafo alrededor de un nodo, en JSON o en Mermaid |
| `communities()` | grupos de código que trabajan juntos, con sus unidades y requisitos |

Un requisito de un módulo cuyos derivados de otro módulo tienen código cuenta como
implementado a través de ellos: un SSS lo está si lo están los SRS que lo satisfacen. Los
enlaces dentro de un mismo módulo (referencias, refinamientos) no cuentan como derivación.

`atlas matrix SRS_TMS` escribe la matriz en `<workspace>/graph/matrix-SRS_TMS.csv` (para una
hoja de cálculo) y `.md`, y muestra el resumen y los huecos; `atlas matrix "*"` la hace con
todos los requisitos. Con las muestras:

```
40 requirements: 4 with code, 1 with tests, 40 in documents.
without code (36): SRS-0005, SRS-0006, …
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
paquetes, los tres orígenes juntos, `trace`, `trace_code`, `impact`, `matrix`, `gaps`,
`neighbors`, comunidades y mismo grafo que `ada_graph.py`. `tests/test_cli.py`: el paso
`graph.build` incremental por origen, `atlas trace` y `atlas matrix`.
