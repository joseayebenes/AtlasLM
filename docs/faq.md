# Preguntas frecuentes: errores conocidos y su remedio

Lo que ya ha fallado alguna vez, con el mensaje que da y qué hacer. Si un error no está
aquí, `atlas check atlas.toml` y `atlas status atlas.toml` suelen decir por dónde va; lo
que se descubra en la [aceptación](aceptacion.md) se añade a esta página.

## Conexión con el modelo

### `server certificate not verified`

El proxy corporativo inspecciona TLS y su certificado raíz no está entre los de Python.
`[network] ca_cert = "system"` usa el almacén de Windows (o de macOS), donde ya está.
Otra opción es la ruta de un PEM con la raíz: se añade a los certificados habituales, no
los sustituye.

### `Missing Authority Key Identifier` con Python 3.13

Python 3.13 verifica los certificados de forma más estricta y rechaza algunos de los que
emiten los proxies que inspeccionan TLS, cuando la raíz llega como PEM en `ca_cert`. Con
`ca_cert = "system"` no pasa. Si no, Python 3.12, que es el recomendado.

### `the proxy requires authentication` (407)

El proxy pide usuario y contraseña. Van en la URL del proxy, pero leídos del entorno:
`proxy = "http://${PROXY_USER}:${PROXY_PASS}@proxy:8080"`. atlas rechaza una contraseña
escrita tal cual en `atlas.toml`.

### `cannot connect`

Si el gateway es interno, puede que no haya que pasar por el proxy: su host va en
`no_proxy`. Si es externo, puede que haga falta el proxy y no esté en el entorno
(`HTTPS_PROXY`). `atlas check` dice, para cada URL, por qué proxy va (o `direct`) y con
qué certificados.

### `credentials rejected by the API` (401)

La clave no llega o no vale. La plantilla la lee de `ATLAS_API_KEY`: tras `setx` hay que
abrir otra terminal (y reiniciar el editor). `atlas check` avisa de las variables de
`atlas.toml` que no están puestas.

### `path or model not found` (404)

`base_url` suele acabar en `/v1`, y el nombre del modelo tiene que ser exactamente el del
servidor.

### Rechazos del cupo (409 o 429)

El gateway admite unas 28 peticiones por minuto y rechaza con 409 o 429 cuando se pasan.
atlas espera lo que diga el servidor y repite, sin contarlo como fallo; `atlas status`
cuenta los rechazos y los segundos de espera de cada paso. Si son muchos,
`[network] requests_per_minute = 28` reparte las peticiones antes de que el gateway las
rechace. Cada proceso lleva su propia cuenta: un `atlas run` y el servidor del editor a la
vez no se coordinan y suman sus peticiones.

## El modelo

### El razonamiento de gemma 4

gemma 4 razona antes de contestar, y ese razonamiento cuenta dentro de `max_tokens` y del
tiempo. Se nota así:
- `atlas check` dice `OK: 'ok' (reasoning model: …)`, o `FAILED` porque el modelo gastó
  todo el presupuesto razonando sin escribir la respuesta;
- el análisis de documentos tarda minutos por sección (con `gemma4:12b` en Ollama, de 1 a
  8 minutos; sin razonamiento, unos 15 s);
- los agentes contestan vacío o se inventan ficheros (con razonamiento, 7 de 10 bien; sin
  él, 5 de 5).

El remedio es `[llm] reasoning_effort = "none"`, que ya ponen la plantilla de `atlas init`
y los agentes de serie (`reasoning_effort: none` en su cabecera). Los agentes que escriben
código van mejor con algo de razonamiento (`low`); `atlas probe` prueba el modelo así y dice
si el servidor aprovecha el razonamiento que atlas le devuelve.

### `the server does not accept json_schema, going on without it` (501)

