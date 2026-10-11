# atlas.pipeline — motor incremental y recuperable

El motor de `atlas run`. Procesa cada unidad (un fichero, un documento, un módulo de DOORS)
una sola vez, mientras no cambie, y sobrevive a cualquier corte. No sabe nada de
documentos ni de código: los pasos se le dan desde fuera.

## Conceptos

| Pieza | Qué es |
|---|---|
| `Step` | un paso con nombre: sus unidades, la huella de cada una, cómo procesarla, qué hacer con una que desaparece (`prune`), de qué pasos depende y, opcionalmente, una preparación común (`prepare`) |
| huella | un hash de lo que afecta a la unidad: su contenido, la configuración que la toca y la versión del código que la procesa |
| `Ledger` | el libro de registro, una tabla de `atlas.db`: por paso y unidad, huella, estado (`ok`, `error`, `quarantined`, `stale`, `running`), intentos, error, fechas y segundos |
| `Runner` | ejecuta los pasos en el orden de sus dependencias |

Para cada unidad, el `Runner` mira el libro: si la huella no ha cambiado y la unidad está
bien, la salta; si no, la procesa. Un error de una unidad se anota y se sigue (tras
`max_attempts` intentos, la unidad queda en cuarentena hasta que cambie). Un error fatal
(conexión, credenciales, cupo agotado) para la ejecución con el estado guardado. Las
unidades que desaparecen se podan.

## Garantías

- **Reanudación.** `atlas run` otra vez sigue donde se quedó: no hay un comando aparte para
  reintentar.
- **Atomicidad.** Los ficheros se escriben en un temporal y se renombran
  (`atomic_write`); en SQLite, una transacción por unidad. Un corte deja la unidad como no
  hecha, nunca a medias.
- **Un proceso por workspace.** Un bloqueo del sistema operativo sobre `atlas.lock`, que el
  sistema suelta aunque el proceso muera con `kill -9`.
- **Ctrl+C con varios hilos.** Las unidades que no han empezado se cancelan; las que están
  en marcha reciben el aviso (`Runner.stopping`) y las largas, como un documento, no
  empiezan más peticiones. El bloqueo no se suelta hasta que terminan.

## Paralelismo

Las unidades de un paso paralelo van en `[pipeline] workers` hilos, o en los de su paso en
`[pipeline.step_workers]`. Cada paso de atlas lo aprovecha a su manera:

| Paso | Unidad | En paralelo |
|---|---|---|
| `doors.requirements` | módulo | no |
| `code.parse` | fichero | sí |
| `docs.process` | documento | sí, y las secciones de todos los documentos comparten `workers` peticiones al modelo |
| `graph.build` | origen (`code`, `doors`, `docs`) | no |
| `index.documents`, `index.requirements` | documento, módulo | sí, y antes `prepare` embebe los textos de todos en lotes llenos |

`Step.prepare` recibe todas las unidades pendientes antes de procesarlas, para hacer juntas
las cosas que les sirven a todas. En el índice, los textos de todos los documentos van en
lotes completos de `[embeddings] batch`, varios lotes a la vez. Si la preparación falla,
cada unidad hace su trabajo como antes.

## Perfiles de tiempo

Cada ejecución deja su perfil en el informe de `atlas run` y en la tabla `runs` de
`atlas.db`, con la versión de atlas. Por paso:
- `seconds`: el tiempo total del paso;
- `scan_seconds`: lo que costó encontrar las unidades y sus huellas;
- `prepare_seconds`: lo que costó la preparación común;
- `units` y `ran`: cuántas unidades hay y cuántas se procesaron;
- `unit_seconds` (total, mediana, máximo) y las tres más lentas (`slowest`);
- `api`: lo que el paso pidió a las API, con los contadores de `atlas.llm.usage()`:
  peticiones de chat y de embeddings, rechazos por cupo, reintentos, segundos esperando
  al limitador (`wait_seconds`) y al servidor (`api_seconds`), y kB enviados.

`atlas status` muestra el de la última ejecución y un resumen de las anteriores:

```
last run: 2026-10-11T09:51:44, atlas 0.1.0, 16.9 s
  step                  seconds   scan  units    ran  slowest unit; API
  doors.requirements       0.07   0.00      2      2  SRS_LARGE (0.07 s)
  code.parse               1.14   0.02    660    660  pkg_000.adb (0.03 s)
  docs.process            12.55   0.00     20     20  spec_00.pdf (2.66 s); 540 chat
  graph.build              1.61   0.09      3      3  docs (0.68 s)
  index.documents          0.86   0.00     20     20  spec_16.pdf (0.03 s); 103 embeddings
  index.requirements       0.70   0.02      2      2  SRS_LARGE (0.08 s); 86 embeddings
```

## Mediciones

Proyecto grande sintético (`make large`: 20 PDF de 260 páginas, 660 ficheros Ada, 5500
requisitos), con la API falsa:

| Ejecución | Tiempo | Peticiones |
|---|---|---|
| primera | 17,2 s | 540 de chat, 189 de embeddings (12 060 textos) |
| segunda, sin cambios | 0,1 s | ninguna |
| tras cambiar un plazo en una página de un PDF | 1,4 s | 1 de chat, 1 de embeddings |

Con la API falsa no hay espera: casi todo el tiempo es CPU, sobre todo la extracción de los
PDF (unos 50 ms por página, en Python puro), y procesar los documentos en paralelo no lo
acorta (entre 17 y 18 s antes y después de la fase 6). Donde se nota es con un modelo que
tarda. Con uno simulado de 1 s por petición y 4 hilos, sobre 6 PDF, la fase 6 bajó la
primera ejecución de 68,1 s a 51,6 s:
- `docs.process`, de 50,6 a 43,2 s (el mínimo, 40,5 s), por procesar los documentos en
  paralelo: la extracción de unos se solapa con las respuestas de otros, y ya no se espera
  al final de cada documento;
- los embeddings, de 30 a 26 peticiones, y `index.requirements`, de 10,2 a 3,1 s, por los
  lotes llenos.

## Como librería

```python
from pathlib import Path
from atlas.pipeline import Ledger, Runner, Step, sha256_file

step = Step(
    name="lines",
    units=lambda ctx: [str(p) for p in Path("src").glob("*.adb")],
    fingerprint=lambda ctx, f: sha256_file(f),
    run=lambda ctx, f: {"lines": len(Path(f).read_text().splitlines())},   # idempotente
    prune=lambda ctx, f: None,                                             # una unidad que desaparece
)
ledger = Ledger("work/atlas.db")
report = Runner(ledger, [step], workers=4).run()
print(report.processed, report.skipped, report.profile["lines"]["seconds"])
print(ledger.runs(1)[0]["report"]["profile"])
```

`examples/library/pipeline.py` es este ejemplo completo.

## Pruebas

`tests/test_pipeline.py`: reanudación tras un corte y tras `kill -9`, cuarentena, poda,
dependencias, varios hilos sobre el mismo `atlas.db`, perfiles, `step_workers`, Ctrl+C con
unidades en otros hilos y `prepare`. `tests/test_large.py`: la segunda ejecución y un PDF
cambiado sobre una copia pequeña del proyecto grande.
