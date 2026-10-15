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

Viene del script de dos ficheros del proyecto doors_kb (`legacy/doors_kb/`: `scripts/descargar_requisitos.py`
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
| `[kinds]` | si hay un atributo que distingue requisitos de títulos e información (ver [Qué es requisito](#qué-es-requisito-kinds)) |

`publish.py` comprueba que los atributos de la correspondencia existen; si no, sugiere el
nombre más parecido. Al publicar:
- los objetos sin ID se omiten y se cuentan;
- el padre de cada requisito es su antecesor más cercano que tenga ID;
- un enlace a un objeto que no se ha descargado se omite y se cuenta.

## Qué es requisito (`[kinds]`)

Cada objeto publicado es `requirement`, `heading` o `information`. atlas indexa los
requisitos, convierte la información en fragmentos de texto y usa los títulos como capítulo
de los requisitos que cuelgan de ellos (`1.1 Track management`). Sin `[kinds]`, lo decide el
contenido: un objeto con texto es requisito; uno con título y sin texto, título; el resto,
información.

Si un atributo de DOORS dice qué es cada objeto, se pone en `[kinds] attribute`, y sus valores
en `[kinds.values]`. Los valores no distinguen mayúsculas ni espacios de más. `others` dice qué
son los objetos con otro valor o sin valor: `requirement` (por defecto), `heading`,
`information` o `heading_or_information`, que deja al contenido si son título o información.
Es lo que hace falta cuando el atributo solo dice si el objeto es requisito:

```toml
[kinds]
attribute = "is_requisite"
others = "heading_or_information"   # lo que no es "Yes": título si tiene título y no texto; información si no
[kinds.values]
requirement = "Yes"
```

Con `information = "No"` en lugar de `others`, un título que diga «No» sería información y sus
requisitos se quedarían sin capítulo. Un módulo que no tiene el atributo se clasifica por
contenido, y `publish.py` lo avisa. El resumen de cada módulo cuenta los objetos de cada tipo
(`4 objects (requirement 2, heading 1, information 1)`), y `publish.py` dice qué valores del
atributo no estaban en `[kinds.values]`: sin `others` lo dice como aviso, porque esos objetos
pasan a ser requisitos.

## El módulo de enlaces de cada enlace

`publish.py` saca el tipo de cada enlace (`satisfies`, `verifies`...) del nombre de su módulo
de enlaces. La primera versión lo pedía con `module(l)`, que contra un DOORS real no compila
(`-E-DXL: <line:53> incorrect arguments for (=)`, 2026-10-15). Ahora no se pregunta: antes del
primer módulo de un proyecto se listan sus módulos de enlaces (los elementos de tipo «Link»,
como se listan los formales), y cada objeto recorre sus enlaces por `o -> "*"` y, aparte, por
cada módulo de enlaces del proyecto (`o -> "/Proyecto/Enlaces/Satisfies"`). Un enlace que sale
en un módulo de enlaces conocido se guarda con él; uno que solo sale por `"*"` (un módulo de
enlaces de otro proyecto) se guarda sin él, y su tipo sale de `[links] default_type`. Con
muchos módulos de enlaces, `--no-link-module` se ahorra esos recorridos y guarda todos los
enlaces sin tipo.

## Lo que está sin validar contra DOORS real

La DXL de atributos, recorrido por cursor y módulos ya estaba comprobada en doors_kb. En un
DOORS real (2026-10-15), `--check` dio bien la jerarquía (`parent`, `level`) y los enlaces
(`target`, `targetAbsNo`); falló `module(l)`, que ya no se usa. Lo que lo sustituye, la lista
de módulos de enlaces del proyecto y `o -> "<módulo de enlaces>"`, **está sin probar contra
DOORS**: las pruebas automáticas usan un DOORS simulado que responde con el mismo protocolo.

La descarga falló después en el mismo DOORS con `incorrect concatenated token`: el número
absoluto se convertía con un molde al estilo de C, `(int)(o."Absolute Number")`, que venía de
doors_kb y que DXL no tiene (toma `(int)` y lo que sigue como una concatenación). Ahora se
asigna a un entero, `int a = o."Absolute Number"`.

`--check` prueba cada primitiva por separado sobre un módulo real y dice cuál falla: también
las dos de los módulos de enlaces. Y al final ejecuta **la DXL de la descarga misma** con un
objeto: los atributos, la primera página y una página desde un cursor. Si todo sale OK, la
descarga compila en ese DOORS. Si fallan las de los módulos de enlaces, `--no-link-module`
descarga los enlaces sin su tipo (`[links] default_type`). Con `--no-links` no se leen los
enlaces.
