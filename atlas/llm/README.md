# atlas.llm — conexión con los modelos

Todo lo que habla con una API compatible con OpenAI pasa por aquí: el análisis de
documentos, los embeddings del índice, las respuestas y los agentes (`AtlasLlm` usa este
mismo cliente). Viene de `legacy/conexion.py`, traducido al inglés (`connection.py`).

| Fichero | Qué tiene |
|---|---|
| `connection.py` | configuración de red, cliente OpenAI con proxy, TLS, límites y reintentos, diagnóstico de errores, `atlas check` y los contadores de uso |
| `structured.py` | `JsonLlm`: pedir un objeto JSON en el escalón de `response_format` que acepte el servidor, comprobarlo contra su esquema y recuperarlo aunque el modelo lo envuelva, lo corte o lo rompa; respuestas de texto que avisan si salen cortadas |
| `probe.py` | `atlas probe`: si el modelo de chat funciona como agente, con tres peticiones pequeñas |

## Conexión

`from_config(cfg)` construye la conexión de `[llm]`, `[embeddings]` y `[network]` (ver la
[guía de configuración](../../docs/configuracion.md)). Cada cliente sale de
`create_client`, cuyo transporte HTTP:
- usa el proxy y los certificados de la configuración (`ca_cert = "system"`: el almacén del
  sistema);
- respeta el límite de peticiones y de tokens por minuto, repartiendo las peticiones en el
  tiempo. Lo comparten todos los clientes que van al mismo servidor con la misma clave;
- con un 409 o un 429 espera (`Retry-After`, `x-ratelimit-*`, cuyo `reset` puede venir en
  segundos o en milisegundos, o espera creciente) hasta
  `rate_limit_max_wait`, sin contarlo como reintento; si es adaptativo, baja el ritmo un
  30 % y lo recupera poco a poco;
- reintenta los 5xx y los fallos de conexión puntuales, pero no los de certificado.

`diagnose(e)` convierte los errores típicos de una red corporativa (certificado, proxy con
autenticación, TLS mutuo, credenciales, URL sin `/v1`) y la falta de crédito (402) en una
indicación concreta, con lo que diga el servidor (y, en OpenRouter, el proveedor), y `is_connection_error(e)` dice cuáles
hacen inútil seguir: el motor los trata como fatales.

`atlas check atlas.toml` hace una petición real al chat y otra a los embeddings y muestra la
configuración efectiva con las claves enmascaradas. Reconoce los modelos que razonan: dice
`OK: 'ok' (reasoning model: …)`, o explica el fallo si el modelo agota `max_tokens`
razonando sin contestar. Si el chat funciona, dice además qué salida estructurada acepta el
servidor: `structured output: json_schema | json_object | none` (una petición pequeña, y una
más por cada escalón que rechace).

`atlas probe atlas.toml` prueba el modelo como lo usan los agentes, con razonamiento
(`[llm] reasoning_effort`, o `low` si está vacío o es `none`):
1. una pregunta que necesita una herramienta: el modelo tiene que llamarla con los argumentos
   correctos;
2. el resultado de la herramienta: tiene que contestar con él;
3. la misma petición sin el razonamiento de la primera vuelta: si el servidor da los tokens
   del prompt, la diferencia dice si vuelve a meter el razonamiento que atlas le devuelve en
   `[llm] reasoning_field`. Si no lo aprovecha, propone el otro campo.

Sirve para comprobar el gateway: las primeras plantillas de chat de gemma 4 cerraban el turno
a mitad de una llamada a herramienta, y OpenCode entraba en bucles. Con `gemma-4-26b` en
OpenRouter (2026-10-15): llama a la herramienta en 2 s, contesta con el resultado y el servidor
mete el razonamiento en el prompt (222 tokens con él, 149 sin él).

`[llm] extra_body` añade campos tal cual a cada petición de chat (de `JsonLlm`, de los
agentes y de `atlas check`): las opciones propias de un servidor que la API de OpenAI no
tiene, como `provider` en OpenRouter o `chat_template_kwargs` en vLLM.

## Respuestas en JSON

