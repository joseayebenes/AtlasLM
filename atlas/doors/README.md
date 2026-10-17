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
| `exchange_modules` | módulos, con una instantánea que cambia con su contenido |
| `exchange_requirements` | ID, texto, título, tipo (`requirement`, `heading` o `information`), padre, orden, nivel, numeración y atributos |
| `exchange_links` | enlaces ya tipados |

Lo que cambia de un proyecto a otro es **quién la rellena**: cada relleno hace su propia
correspondencia con la fuente, y atlas no necesita saber de dónde vienen los datos.

```
DOORS ─► tools/doors_export ─┐
ReqIF ─► tools/reqif_import ─┼─► requirements.sqlite ─► atlas (adapter = "exchange")
hoja de cálculo... ──────────┘
```

- **DOORS**: `tools/doors_export` descarga en bruto desde DOORS (en el PC con DOORS) y
  publica la base aplicando `mapping.toml`. Ese fichero dice qué atributo da el ID y el
  texto, qué atributos pasan, cómo se llama cada módulo y qué tipo es cada módulo de
  enlaces. Ver [su README](../../tools/doors_export/README.md).
- **ReqIF**: `tools/reqif_import` lee uno o varios `.reqif` o `.reqifz` (lo que entrega
  DOORS cuando no hay acceso por COM, o lo que manda el cliente) en cualquier equipo, sin
  dependencias, con el mismo `mapping.toml`: `SPECIFICATION` → módulo, `SPEC-HIERARCHY` →
  padre, orden y nivel, `SPEC-OBJECT` → requisito, `SPEC-RELATION` → enlace tipado. Ver
  [su README](../../tools/reqif_import/README.md).
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

Para leer directamente otra base con consultas propias queda el adaptador `database`
(`[sources.doors.database]`, SQLite u ODBC). Sus claves están en la
[guía de configuración](../../docs/configuracion.md).

## En `atlas run`

El paso `doors.requirements` tiene una unidad por módulo, cuya huella es la instantánea del
módulo: un módulo que no cambia no se vuelve a leer. Al leerlo, guarda en `atlas.db` sus
requisitos y enlaces, y anota cuántos se han añadido, quitado o cambiado. Después, el grafo
y el índice toman de ahí lo que necesitan.

## Como librería

```python
from atlas.doors import ExchangeAdapter, diff_requirements
ad = ExchangeAdapter({"exchange": {"path": "requirements.sqlite"}})
reqs = list(ad.requirements("SRS_TMS"))
links = list(ad.links("SRS_TMS"))
print(diff_requirements({"SRS-0001": "old hash"}, reqs))     # añadidos, cambiados, quitados
```

## Pruebas

`tests/test_doors.py` (adaptador `database`), `tests/test_doors_exchange.py` (contrato de la
base y adaptador `exchange`) y `tests/test_doors_export.py` (el exportador con un DOORS
simulado, desde la descarga hasta `atlas run`).
