# Importador de ReqIF → base de intercambio de atlas

atlas lee siempre los requisitos de la **base de intercambio** (`atlas/doors/exchange.py`).
`tools/doors_export` la rellena desde DOORS, pero necesita Windows, el cliente de DOORS,
`pywin32` y permiso para ejecutar DXL por COM. En muchos proyectos la administración de DOORS
solo entrega ReqIF, o el ReqIF viene del cliente. Este importador rellena la misma base desde
uno o varios ficheros ReqIF (`.reqif`, o `.reqifz`, un zip con ellos), en cualquier equipo y
sin dependencias: solo la biblioteca estándar (`xml.etree`, `zipfile`).

```
SRS.reqif, SSS.reqifz ──reqif_to_exchange.py + mapping.toml──► requirements.sqlite ──► atlas
```

## Uso

```bash
cp tools/reqif_import/mapping.example.toml mapping.toml     # y ajústalo a tu ReqIF
python tools/reqif_import/reqif_to_exchange.py SRS.reqif SSS.reqif --mapping mapping.toml \
    --out requirements.sqlite --id-pattern '^(?:SRS|SSS)-\d{4}$'
```

| Opción | Qué hace |
|---|---|
| `--mapping` | la correspondencia (abajo); sin ella, el ID de cada requisito es el `IDENTIFIER` de su `SPEC-OBJECT` |
| `--out` | la base de intercambio que se rellena (`requirements.sqlite`) |
| `--module NOMBRE` | solo esa especificación, por su nombre en el ReqIF o en atlas; se puede repetir |
| `--keep-others` | no quita de la base los módulos que no se importan ahora |
| `--id-pattern` | avisa de los IDs que no casan (el `id_pattern` de `[sources.doors.convention]`) |

Los ficheros se leen juntos: los enlaces entre módulos de ficheros distintos se resuelven.
Como `publish.py`, la base es de esta importación: los módulos importados antes y no ahora
se quitan, salvo con `--keep-others` o `--module`. Un módulo se escribe entero, en una
transacción, con `ExchangeWriter`, que comprueba el contrato; si un módulo falla (un ID que ya
usa otro módulo), los demás se escriben.

Y en `atlas.toml`, como con el exportador de DOORS:

```toml
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "requirements.sqlite"
```

Con las muestras (`python examples/make_samples.py` escribe
`examples/samples/requirements.reqif` a partir de los mismos requisitos que
`requirements.sqlite`):

```bash
python tools/reqif_import/reqif_to_exchange.py examples/samples/requirements.reqif \
    --mapping tools/reqif_import/mapping.example.toml --out /tmp/requirements.sqlite
```

## Qué se lee

| ReqIF | En la base de intercambio |
|---|---|
| `SPECIFICATION` | un módulo: su `LONG-NAME`, o el de `[modules]` |
| `SPEC-HIERARCHY` | el orden, el nivel (su profundidad) y el padre (el antepasado más cercano con ID) de cada objeto; la numeración se calcula de ahí (1, 1.1, 1.2...) |
| `SPEC-OBJECT` | un requisito: ID, texto, título, tipo y atributos según `mapping.toml`; `source_key` es su `IDENTIFIER` |
| `SPEC-RELATION` | un enlace de su origen a su destino, del tipo de su `SPEC-RELATION-TYPE` |

Los valores: cadenas, números, fechas y booleanos tal cual; un enumerado, el `LONG-NAME` de
sus valores, separados por comas; un XHTML, su texto, con una línea por párrafo, elemento de
lista o salto (las imágenes y los objetos incrustados no). El valor por defecto de una
definición de atributo vale para los objetos que no traen uno.

## La correspondencia

Es el `mapping.toml` de `tools/doors_export` (lo lee su `Mapping`), con los nombres del ReqIF
a la derecha: el `LONG-NAME` de las definiciones de atributos, de las especificaciones y de
los tipos de relación. Ver `mapping.example.toml`.

- `[fields] id`: el primero que tenga el objeto. Un atributo (`"Requirement ID"`), o
  `"identifier"` (el `IDENTIFIER` del `SPEC-OBJECT`: en un ReqIF de DOORS es interno, no el ID
  de documentos y código), `"absolute_number"` (`ReqIF.ForeignID`, el Absolute Number de
  DOORS) o `"module_number"` (el nombre del módulo en atlas y ese número).
- `[fields] text` y `heading`: `"Object Text"` y `"Object Heading"` (los de una
  correspondencia escrita para DOORS) encuentran también `ReqIF.Text` y `ReqIF.ChapterName`,
  los nombres de la guía de implementación de ReqIF; `"Absolute Number"`, `ReqIF.ForeignID`.
- `[attributes]`: cada objeto lleva los que define su `SPEC-OBJECT-TYPE`; los de otro tipo
  (los títulos suelen tener el suyo) no van, ni vacíos.
- `[link_types]`: tipo en atlas = `SPEC-RELATION-TYPE`; los demás, `[links] default_type` o
  el nombre del tipo en minúsculas y con `_`.
- `[kinds]`: como en DOORS; sin la sección, un objeto con texto es `requirement`, uno con
  título y sin texto `heading` y el resto `information`.

Un atributo de la correspondencia que no define ningún tipo de objeto es un error, con los
nombres más parecidos; el texto o el título que falte, un aviso.

## Avisos

Los de `publish.py`, en el registro y en el resumen de cada módulo:
- objetos sin ID, que se quedan fuera, y IDs repetidos, que se quedan con el primero (con el
  `IDENTIFIER` de los dos, para encontrarlos);
- valores del atributo de `[kinds]` sin tipo, y requisitos publicados con un ID que no es el
  primero de `[fields] id`;
- enlaces cuyo destino no está en ninguno de los ficheros (falta el ReqIF de ese módulo) o
  no tiene ID;
- y los IDs que no casan con `--id-pattern`.

`xml.etree` no resuelve entidades externas ni descarga DTD; un fichero que no es XML o no es
ReqIF es un error con su nombre.

## Pruebas

`tests/test_doors_exchange.py`: el ReqIF de las muestras da las mismas filas que
`make_samples.py` (salvo la numeración, que se calcula de la jerarquía, y `source_key`), un
ReqIF pequeño con XHTML, enumerados, `[kinds]`, IDs repetidos o fuera de patrón, enlaces a
otro fichero y a uno que falta, y `atlas run` sobre la base que deja, con el servidor falso.
