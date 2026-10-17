# Aceptación en el proyecto real

El guion de las dos primeras semanas con atlas en el proyecto: un puesto Windows, el
gateway corporativo por el proxy, DOORS, los PDF y el código Ada de verdad. Es lo que el
plan deja sin marcar en la fase 6 ([legacy/PLAN.md](../legacy/PLAN.md)): la instalación
limpia en un puesto Windows y las metas medibles sobre el proyecto real.

Cada paso tiene una casilla y **una cifra** que apuntar. Lo que falle y tenga remedio va a
la [FAQ](faq.md); lo que no, a una incidencia con el comando, la salida y `atlas status`.
Todo lo que se usa aquí existe en la 0.2.0.

Antes de empezar:
- la entrega: el wheelhouse `wheelhouse-win_amd64-py312` con `[dev]` (ver el paso 1) y
  una copia del repositorio (`tests/`, `examples/` y `tools/doors_export/`);
- la URL del gateway, el nombre del modelo, la clave y el proxy (con su usuario si lo pide);
- dos ingenieros del proyecto que escriban preguntas (semana 2) y dos que usen el editor;
- un módulo de DOORS pequeño (decenas de requisitos) que conozcan bien.

## Semana 1: instalación, DOORS y todo lo que no gasta cupo

### 1. Puesto Windows limpio (día 1)

Desde un equipo con red y el repositorio, la entrega con las dependencias de las pruebas:

```bash
make wheelhouse-windows EXTRAS=all,dev     # dist/wheelhouse-win_amd64-py312/, con pytest
```

En el puesto, siguiendo solo la [guía de instalación](instalacion.md):

- [ ] Instalar sin red. Cifra: **minutos** desde copiar el wheelhouse hasta `atlas --version`.
  ```powershell
  py -3.12 -m venv C:\atlas\venv
  C:\atlas\venv\Scripts\python -m pip install --no-index --find-links C:\atlas\wheelhouse "atlaslm[all,dev]"
  C:\atlas\venv\Scripts\atlas --version
  ```
- [ ] `atlas init` en la carpeta del proyecto, con `[llm] base_url` y `model` del gateway,
  `[network] proxy = "${HTTPS_PROXY:-}"`, `ca_cert = "system"` y
  `requests_per_minute = 28`, y la clave con `setx ATLAS_API_KEY "..."` (otra terminal
  después). Cifra: **claves cambiadas** de la plantilla.
- [ ] `atlas check atlas.toml`: una petición al chat y otra a los embeddings por el proxy.
  Cifra: **segundos** de `check` y lo que dice del modelo (`reasoning model` o no).
- [ ] `atlas probe atlas.toml`: el modelo como agente. Cifra: **segundos** hasta la llamada
  a la herramienta, y si el servidor aprovecha el razonamiento devuelto.
