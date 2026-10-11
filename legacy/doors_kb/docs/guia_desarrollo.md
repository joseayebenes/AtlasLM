# Guia de desarrollo

## Montar el entorno

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,embeddings]"   # Linux/macOS: nucleo, herramientas y numpy
```

En la maquina con DOORS, ademas del nucleo hace falta el extra de Windows:

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev,win,embeddings]"
```

## Ejecutar las comprobaciones

```bash
.venv/bin/pytest -q                       # toda la suite: no necesita DOORS ni Windows
.venv/bin/ruff check src tests examples   # estilo e imports
.venv/bin/python examples/demo_sync_fake.py        # recorrido narrado de la sincronizacion
.venv/bin/python examples/demo_busqueda_hibrida.py # recorrido narrado de la busqueda
```

Que la suite completa corra sin DOORS no es casualidad: es el requisito RNF-013 y la razon
de que exista `FakeDoorsSource`. Si una prueba nueva necesitara DOORS para pasar, casi
siempre significa que la logica que quiere probar esta en la capa equivocada.

## Estructura

```
src/doors_kb/
  config.py          Configuracion por entorno, validada al arrancar
  models.py          Modelo de dominio y hash canonico
  errors.py          Errores con mensajes accionables
  sources/
    base.py          Protocolo RequirementsSource  <- la frontera del sistema
    fake.py          Fuente en memoria para pruebas y demos
    doors/
      dxl.py         Generacion de DXL (codigo puro, testeable en cualquier sitio)
      com_worker.py  Hilo COM unico, timeouts, reintentos
      client.py      RequirementsSource real sobre la sesion Automation
  db/
    schema.sql       Esquema de la copia local, indice FTS y embeddings
    repository.py    Persistencia, hashes, borrado logico, historial, indice
  sync/service.py    Algoritmo de sincronizacion segura
  embeddings/
    text.py          Que texto representa a un requisito
    provider.py      Llamada a la API de embeddings (y proveedor falso)
    service.py       Que hay que reembeder y cuando
  search/
    lexical.py       FTS5 con bm25 y fragmentos citables
    vector.py        Similitud coseno sobre los embeddings
    hybrid.py        Fusion RRF de los dos rankings
  servers/
    response.py      Limite y recorte de las respuestas MCP
    doors_server.py  Servidor MCP directo (H1)
    kb_server.py     Servidor MCP sobre la copia local (H7)
  cli/
    sync_doors.py    Comando doors-sync
    embed.py         Comando doors-embed
    search.py        Comando doors-search
```

La regla estructural: **`db/` y `sync/` no conocen COM ni DXL**. Hablan del protocolo
`RequirementsSource`. Romper esa regla arrastraria Windows a toda la base de codigo.

## Convenciones

- **Idioma.** Documentacion, docstrings y comentarios en espanol; identificadores de codigo
  y nombres de tabla en ingles, como en la especificacion (`requirements`, `content_hash`).
- **Sin acentos en codigo, docs ni mensajes.** El CLI y los logs se leen en consolas de
  Windows con codificaciones heredadas donde los acentos salen mal; mantener la convencion
  en todo el proyecto evita mezclar dos estilos. La especificacion original (`especificacion.md`)
  se conserva tal cual, con sus acentos.
- **Docstring de modulo en todos los archivos**, indicando responsabilidad y requisitos que
  cubre. Un lector debe saber para que existe un archivo sin leer su cuerpo.
- **Los comentarios explican el porque**, no el que. Si algo no es obvio (por que se reactiva
  un registro con el mismo hash, por que el cursor no es un offset), el motivo va escrito.
- **Type hints completos** y `dataclasses` para el modelo. Sin metaprogramacion.
- **Errores accionables**: que fallo, sobre que modulo o atributo y que hacer. Nunca
  `except Exception: pass`.
- **Tests como documentacion**: nombre descriptivo y docstring que cita el requisito o
  criterio de aceptacion que verifican.

## Como anadir cosas

### Una herramienta MCP nueva

1. Anade el metodo al protocolo `RequirementsSource` (`sources/base.py`).
2. Implementalo en `FakeDoorsSource` **primero**: es lo que permite probarlo.
3. Genera el DXL en `sources/doors/dxl.py` y consumelo en `client.py`.
4. Registra la tool en `servers/doors_server.py` con `annotations=SOLO_LECTURA` y haz que
   devuelva a traves de `responder(...)`, para que respete el limite de tamano.
5. Anade la fila correspondiente a [`trazabilidad.md`](trazabilidad.md).

No anadas nunca una tool que ejecute DXL recibido del agente: es la unica linea roja del
diseno (RF-040, ADR-003).

### Un atributo nuevo en la sincronizacion

Basta con incluirlo en `DOORS_SYNC_ATTRIBUTES`; no hay que migrar el esquema (RF-053). Pero
tenlo en cuenta: **el perfil de atributos define el hash de contenido**. Anadir o quitar uno
cambia el hash de todos los requisitos del modulo, que pasaran a contar como `updated` en la
siguiente pasada (riesgo R-005). Conviene fijar el perfil antes de generar embeddings.

### Un atributo nuevo en el texto de embedding

Se anade a `EMBEDDINGS_ATTRIBUTES`, pero **tiene que estar antes en `DOORS_SYNC_ATTRIBUTES`**:
el texto de embedding solo puede usar atributos que la copia local contenga. Si no lo esta,
el servicio avisa por log en lugar de aplicarlo en silencio. Cambiar este perfil regenera
todos los embeddings del modulo (ADR-013), lo que con un proveedor de pago tiene coste.

### El siguiente hito (H8, Graph-RAG)

La tabla `links` ya existe en el esquema y el cliente de DOORS sabe leer trazabilidad
(`get_links`). Falta sincronizarla y anadir una expansion que, partiendo de los resultados de
`buscar_hibrida`, siga los enlaces para aportar contexto relacionado (RF-079). El sitio
natural es un modulo nuevo en `search/`, sin tocar los tres modos actuales.

## Que se prueba y que no

| Se prueba aqui | Requiere DOORS real |
|---|---|
| Clasificacion de cambios, borrado logico y reactivacion | Que el DXL generado se ejecute sin errores del interprete |
| Reglas de sincronizacion segura y cursor | Rendimiento por pagina en un modulo grande (CA-001, CA-006) |
| Generacion y escapado de DXL | Comportamiento de la sesion Automation y el login |
| Timeouts, envenenamiento y reintentos del worker | Trazabilidad entrante con permisos reales |
| Catalogo MCP, anotaciones y limites de respuesta | La llamada real a la API de embeddings |
| Indice FTS, generacion incremental y fusion de rankings | La calidad semantica del modelo elegido |

Lo de la derecha se verifica siguiendo [`operacion_windows.md`](operacion_windows.md).
