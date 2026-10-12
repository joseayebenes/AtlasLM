# atlas.llm — conexión con los modelos

Todo lo que habla con una API compatible con OpenAI pasa por aquí: el análisis de
documentos, los embeddings del índice, las respuestas y los agentes (`AtlasLlm` usa este
mismo cliente). Viene de `legacy/conexion.py`, traducido al inglés (`connection.py`).

| Fichero | Qué tiene |
|---|---|
| `connection.py` | configuración de red, cliente OpenAI con proxy, TLS, límites y reintentos, diagnóstico de errores, `atlas check` y los contadores de uso |
| `structured.py` | `JsonLlm`: pedir un objeto JSON y recuperarlo aunque el modelo lo envuelva, lo corte o lo rompa |

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
indicación concreta, con lo que diga el servidor, y `is_connection_error(e)` dice cuáles
hacen inútil seguir: el motor los trata como fatales.

`atlas check atlas.toml` hace una petición real al chat y otra a los embeddings y muestra la
configuración efectiva con las claves enmascaradas. Reconoce los modelos que razonan: dice
`OK: 'ok' (reasoning model: …)`, o explica el fallo si el modelo agota `max_tokens`
razonando sin contestar.

## Respuestas en JSON

`JsonLlm.ask_json` pide un objeto con un esquema y lo recupera en tres pasos: el mayor
objeto JSON del texto que tenga alguna de las claves esperadas, lo que se pueda reparar de
uno cortado, y, si nada sirve, una corrección pedida al modelo enseñándole su error. Una
respuesta cortada por longitud nunca se da por buena: `TruncatedResponse` lleva lo
reparado para que quien llama divida su entrada y pregunte otra vez.

Si el servidor no acepta `response_format` con esquema (400, 422 o 501, como Ollama con
algunos modelos), sigue sin él. `concurrency` limita cuántas peticiones de una misma
instancia hay en vuelo a la vez, aunque la usen muchos hilos: así comparten las secciones
de todos los documentos que se procesan juntos `[pipeline] workers` peticiones.

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

`tests/test_fake_api.py` (cliente, `check` con modelos que razonan, contadores de uso),
`tests/test_llm_structured.py` (reparación de JSON, servidores sin salida estructurada,
razonamiento, límite de concurrencia) y `tests/test_config.py` (secretos y enmascarado).