- [ ] Las pruebas de seguridad contra lo instalado: copiar `tests\` (con `tests\fakes\`) y
  `examples\make_samples.py` a una carpeta, con esa misma forma, y desde ella:
  ```powershell
  C:\atlas\venv\Scripts\python -m pytest -q tests\test_security.py
  ```
  Genera las muestras, arranca el servidor falso y comprueba que una ejecución, una
  búsqueda, una respuesta, un `trace` y un agente solo se conectan a la API configurada.
  Cifra: **pruebas que pasan** de las que hay.

### 2. DOORS: la primera matriz real (día 2)

En el PC con DOORS, con el usuario de Windows de DOORS ([tools/doors_export](../tools/doors_export/README.md)):

- [ ] `python tools\doors_export\doors_download.py --check --module "/Proyecto/.../SRS"`
  sobre el módulo pequeño. Cifra: **primitivas OK** de las que prueba.
- [ ] La descarga (`--module` o `--folder`) y `raw_store.py doors_raw.sqlite` para el
  resumen. Cifras: **módulos, objetos y minutos** de la descarga.
- [ ] `publish.py --raw doors_raw.sqlite --mapping mapping.toml --out requirements.sqlite`,
  ajustando `mapping.toml` hasta que no queden avisos que importen (IDs repetidos,
  atributos que no existen, módulos sin descargar). Cifra: **requisitos publicados** y
  **vueltas** a `mapping.toml`.
- [ ] En `atlas.toml`, `[sources.doors] enabled = true` con la base publicada y, de
  momento, `[pipeline] steps = ["doors"]`; `atlas run atlas.toml`. Cifra: **segundos**.
- [ ] La primera matriz: `steps = ["doors", "graph"]`, `atlas run` y
  `atlas matrix MÓDULO atlas.toml`. Sin código todavía, todo sale «without code»: lo que se
  comprueba es la jerarquía, los enlaces entre módulos (`derived`) y los atributos. Cifra:
  **requisitos con `derived`** que un ingeniero da por buenos, de 20 mirados en DOORS.

### 3. Documentos y código sin modelo (días 3 a 5)

- [ ] `atlas sections atlas.toml` sobre diez PDF reales, de tipos distintos. Para cada uno,
  si las secciones son las del índice del documento; si no, ajustar `structure` y
  `heading_regex` de su `[[sources.docs]]` (la salida dice qué líneas parecían títulos y por
  qué se rechazaron). Cifra: **PDF bien a la primera**, de 10, y **bien tras ajustar**.
- [ ] `analysis = "regex"` en las fuentes de documentos (con `req_regex` si los requisitos
  se citan de otra forma), `[sources.code] roots` con el código Ada y
  `steps = ["doors", "code", "docs", "graph"]`, sin `index`: así no se pide nada al modelo.
  `atlas run atlas.toml`, y después `atlas status`: en el perfil, ningún paso da peticiones.
  Cifras: **minutos** de la primera ejecución y **peticiones** (tienen que ser 0).
- [ ] `atlas graph atlas.toml`: ficheros, subprogramas, llamadas, `ambiguous calls` y
  llamadas sin resolver (las librerías externas salen aquí), y los huecos. Una raíz mal
  escrita no avisa: da 0 ficheros. Cifra: **ficheros Ada** leídos frente a los que tiene el
  proyecto.
- [ ] Los huecos: `atlas graph` los cuenta y `atlas matrix MÓDULO atlas.toml` lista los
  requisitos sin código y sin pruebas. Las listas de los demás (IDs citados en el código o
  en los documentos que no están en DOORS, valores que difieren entre DOORS y un
  documento) las da la herramienta `gaps`, también desde Python:
  `GraphTools.from_config(load_config("atlas.toml")).gaps()`. Un ID «not in DOORS» suele ser
  otra forma de escribirlo (un guion, un cero, un prefijo), que se arregla en
  `[sources.doors.convention]`. Cifras: **requisitos con código**, y cuántos de los «not in
  DOORS» son otra forma del ID.
- [ ] `atlas trace ID atlas.toml` de diez requisitos que los ingenieros conozcan. Cifra:
  **trazas completas** (texto, documentos, código y pruebas que el ingeniero esperaba), de
  10.
- [ ] Una segunda ejecución sin cambios: `atlas run atlas.toml` y `atlas status`. Cifras:
  **segundos** y **peticiones** (0). Es la meta 1.

## Semana 2: el modelo, las preguntas y el editor

### 4. La ingesta con el modelo, de noche

- [ ] `analysis = "llm"` en una fuente de documentos (las demás, `enabled = false`) y
  `"index"` en los pasos. Lanzarlo al irse y dejar la ventana abierta; si se corta (red,
  cupo, apagado), `atlas run` sigue donde se quedó. A la mañana, `atlas status`: unidades
  hechas y en error, y por paso las peticiones de chat y de embeddings, los rechazos del
  cupo (409 o 429) y los segundos de espera. Cifras: **horas**, **peticiones por sección**
  y **rechazos**.
- [ ] Repetir con las demás fuentes, una por noche, con lo aprendido del cupo
  (`requests_per_minute`, `[pipeline] workers`). Cifra: **secciones por hora**.
- [ ] Un corte a propósito (Ctrl+C a mitad) y relanzar: nada hecho se repite. Es la meta 3.
  Cifra: **unidades repetidas** (0).
- [ ] Cambiar una sola cosa (un requisito en DOORS, descargado y publicado; un `.adb`; una
  página de un PDF) y relanzar. Es la meta 2. Cifras: **unidades procesadas** y
  **peticiones**.

### 5. Veinte preguntas del proyecto

- [ ] Dos ingenieros escriben 20 preguntas con su respuesta conocida, en el formato de
  `examples/eval/` (ver [atlas.agents](../atlas/agents/README.md)): búsquedas (`search` con
  los textos que deben salir), consultas del grafo (`trace`, `impact`) y preguntas al
  asistente (`question`), mezcladas, en español y en inglés, con IDs exactos y con
  paráfrasis. En `[agents.eval] sets`.
- [ ] `atlas eval atlas.toml`. Cifras: **acierto@3 y MRR** de la búsqueda híbrida y de la
  léxica, **consultas del grafo bien** y **respuestas del asistente bien**, con su mediana
  de segundos. Es la meta 7.

### 6. El servidor en el editor, una semana

- [ ] `atlas serve` en el editor de dos personas (OpenCode o VS Code), configurado como
  dice la [guía de instalación](instalacion.md#5-conectar-el-editor), con el proxy y la
  clave en el entorno del editor ([FAQ](faq.md#el-servidor-mcp-no-llega-al-modelo)).
  Cifra: **minutos** hasta la primera respuesta útil en el editor.
- [ ] Durante la semana, cada uno apunta las preguntas que hizo, si la respuesta sirvió y
  cuánto tardó. La [guía de uso](uso.md) dice qué herramienta conviene para cada tarea.
  Cifras: **preguntas**, **útiles** y **segundos** típicos.

### 7. Las metas, en el plan

- [ ] Rellenar en [legacy/PLAN.md](../legacy/PLAN.md), en la tabla de las metas medibles,
  una columna con el proyecto real:

| Meta | Cifra de este guion |
|---|---|
| 1. Procesar una vez | segundos y peticiones de la segunda ejecución sin cambios (paso 3) |
| 2. Solo los cambios | unidades y peticiones tras un cambio (paso 4) |
| 3. Recuperable | unidades repetidas tras el corte (paso 4) |
| 4. Configurable por fichero | claves cambiadas, y si hizo falta algo fuera de `atlas.toml` (pasos 1 a 4) |
| 5. Importable | `GraphTools.from_config(load_config("atlas.toml")).trace("ID")` en un script, con el workspace real |
| 6. Trazabilidad | trazas completas de 10 (paso 3) |
| 7. Evaluable | lo que da `atlas eval` con las 20 preguntas (paso 5) |

Y marcar las dos casillas de aceptación de la fase 6, o anotar qué falta.
