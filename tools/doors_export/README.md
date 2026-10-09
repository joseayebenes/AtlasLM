# Exportador de DOORS → base de intercambio de atlas

atlas lee siempre los requisitos de la **base de intercambio** (`atlas/doors/exchange.py`),
que tiene un esquema fijo. Este exportador la rellena a partir de IBM DOORS Classic en dos
pasos:

```
DOORS ──(1) doors_download.py──► doors_raw.sqlite ──(2) publish.py + mapping.toml──► requirements.sqlite ──► atlas
        en el PC con DOORS             copia en bruto       en cualquier equipo             base de intercambio
```

1. **Descarga** (`doors_download.py` + `raw_store.py`). Guarda todo tal como viene de
   DOORS: todos los atributos de objeto con su nombre de DOORS, la jerarquía (padre y
   nivel de cada objeto), los enlaces salientes (módulo y objeto destino, y módulo de
   enlaces) y la lista de atributos de cada módulo. Necesita Windows, el cliente de DOORS
   y `pywin32`, y no depende de atlas: los dos ficheros se pueden copiar sueltos.
2. **Publicación** (`publish.py`). Aplica `mapping.toml` y escribe la base de intercambio.
   No necesita DOORS: si cambias la correspondencia, solo vuelves a publicar.

Viene del script de dos ficheros del proyecto doors_kb (`scripts/descargar_requisitos.py`
y `scripts/modelo_sqlite.py`). Está traducido al inglés y además lee la jerarquía y los
enlaces, guarda la definición de los atributos y tiene versión de esquema. Una base de
aquel script se actualiza sola al abrirla, y en módulos muy grandes ya no falla el
marcado de borrados.

## Uso

En el PC con DOORS, con el mismo usuario de Windows que DOORS:

```powershell
pip install pywin32

# 1. Antes de la primera descarga: ¿funcionan en tu DOORS las primitivas de jerarquía y enlaces?
python tools\doors_export\doors_download.py --check --module "/Proyecto/Requisitos/SRS"

# 2. Descarga: un módulo, o todos los de una carpeta o proyecto
python tools\doors_export\doors_download.py --folder "/Proyecto" --db doors_raw.sqlite
python tools\doors_export\raw_store.py doors_raw.sqlite          # resumen de lo descargado
```

Python abre una ventana nueva de DOORS: inicia sesión **en esa ventana**. Las descargas
siguientes son incrementales: solo se reescriben los objetos cuyo contenido, padre o
enlaces han cambiado. Un objeto que ya no está en DOORS solo se marca como borrado si se
ha recorrido el módulo entero.

Después, en cualquier equipo con el repositorio de AtlasLM:

```bash
cp tools/doors_export/mapping.example.toml mapping.toml     # y ajústalo a tu proyecto
python tools/doors_export/publish.py --raw doors_raw.sqlite --mapping mapping.toml --out requirements.sqlite
```

Y en `atlas.toml`:

```toml
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "requirements.sqlite"
```

## La correspondencia (`mapping.toml`)

Sigue una regla única: a la izquierda va lo que ve atlas y a la derecha lo que hay en DOORS,
con su nombre exacto. Ver `mapping.example.toml`.

| Sección | Qué dice |
|---|---|
| `[fields]` | de dónde salen el ID (`identifier`, un atributo como `"Requirement ID"`, o `absolute_number` con un solo módulo), el texto y el título |
| `[attributes]` | qué atributos de DOORS pasan a atlas y con qué nombre |
| `[modules]` | cómo se llama cada módulo en atlas (por defecto, como en DOORS) |
| `[link_types]` | qué tipo de enlace es cada módulo de enlaces (`satisfies`, `verifies`...) |
| `[kinds]` | si hay un atributo que distingue requisitos de títulos e información |

`publish.py` comprueba que los atributos de la correspondencia existen; si no, sugiere el
nombre más parecido. Al publicar:
- los objetos sin ID se omiten y se cuentan;
- el padre de cada requisito es su antecesor más cercano que tenga ID;
- un enlace a un objeto que no se ha descargado se omite y se cuenta.

## Lo que está sin validar contra DOORS real

La DXL de atributos, recorrido por cursor y módulos ya estaba comprobada en doors_kb. La
nueva de jerarquía (`parent`, `level`) y de enlaces (`target`, `targetAbsNo`, `module(l)`)
**no se ha podido probar sin DOORS**. Las pruebas automáticas usan un DOORS simulado que
responde con el mismo protocolo.

`--check` prueba cada primitiva por separado sobre un módulo real y dice cuál falla. Si
falla solo la del módulo de enlaces, `--no-link-module` descarga los enlaces sin él; en ese
caso los tipos salen de `[links] default_type`. Con `--no-links` no se leen los enlaces.
