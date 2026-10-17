# Seguridad

Revisión de la fase 6 (11 de octubre de 2026): qué sale del puesto, qué se guarda, cómo se
tratan los secretos, qué puede hacer cada pieza y qué queda pendiente. Las afirmaciones que
se pueden comprobar tienen una prueba automática, citada en cada apartado.

## Qué sale del puesto

**Solo peticiones a las URL de `[llm]` y `[embeddings]`**, directas o por el proxy de
`[network]`. No hay telemetría, ni descargas de modelos, ni consultas a otros servicios.

`tests/test_security.py` lo comprueba con los ganchos de auditoría de Python, que ven cada
socket del proceso, en cualquier hilo y lo abra la librería que lo abra. Durante una
ejecución completa (DOORS, código, documentos, grafo e índice), una búsqueda, una respuesta
citada, un `trace` y una pregunta a un agente ADK, todas las conexiones van a la API
configurada.

Qué se envía al modelo:
- el texto de cada sección de los documentos con `analysis = "llm"`, y los resúmenes de
  sección para el resumen del documento;
- los textos que se indexan, para los embeddings: fragmentos de los documentos, resúmenes,
  requisitos de DOORS, definiciones;
- en `atlas ask`, la pregunta y los pasajes encontrados; si la pregunta es amplia, los
  documentos o módulos de los que trata, enteros;
- en los agentes (`atlas agent`, `verify`, `impact`, `coverage`, `eval`, `ask_atlas`), la
  pregunta y lo que devuelven sus herramientas: requisitos, código fuente, documentos.

Por eso el modelo tiene que ser el gateway corporativo o uno local. atlas no lo decide: va
a donde diga `base_url`. La excepción es `atlas.openrouter.toml`, que manda a OpenRouter, un
servicio externo, las muestras sintéticas de `examples/samples` para comparar modelos
([guía](openrouter.md)); no se usa con datos del proyecto.

Otras piezas que podrían conectarse y no lo hacen:
- **ADK**: los agentes usan `AtlasLlm`, que va por el cliente de `atlas.llm`; no se usan
  modelos de Google ni LiteLLM. En la prueba, el agente no abrió más conexiones que las del
  modelo. La telemetría de ADK (OpenTelemetry) solo exporta si se configura con las
  variables `OTEL_*`.
- **`adk web`** abre un servidor web en `127.0.0.1`: solo se ve desde el propio puesto.
- **`atlas serve`** habla por stdio con el editor: no abre ningún puerto.
- **pymupdf** y **markitdown** solo reciben rutas de ficheros locales; el detector de tipos
  de markitdown (magika) lleva el modelo dentro del paquete.

## Qué se guarda

Todo en el workspace del proyecto (`[project] workspace`):

| Dónde | Qué |
|---|---|
| `atlas.db` | requisitos y enlaces, unidades de código, documentos, índice (textos y vectores), grafo, libro de registro y perfiles de las ejecuciones |
| `docs/` | el Markdown de cada documento y la caché de análisis de secciones |
| `graph/` | el grafo exportado (GraphML, JSON) y las matrices |
| `reports/` | verificaciones, propuestas de enlaces, juicios de cumplimiento, informes y evaluaciones |

Es información del proyecto, con la misma confidencialidad que los documentos y el código
de los que sale: la carpeta necesita los mismos permisos. Nada está cifrado por atlas; en un
portátil, el cifrado de disco del sistema (BitLocker, FileVault).

El libro de registro guarda los errores de cada unidad (500 caracteres como mucho). Son
mensajes de las librerías: no llevan claves.

## Secretos

**Solo del entorno.** Un valor `${VARIABLE}` o `${VARIABLE:-valor}` se lee del entorno al
cargar la configuración. atlas se niega a arrancar si el fichero tiene escrito tal cual:
- un `api_key` de `[llm]` o `[embeddings]` para un servidor que no es local (un servidor
  local, como Ollama, no necesita clave);
- una contraseña en la URL del proxy (`http://usuario:contraseña@proxy`);
- una contraseña en el DSN de ODBC (`PWD=` o `Password=`).

El mensaje de error dice dónde está, pero no la repite. Pruebas:
`test_secrets_written_in_the_file_are_refused` y
`test_secrets_from_the_environment_and_keys_of_local_servers_are_fine`.

Además:
- **Huellas sin secretos.** El motor guarda en `atlas.db` una huella de las secciones de la
  configuración de las que depende cada paso, y no incluye en ella claves ni contraseñas.
  Así no queda en disco ningún hash de un secreto (que se podría atacar sin conexión) y
  cambiar una contraseña no reprocesa nada (`test_fingerprints_ignore_secrets`).
- **Enmascarados.** `atlas check` y los mensajes de conexión muestran las claves como
  `abc***` y quitan el usuario y la contraseña de las URL
  (`test_check_masks_the_credentials_of_urls`).
- La contraseña de la clave privada del certificado de cliente (TLS mutuo) va en la
  variable `RAG_CLIENT_KEY_PASSWORD`.
- El editor pasa su entorno a `atlas serve`. Si no lo tiene, OpenCode tiene `environment` y
  VS Code tiene `env` o `envFile`, que también leen del entorno o de un fichero aparte.

## TLS y proxy

- La verificación de certificados está siempre activa. `[network] insecure = true` la
  desactiva solo para diagnosticar, y `atlas check` lo avisa.
- `ca_cert = "system"` usa el almacén de certificados del sistema (truststore), donde está
  la raíz corporativa; también se puede dar un PEM.