`JsonLlm.ask_json` pide un objeto con un esquema y lo recupera en cuatro pasos: el mayor
objeto JSON del texto que tenga alguna de las claves esperadas, lo que se pueda reparar de
uno cortado, la comprobación contra el esquema (`validate`), y, si no hay objeto o no cumple
el esquema, una corrección pedida al modelo citándole su primer error («That JSON object
does not follow the schema: requirements[2].text must be a string»). Si la corrección no
mejora (tiene tantos errores o más), se queda la primera respuesta y quien llama la ajusta
(`normalize` en `atlas.docs`, `_clean` en `verify`, `link` y `comply`). Una respuesta cortada
por longitud nunca se da por buena: `TruncatedResponse` lleva lo reparado para que quien
llama divida su entrada y pregunte otra vez.

`validate(obj, schema)` devuelve la lista de lo que incumple, con el sitio de cada cosa, para
el subconjunto de JSON Schema que usa atlas: `type`, `properties`, `required`, `items` y
`enum`. No hace falta ninguna dependencia.

`response_format` se pide en tres escalones: `json_schema`, `json_object` (casi todos los
servidores que rechazan un esquema lo aceptan, y ya evita la prosa y los bloques de código)
y ninguno. Si el servidor rechaza un escalón (400, 422 o 501, como Ollama con algunos
modelos), se baja al siguiente, y lo aprendido vale para todo el proceso: se guarda por
(`base_url`, `model`), así que los `JsonLlm` que se crean por cada orden o por cada llamada
MCP (`verify_requirement`, `link_requirement`) no pagan otra petición rechazada.
`JsonLlm.structured` (o `structured_output(cfg)`) dice en qué escalón está.

`JsonLlm.ask_text` mira `finish_reason`: si la respuesta llega vacía o cortada por
`max_tokens`, pregunta otra vez con el doble de presupuesto (el doble de los tokens que gastó
la primera si no hay `max_tokens`). Devuelve un `TextAnswer`, un `str` con `truncated` (sigue
cortada después de repetir) y `requests` (1 o 2). `atlas ask` y `ask_docs` lo dicen: la
respuesta lleva `truncated` y `atlas ask` avisa de que conviene subir `[llm] max_tokens`.

`concurrency` limita cuántas peticiones de una misma instancia hay en vuelo a la vez, aunque
la usen muchos hilos: así comparten las secciones de todos los documentos que se procesan
juntos `[pipeline] workers` peticiones.

## Contadores de uso

`usage()` devuelve lo que ha pedido el proceso desde que empezó: peticiones por destino
(`chat`, `embeddings`, `other`), rechazos por cupo (`rejected`), reintentos (`retried`),
segundos esperando al limitador (`wait_seconds`) y al servidor (`api_seconds`), y kB
enviados (`sent_kb`). Los perfiles de `atlas run` guardan lo que creció en cada paso.

## Como librería

```python
from atlas.config import load_config
from atlas.llm import JsonLlm, create_client, from_config, usage

conn = from_config(load_config("atlas.dev.toml"))
client, proxy = create_client(conn.llm)              # un cliente OpenAI con todo lo anterior
llm = JsonLlm(conn.llm, concurrency=4)
data = llm.ask_json("Answer in JSON.", "List two colours.", {"type": "object", "properties":
                    {"colours": {"type": "array", "items": {"type": "string"}}}}, "colours")
print(data["colours"], usage()["chat"])
```

También funciona solo, con su propio `connection.toml`:
`python -m atlas.llm.connection init | show | check`.

## Pruebas

`tests/test_fake_api.py` (cliente, `check` con modelos que razonan y el escalón de salida
estructurada, contadores de uso, la espera por `Retry-After` y por las cabeceras
`x-ratelimit-*` por HTTP, reintentos de los 5xx, un cupo que no se libera, `usage` del tamaño
real, `atlas probe` contra un servidor que llama a herramientas), `tests/test_llm_structured.py`
(reparación y validación de JSON, la corrección que cita el error, los escalones
`json_schema → json_object → none` compartidos entre instancias, `finish_reason` en
`ask_text`, razonamiento, límite de concurrencia) y `tests/test_config.py` (secretos y
enmascarado).

Todas usan el servidor falso (`tests/fakes/fake_api.py`), cuyo fichero de control, releído en
cada petición, inyecta los fallos: `chat_429_after`, `chat_status_once: [500, 503]`,
`retry_after` y `ratelimit_headers: {remaining, reset}` en los rechazos, `tool_call: {name,
args}`, `usage_chars_per_token` (con él, una respuesta más larga que `max_tokens` sale cortada
con `finish_reason` `length`) y `no_structured_output` (`true`, o `"json_schema"` para aceptar
`json_object`), además de los de antes. La lista completa está al principio del fichero.