Algunos servidores no aceptan `response_format` con esquema JSON: Ollama con gemma 4
responde 501 «structured output is unavailable», otros 400 o 422. No es un error: atlas
avisa una vez y sigue sin esquema, sacando el JSON del texto de la respuesta. Lo que se
pierde es que el servidor obligue a la forma; de vez en cuando un campo llega con otro tipo
(un objeto donde se esperaba texto) y se corrige pidiéndoselo otra vez al modelo.

### Ollama recorta las secciones largas: `OLLAMA_CONTEXT_LENGTH`

Ollama arranca con un contexto pequeño y corta lo que no cabe **sin avisar**: una sección
larga se analiza a medias, y un agente olvida el principio de la conversación. Hay que
arrancarlo con más contexto:

```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
```

En Windows, `setx OLLAMA_CONTEXT_LENGTH 16384` y reiniciar Ollama (también el icono de la
bandeja). No afecta al gateway, que tiene su propio contexto.

## Rutas y fuentes

### Las rutas relativas

En la 0.2.0, las rutas de las fuentes (`[[sources.docs]] paths`, `[sources.code] roots`,
`[sources.doors.exchange] path`) son relativas a **la carpeta desde la que se lanza atlas**,
no a la de `atlas.toml`. Las de `[project] workspace`, `[agents] dirs` y
`[agents.eval] sets` sí son relativas al fichero. Por eso:
- hay que lanzar atlas desde la carpeta del proyecto: `cd C:\proyectos\tms` y luego
  `atlas run atlas.toml`;
- el editor tiene que arrancar `atlas serve` en esa carpeta (`cwd` en su configuración);
- lanzado desde otra carpeta, el workspace es el mismo pero las fuentes no se encuentran:
  los documentos avisan (ver abajo), el código da 0 ficheros sin avisar y, como
  `[pipeline] prune_missing` es `true` por defecto, lo que ya estaba procesado se quita del
  workspace (se vuelve a procesar al lanzarlo bien).

Una ruta absoluta evita el problema.

### `[sources.docs] 'nombre': ... does not exist`

Un `paths` de esa fuente no existe visto desde la carpeta actual: está mal escrito o atlas
se lanzó desde otra carpeta. La fuente sigue con las rutas que sí existen. Con
`atlas sections atlas.toml` se ve, sin gastar nada, qué documentos encuentra cada fuente.

Mensajes parecidos:
- `exchange database ... does not exist`: la base de intercambio de `[sources.doors]`
  (la que publica `publish.py`);
- `configuration file ... does not exist`: el `atlas.toml` de la línea de comandos;
- `CA certificate ... does not exist`: el PEM de `[network] ca_cert`.

### El código da 0 ficheros

Una raíz de `[sources.code] roots` que no existe no avisa: el paso `code.parse` no
encuentra unidades, y `atlas graph` no tiene la línea «code graph». Hay que comprobar la ruta y
la carpeta desde la que se lanza atlas, y que `languages` y `exclude` no dejen todo fuera.

## El editor

### El servidor MCP no llega al modelo

El editor arranca `atlas serve` con **su** entorno, no con el de la terminal: `atlas check`
puede ir bien en PowerShell y el servidor no tener ni el proxy ni la clave.
- Lo puesto con `$env:...` o en el `$PROFILE` solo vale en PowerShell. Para el editor:
  `setx` (o «Editar las variables de entorno de esta cuenta») y reiniciar el editor
  cerrando todas sus ventanas, o abrirlo desde esa PowerShell (`code .`, `opencode`).
- Los clientes hechos con la librería de MCP (Claude Desktop, el `McpToolset` de ADK)
  solo pasan `PATH` y pocas más: el proxy y la clave van en su `env`.
- En OpenCode van en `environment`; en VS Code, en `env` o en un `envFile` fuera del
  repositorio.

El servidor escribe al arrancar cómo llega al modelo y qué variables le faltan (en VS Code,
en la salida «MCP: atlas»), y las herramientas que usan el modelo las añaden a su error.
Más en la [guía de instalación](instalacion.md#el-entorno-del-servidor-proxy-y-clave).
