# Cambios

Formato de [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Mientras la versión
sea 0.x, la API de la librería puede cambiar entre versiones menores; cada cambio que
obligue a tocar la configuración o reprocese datos se anota en «Al actualizar».

## [Sin publicar]

### Añadido
- `atlas eval` mide también la recuperación, sin modelo: casos `search` (acierto@1, @3, @5 y
  @10 y rango recíproco medio, con la búsqueda híbrida y con la léxica) y casos con una
  consulta del grafo (`trace`, `trace_code`, `impact`, `find_symbol`, `search_code_docs`).
  `examples/eval/retrieval.yaml` tiene 30 casos sobre las muestras; con `embeddinggemma`, la
  híbrida acierta a la primera en el 75 % y entre las tres primeras en el 90 %, y el grafo,
  10 de 10. Cierra la meta 7 del plan.
- El código se indexa: paso `index.code`, una unidad por unidad de código (en Ada, el
  paquete con su especificación y su cuerpo), y un registro `code_doc` por subprograma,
  prueba, tipo, variable y constante, con su comentario de cabecera, los requisitos que cita
  y su código. `search` y `atlas ask` encuentran código; la herramienta `search` acepta
  `kind = "code_doc"`. En el proyecto grande añade 1,3 s y 48 peticiones de embeddings.
- `Index.search(..., dense=False)`: solo IDs y BM25, sin ninguna petición.

- `atlas link ID|MÓDULO` y el agente `req_linker`: para cada requisito sin código, el código
  que puede implementarlo, con confianza (alta, media, baja) y motivo. Los candidatos salen sin
  modelo (el código que mencionan sus secciones, el de los requisitos enlazados y la búsqueda
  en el código indexado), y el agente solo elige entre ellos. El informe
  (`reports/link-<objetivo>.md`) dice qué etiqueta `Req:` añadir y dónde; no se escribe nada
  en el código. También como herramienta `link_requirement` de `atlas serve`, si se pide.

- `check_code` y `run_test` (`atlas.code.BuildTools`): el compilador y las pruebas del
  proyecto para los agentes. `check_code` compila un fichero sin generar código y devuelve los
  errores y avisos con su sitio; `run_test` construye un procedimiento de prueba y lo ejecuta.
  Los comandos van en `[sources.code] check_command` y `test_command`, como listas de
  argumentos, con `tool_path` y `command_timeout`. `atlas.dev.toml` los configura para GNAT
  instalado con Alire. `run_test` ejecuta código y solo se ofrece si se pide.
- `examples/agents/`: dos agentes de proyecto que escriben, `implementer` y `tester`.

### Cambiado
- `edit_file` tolera también el listado de `read_file` copiado en el texto: quita los números
  de línea, las barras antes de cada salto de línea y los `\n` escritos como texto, en el
  texto buscado y en el nuevo. Con `gemma4:12b`, un cambio de varias líneas pasó de 0 de 2 a
  2 de 2.
- Los PDF se extraen con pymupdf, y markitdown (pdfminer) queda como alternativa
  (`[plugins] disabled = ["extractor:pymupdf"]`). Es unas 30 veces más rápido (1,5 ms por
  página en vez de 45: la primera ejecución del proyecto grande baja de 17,2 a 6,1 s) y el
  texto sigue el orden en que se escribió, así que el número de un título no se separa de
  él. En la muestra sin marcadores, pdfminer perdía 18 de las 27 subsecciones y sus
  requisitos quedaban en la sección anterior.
- Cuando varios extractores leen la misma extensión, se usa el de mayor `priority`
  (atributo nuevo de `DocumentExtractor`).
- La huella de `docs.process` incluye el extractor, y `atlas sections` dice cuál se usa.

### Al actualizar
- El informe `reports/eval-<conjunto>.json` agrupa los resultados en `agents` y `retrieval`.
- `code_doc` ya estaba en `[index] kinds` por defecto: la siguiente ejecución embebe todo el
  código una vez. Para no indexarlo, quítalo de `kinds`. El código compite con los documentos
  en la búsqueda: en las muestras, una búsqueda de documentos bajó de la primera a la cuarta
  posición; `kind` en `search` busca en una sola clase de texto.
- Todos los documentos se vuelven a procesar una vez. El texto de cada sección de un PDF
  cambia (saltos de línea, posición de las cabeceras), así que la caché de análisis no sirve
  y, con `analysis = "llm"`, todas las secciones de los PDF vuelven al modelo. Los Markdown
  salen de la caché.
- pymupdf tiene licencia AGPL-3.0 (o comercial de Artifex). Ya era una dependencia del extra
  `[docs]`, para los marcadores; conviene confirmar que la política de la empresa la admite.

## [0.1.0] — 2026-10-11

Primera versión para el repositorio interno: las fases 0 a 6 del plan (`legacy/PLAN.md`).

### Fase 6: consolidación

**Rendimiento**
- Perfil de tiempo de cada ejecución, guardado en `atlas.db` con la versión. Por paso:
  segundos (y cuántos se van en buscar unidades y en preparar), unidades, las más lentas y
  lo que se pidió a las API: peticiones, rechazos, reintentos, espera del limitador y del
  servidor. `atlas status` lo muestra.
- `[pipeline.step_workers]`: unidades a la vez por paso (`docs`, `code`, `index`).
- `docs.process` procesa varios documentos a la vez, y las secciones de todos comparten
  `workers` peticiones al modelo. Con un modelo simulado de 1 s por petición y 4 hilos,
  un 15 % menos en 6 PDF (50,6 → 43,2 s).
- Lotes de embeddings llenos entre documentos y entre módulos de DOORS, varios a la vez:
  189 peticiones en vez de 207 en el proyecto grande.

**Empaquetado**
- Versión 0.1.0, dependencias con rangos compatibles y ADK fijado a 2.11. `truststore`
  pasa a ser dependencia (`ca_cert = "system"`).
- `constraints.txt` con las versiones exactas probadas en Python 3.10 a 3.13.
- `make wheel`, `wheelhouse`, `wheelhouse-windows` y `check-install` (instalación limpia
  sin red, como dice la guía).
- `atlas init` escribe una plantilla comentada de `atlas.toml`; `atlas --version`.

**Seguridad**
- Los secretos solo vienen del entorno: no carga una configuración con un `api_key` para
  un servidor que no es local, o con una contraseña en la URL del proxy o en el DSN de
  ODBC, escritos tal cual.
- Las huellas de configuración no incluyen secretos.
- `atlas check` enmascara también las credenciales de `base_url`.
- Prueba de red: una ejecución completa, búsquedas, respuestas y un agente ADK solo se
  conectan a la API configurada.

**Documentación**
- [Instalación](docs/instalacion.md), [configuración](docs/configuracion.md) (cada clave,
  comprobado por las pruebas) y [seguridad](docs/seguridad.md).
- Un README por módulo, y el README raíz como índice.
- Ejemplos de uso como librería en `examples/library/`, que las pruebas ejecutan.

**Arreglos**
- La caché de análisis de secciones se escribe de forma atómica; un fichero dañado por un
  corte se vuelve a analizar en vez de dejar la sección en error.
- Ctrl+C con unidades en varios hilos ya no espera a las que no han empezado, y el
  bloqueo del workspace no se suelta mientras alguna sigue escribiendo.

### Fases 0 a 5

- **Fase 0, cimientos.** Configuración validada con `${VAR}` y huellas por sección; motor
  incremental (`Step`, `Ledger`, `Runner`) con reanudación, cuarentena, poda y bloqueo del
  workspace; sistema de plugins; cliente de los modelos.
- **Fase 1, migración.** Los scripts de `legacy/` como módulos: `atlas.docs`
  (`pdf_struct.py`), `atlas.index` (`doc_rag.py`), `atlas.code` y `atlas.graph`
  (`ada_graph.py`), y un solo servidor MCP (`atlas serve`) con las herramientas de los dos
  anteriores y las de ficheros. Mismos resultados, salvo las diferencias buscadas.
- **Fase 2, motor incremental completo.** Todos los pasos en el motor; una segunda
  ejecución sin cambios no hace ninguna petición, y un PDF cambiado en una página solo
  vuelve al modelo con esa sección.
- **Fase 3, DOORS.** La base de intercambio de requisitos y el exportador de DOORS
  (`tools/doors_export`); los requisitos, también en el índice.
- **Fase 4, grafo unificado.** Código, DOORS y documentos en un grafo; `trace`,
  `trace_code`, `impact`, `matrix`, `gaps`, `neighbors` y comunidades, sin modelo; cifras
  que difieren entre DOORS y los documentos.
- **Fase 5, agentes.** Agentes ADK definidos en ficheros Markdown, `AtlasLlm`,
  `build_context`, verificación de requisitos con la documentación, informes de impacto y
  cobertura, `ask_atlas` y evaluación (`atlas eval`: 23 de 24 con `gemma4:12b`).

### Al actualizar

Desde un workspace de una versión anterior (sin número):
- `atlas.db` gana la columna `seconds` del libro de registro y la tabla `runs`; se añaden
  solas al abrirla.
- Si el DSN de `[sources.doors.database]` lleva contraseña, los módulos de DOORS se leen
  una vez más (su huella ya no la incluye). Nada más se reprocesa.
- Una configuración con una clave o una contraseña escrita tal cual deja de cargar: hay
  que pasarla a `${VARIABLE}` (el error dice dónde).

### Pendiente

- Instalar y repetir la prueba de red en un puesto Windows.
- Las metas medibles de la sección 1 del plan sobre el proyecto real.
- Validar en DOORS real la DXL de jerarquía y enlaces, y la descarga de un módulo grande.
- `[sources.code] summaries` y `[sources.doors] attributes` se aceptan pero no tienen
  efecto, y el código no se indexa (`code_doc`).