- Con Python 3.13 y sin `ca_cert = "system"`, atlas quita del contexto TLS la bandera
  `VERIFY_X509_STRICT`, que 3.13 enciende por defecto. Esa bandera rechaza certificados a los
  que les falta una extensión (el Authority Key Identifier, por ejemplo), como los que firman
  algunos proxies que inspeccionan TLS, aunque su cadena sea de confianza. Sin ella se
  comprueba lo mismo que en 3.12: que la cadena llega a una raíz de confianza, las fechas y el
  nombre del servidor. Con `"system"` decide el sistema, que no la aplica. Si aun así sale
  «Missing Authority Key Identifier», `atlas check` recomienda `ca_cert = "system"` o
  Python 3.12.
- El proxy es el de `[network] proxy`. Si no se dice, el de `HTTPS_PROXY` o `HTTP_PROXY`,
  como pip o curl; `no_proxy` (o `NO_PROXY`) dice qué servidores van directos. Del mismo
  modo, sin `ca_cert` se añade el PEM de `SSL_CERT_FILE` si existe.

## Qué puede hacer cada pieza

| Pieza | Puede | Límites |
|---|---|---|
| Herramientas de ficheros (`list_files`, `grep`, `read_file`, `outline`, `read_subprogram`, `edit_file`, `write_file`, `replace_subprogram`, `add_subprogram`) | leer y, si se piden, escribir código | solo dentro de `[sources.code] roots`, sin `..` ni enlaces simbólicos que salgan, y nada de lo excluido. Las que escriben no se ofrecen si no se piden en `[mcp] tools` o en las herramientas de un agente, y el servidor MCP las marca como destructivas para que el editor pida confirmación. Antes de escribir, la comprobación de sintaxis (`syntax_commands`) se lanza sobre una copia en `<workspace>/build/syntax` |
| Biblioteca y manuales (`library_spec`, `search_reference`) | leer las especificaciones y los manuales de `[sources.code] library` y `reference`, fuera de las raíces | solo lectura y solo de lo que nombra el TOML; el índice de los manuales va a `<workspace>/reference.db` |
| Servidor de lenguaje (`find_definition`, `find_references`, `describe_symbol`) | lanzar el programa de `[sources.code] language_server`, que lee el código del proyecto | el programa viene del TOML y tiene su confianza; habla por su entrada y salida estándar, sin red; atlas le escribe un proyecto GPR en `<workspace>/build`. Solo pregunta por ficheros de las raíces |
| Compilador (`check_code`, `run_test`) | lanzar los comandos de `[sources.code]` sobre un fichero del código; `run_test` ejecuta además la prueba compilada | los comandos vienen del TOML, como listas de argumentos y sin intérprete de órdenes, y tienen la confianza del propio `atlas.toml`; solo con ficheros dentro de las raíces del código; lo que producen va a `<workspace>/build`; tiempo máximo `command_timeout`. `run_test` solo se ofrece si se pide, y el servidor MCP la marca como destructiva |
| `atlas develop` con `attempts` | copiar la carpeta de trabajo, la de `atlas.toml` (sin `.git` ni el workspace), en `<workspace>/attempts/<n>` y ejecutar en cada copia los comandos de `[sources.code]` | las copias se borran al acabar cada encargo; las raíces del código tienen que estar dentro de la carpeta de trabajo |
| Agentes de serie | consultar documentos, requisitos, grafo y código | ninguno tiene herramientas de escritura ni de ejecución; `max_steps` limita sus llamadas al modelo. Los de `examples/agents/` sí escriben y compilan: son ejemplos para copiar |
| Lectura de requisitos | leer la base de intercambio o la base de DOORS | solo lectura; las consultas del adaptador `database` vienen del TOML y el módulo va como parámetro (`:module`), nunca pegado al SQL |
| Plugins (`[plugins] modules` y entry points `atlas.plugins`) | ejecutar código Python al cargarse | tienen la confianza del propio `atlas.toml`: quien puede editarlo puede ejecutar código con el usuario que lanza atlas |
| Agentes del proyecto (`agents/*.md`) y conjuntos de evaluación | definir instrucciones y herramientas | YAML leído con `safe_load`; solo pueden usar herramientas registradas |
| `tools/doors_export` | leer DOORS por COM y DXL en el PC con DOORS | solo lectura de DOORS; escribe la base de intercambio |

En el código no hay `eval`, `exec`, `pickle` ni procesos lanzados con shell.

## Dependencias

- Todas vienen de PyPI. `constraints.txt` fija la versión exacta de cada una, y el
  wheelhouse permite instalar sin red (ver [instalación](instalacion.md)).
- `pip-audit` sobre los conjuntos fijados de Python 3.10, 3.11, 3.12 y 3.13: sin
  vulnerabilidades conocidas el 11 de octubre de 2026. Conviene repetirlo con cada versión:
  `pip-audit -r constraints.txt`.
- `tree-sitter-ada` 0.1.0 declara `build` y `twine` como dependencias, aunque no los usa.

## Riesgos que quedan

- **Lo que retiene el gateway.** atlas le envía documentos y código; qué guarda y cuánto
  tiempo depende de la política del gateway, no de atlas.
- **Instrucciones dentro de los datos.** Un documento, un requisito o un comentario de
  código puede contener texto dirigido al modelo. Los agentes de serie no pueden escribir
  nada y su salida es para una persona; las herramientas que escriben hay que pedirlas
  expresamente y el editor pide confirmación.
- **`atlas.db` sin cifrar**, protegido solo por los permisos de la carpeta.
- **Windows.** La revisión se ha hecho en macOS; falta repetir la prueba de red y la
  instalación en un puesto Windows.
