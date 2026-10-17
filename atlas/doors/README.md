# atlas.doors — requisitos

Lee los requisitos oficiales y sus enlaces. atlas lee siempre de una **base de
intercambio**: una base SQLite con un esquema fijo y versionado (`exchange.py`).

| Fichero | Qué tiene |
|---|---|
| `exchange.py` | el esquema de la base de intercambio, `ExchangeWriter` (para rellenarla) y su lectura |
| `__init__.py` | los adaptadores `exchange` y `database`, normalización de IDs y diferencias entre versiones de un módulo |

## La base de intercambio

| Tabla | Qué guarda |
|---|---|
| `exchange_modules` | módulos, con una instantánea que cambia con su contenido, y cuándo y de dónde se exportaron (`filled_at`, `source`) |
| `exchange_requirements` | ID, texto, título, tipo (`requirement`, `heading` o `information`), padre, orden, nivel, numeración y atributos |
| `exchange_links` | enlaces ya tipados |

Lo que cambia de un proyecto a otro es **quién la rellena**: cada relleno hace su propia
correspondencia con la fuente, y atlas no necesita saber de dónde vienen los datos.

```
DOORS ─► tools/doors_export ─┐
ReqIF, hoja de cálculo... ───┼─► requirements.sqlite ─► atlas (adapter = "exchange")
```

- **DOORS**: `tools/doors_export` descarga en bruto desde DOORS (en el PC con DOORS) y
  publica la base aplicando `mapping.toml`. Ese fichero dice qué atributo da el ID y el
  texto, qué atributos pasan, cómo se llama cada módulo y qué tipo es cada módulo de
  enlaces. Ver [su README](../../tools/doors_export/README.md).
- **Otro origen**: basta con escribir la base con `ExchangeWriter`, que comprueba el
  contrato: IDs únicos, tipos válidos y enlaces que salen del propio módulo.

En `atlas.toml` solo se dice dónde está la base y cómo casan los IDs con documentos y
código:

```toml
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "C:/atlas/tms/requirements.sqlite"
[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-\\d{3,6}$"
inline_reference_regex = "\\b(?:SRS|SSS)-\\d{3,6}\\b"   # "see SSS-0012" en el texto es un enlace
```

`id_pattern` sirve también, sin sus anclas, para encontrar los IDs en las etiquetas `Req:` del
código y en los requisitos de los documentos; `id_regex` la sustituye si el código los escribe
de otra forma. Si el código escribe variantes (`srs-4`, `SRS 0004`), `id_canonical` las lleva
a la forma de DOORS sin tocar los IDs de DOORS:

```toml
id_regex = "(?i)\\b(?:SRS|SSS)[-_ ]?\\d+\\b"
id_canonical = { regex = "^(SRS|SSS)[-_ ]?0*(\\d+)$", format = "{1}-{2:04d}" }
```

Qué formas hay de verdad lo dice `atlas graph`: las formas de ID de cada origen (código,
DOORS, documentos), con su prefijo, su separador y sus cifras.

Para leer directamente otra base con consultas propias queda el adaptador `database`
(`[sources.doors.database]`, SQLite u ODBC). Sus claves están en la
[guía de configuración](../../docs/configuracion.md).

## En `atlas run`

El paso `doors.requirements` tiene una unidad por módulo, cuya huella es la instantánea del
módulo: un módulo que no cambia no se vuelve a leer. Al leerlo, guarda en `atlas.db` sus
requisitos y enlaces, y anota cuántos se han añadido, quitado o cambiado. Después, el grafo
y el índice toman de ahí lo que necesitan.

## Los atributos que se enseñan

Qué atributos de DOORS entran en atlas lo decide el relleno (en el exportador, `[attributes]`
de `mapping.toml`; en el adaptador `database`, `attribute_columns`). De esos, `[sources.doors]
attributes` dice cuáles enseña atlas: las columnas de `atlas matrix` y lo que dan `matrix` y
`list_requirements` de cada requisito (por defecto `status` y `verification`). Los nombres no
distinguen mayúsculas. La búsqueda usará la misma lista para filtrar.

## La fecha de la exportación

Cada módulo de la base de intercambio lleva cuándo y de dónde se exportó
(`exchange_modules.filled_at` y `source`, que escribe el relleno). El adaptador lo da con
`exported_at(módulo)` y `module_info(módulo)` (`{"exported_at", "source"}`); el adaptador
`database` no lo sabe y devuelve `""`. Un adaptador propio puede dar la suya: los dos métodos
de `RequirementsAdapter` no son abstractos.

El paso `doors.requirements` la guarda en la tabla `modules` de `atlas.db` (`module`,
`exported_at`, `source` y `run_started`, cuándo empezó la ejecución que lo leyó). Una
exportación nueva del mismo contenido también se vuelve a leer, por su fecha; no cuesta
peticiones. De ahí la toman:
- `atlas status`: `DOORS: 3 modules, exported 2026-10-14, 9 days ago` (si los módulos tienen
  fechas distintas, el intervalo y la más antigua);
- `atlas graph`, junto a la fecha de construcción del grafo, con la fecha y el origen de cada
  módulo;
- la cabecera de `atlas matrix` (el `.md`) y una columna `exported_at` del CSV;
- `trace`, `get_requirement` y `build_context`, en un campo `exported_at`.

Con `[sources.doors] max_age_days = N`, `atlas run` y `atlas serve` avisan en el registro de
cada módulo exportado hace más de N días.

## Como librería

```python
from atlas.doors import ExchangeAdapter, diff_requirements
ad = ExchangeAdapter({"exchange": {"path": "requirements.sqlite"}})
reqs = list(ad.requirements("SRS_TMS"))
links = list(ad.links("SRS_TMS"))
ad.module_info("SRS_TMS")      # {"exported_at": "2026-10-14T08:00:00+00:00", "source": "/Proyecto/SRS"}
print(diff_requirements({"SRS-0001": "old hash"}, reqs))     # añadidos, cambiados, quitados
```

## Pruebas

`tests/test_doors.py` (adaptador `database`), `tests/test_doors_exchange.py` (contrato de la
base y adaptador `exchange`) y `tests/test_doors_export.py` (el exportador con un DOORS
simulado, desde la descarga hasta `atlas run`).
