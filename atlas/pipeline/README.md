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
- **Presupuesto.** `Runner(deadline=<instante>, max_requests=N)` para la ejecución limpia:
  ver más abajo.

## Estimación: `dry_run` y `Step.estimate`

`Runner(..., dry_run=True).run()` recorre las unidades y las huellas de cada paso como una
ejecución, pero sin tomar el bloqueo (puede mirar mientras otro proceso trabaja), sin
procesar ni podar nada y sin guardar la ejecución. El informe dice, por paso, `pending`
(unidades por procesar) y `estimate`, lo que devuelve el `estimate` del paso con esas
unidades:

```python
Step(..., estimate=lambda ctx, units: {"requests": {"chat": 2 * len(units)}, "seconds": [60, 66],
                                       "text": "40 chat requests, about 1 min at 28/min"})
```

Todas las claves son opcionales. Un paso cuyas dependencias tienen unidades pendientes
lleva `after` con ellas: no se puede estimar lo que aún no han producido. En una ejecución
normal el `Runner` también lo pide al empezar cada paso, lo escribe en el registro y lo deja
en el informe; si falla, el paso sigue sin estimación.

En atlas (`atlas estimate`, `atlas status` y el principio de cada paso de `atlas run`):

| Paso | Peticiones |
|---|---|
| `docs.process` | las secciones cuyo análisis no está en la caché (`pending_requests`, con las mismas secciones que `preview`), más el resumen de cada documento al que le falte; hasta un 10 % más por las secciones que se dividen |
| `index.*` | los lotes de `[embeddings] batch` de los textos sin vector en la caché (`embed_missing(dry_run=True)`) |

Tiempo = máx(peticiones / `requests_per_minute`, peticiones × latencia / peticiones a la
vez), con la latencia del último perfil del paso en la tabla `runs` (`api_seconds` entre
sus peticiones) o la de `atlas estimate --latency S`. En el proyecto grande, con todo por
hacer, la estimación da las 540 peticiones de chat medidas; con los documentos, DOORS y el
código ya procesados, las 103 + 86 + 48 = 237 de embeddings.

## Presupuesto y progreso

`Runner(deadline=time.time() + 3600, max_requests=1000)`: antes de empezar cada unidad, y
cada vez que una unidad larga mira `stopping` (un `Stopping`, el `Event` que consulta el
presupuesto al preguntarle), el `Runner` comprueba si pasó el instante o si los contadores
de peticiones de `meters` (`chat`, `embeddings`, `other`) crecieron `max_requests` desde el
principio. No hay hilo temporizador. Al llegar al límite:
- las unidades que no han empezado se quedan para la próxima vez;
- un documento a medias deja de pedir secciones y acaba con `Interrupted`, que el `Runner`
  absorbe: la unidad queda `running` en el libro, como tras un `kill`, y se rehace (sus
  secciones ya están en la caché);
- los pasos siguientes no empiezan;
- el informe, y la tabla `runs`, dicen `stopped_by = "budget"`, y `atlas run` sale con el
  código 3.

El trabajo de `prepare` (los embeddings de todo el índice en lotes) no se corta a mitad:
cuenta para `max_requests`, pero el límite se mira antes y después.

`progress` recibe una línea cada 30 s o cada 25 unidades terminadas de un paso; `atlas run`
la escribe por stderr. Lo que queda sale de las peticiones que esperaba la estimación y de
las que ya se han hecho o, si no hay estimación, de las unidades:

```
docs.process 120/2080 units, 35 min, ~2 h 10 min left
```

## Paralelismo

Las unidades de un paso paralelo van en `[pipeline] workers` hilos, o en los de su paso en
`[pipeline.step_workers]`. Cada paso de atlas lo aprovecha a su manera:

| Paso | Unidad | En paralelo |
|---|---|---|
| `doors.requirements` | módulo | no |
| `code.parse` | fichero | sí |
| `docs.process` | documento | sí, y las secciones de todos los documentos comparten `workers` peticiones al modelo |
| `graph.build` | origen (`code`, `doors`, `docs`) | no |
| `index.documents`, `index.requirements`, `index.code` | documento, módulo, unidad de código | sí, y antes `prepare` embebe los textos de todos en lotes llenos |

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
last run: 2026-10-11T16:15:39, atlas 0.1.0, 6.9 s
  step                  seconds   scan  units    ran  slowest unit; API
  doors.requirements       0.07   0.00      2      2  SRS_LARGE (0.07 s)
  code.parse               1.28   0.02    660    660  pkg_000.ads (0.03 s)
  docs.process             1.21   0.00     20     20  spec_03.pdf (0.35 s); 540 chat
  graph.build              1.57   0.05      3      3  docs (0.67 s)
  index.documents          0.85   0.00     20     20  spec_17.pdf (0.03 s); 103 embeddings
  index.requirements       0.70   0.02      2      2  SRS_LARGE (0.08 s); 86 embeddings
  index.code               1.25   0.03    360    360  pkg_248 (0.01 s); 48 embeddings
```

## Mediciones

Proyecto grande sintético (`make large`: 20 PDF de 260 páginas, 660 ficheros Ada, 5500
requisitos), con la API falsa:

| Ejecución | Tiempo | Peticiones |
|---|---|---|
| primera | 7,2 s | 540 de chat, 237 de embeddings (15 120 textos) |
| segunda, sin cambios | 0,1 s | ninguna |
| tras cambiar un plazo en una página de un PDF | 0,9 s | 1 de chat, 1 de embeddings |

Hasta la 0.1.0 los PDF se extraían con markitdown (pdfminer, Python puro, unos 45 ms por
página) y la primera ejecución tardaba 17,2 s, 12,6 de ellos en `docs.process`. Con pymupdf
(1,5 ms por página) `docs.process` tarda 1,3 s. Indexar el código (`index.code`, 3060
subprogramas, tipos y variables en 360 unidades) añade 1,3 s y 48 peticiones de embeddings. Con el tamaño de la primera meta del plan (80
PDF de 1040 páginas y 2080 secciones, 20 910 requisitos y 3060 subprogramas), medido con
markitdown, la primera ejecución tardaba 62 s y la segunda, sin cambios, 0,3 s y ninguna
petición.

Con la API falsa no hay espera: el tiempo es CPU, y procesar los documentos en paralelo no lo
acorta. Donde se nota es con un modelo que tarda. Con uno simulado de 1 s por petición y 4 hilos, sobre 6 PDF, la fase 6 bajó la
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
unidades en otros hilos, `prepare`, `dry_run` y `estimate`, el presupuesto (por peticiones,
por hora y con una unidad a medias) y las líneas de progreso. `tests/test_cli.py`: `atlas
estimate` y una ejecución con `max_requests` contra el servidor falso lento, que la
siguiente termina sin repetir peticiones. `tests/test_large.py`: la segunda ejecución y un PDF
cambiado sobre una copia pequeña del proyecto grande.
