# Pruebas con modelos de OpenRouter

[OpenRouter](https://openrouter.ai) da acceso, con una sola clave y la API de OpenAI, a
cientos de modelos, entre ellos el del gateway corporativo (`google/gemma-4-26b-a4b-it`,
gemma 4 26B A4B). `atlas.openrouter.toml` lo usa para probar y comparar modelos de chat
sobre las muestras de `examples/samples`, sin descargarlos ni ocupar la memoria del Mac.

**Solo con las muestras.** OpenRouter es un servicio externo: el texto de cada petición
llega a OpenRouter y al proveedor que sirve el modelo. Las muestras son sintéticas; no
apuntes `atlas.openrouter.toml` a documentos, requisitos o código del proyecto (ver
[seguridad](seguridad.md)).

## Puesta en marcha

1. Una clave en [openrouter.ai/keys](https://openrouter.ai/keys), en el entorno (por
   ejemplo, en `~/.zshrc`):

   ```bash
   export OPENROUTER_API_KEY=sk-or-...
   ```

   Los modelos de pago necesitan crédito en la cuenta; los `:free`, no (ver
   [límites](#límites-y-coste)).
2. Los embeddings siguen en Ollama, con `embeddinggemma` como en el gateway, así que entre
   un modelo y otro solo cambia el de chat. Arranca Ollama antes:
   `OLLAMA_CONTEXT_LENGTH=16384 ollama serve`.
3. Las muestras (`make samples`) y la comprobación de las dos conexiones:

   ```bash
   make check-openrouter
   ```

## Elegir el modelo

El modelo va en la variable `ATLAS_MODEL`, con el nombre que le da OpenRouter; sin ella, el
del gateway. Cada modelo tiene su workspace, `workspace-openrouter/<modelo>`: cambiar de
modelo no reprocesa lo del anterior y sus informes no se pisan.

```bash
make eval-openrouter ATLAS_MODEL=qwen/qwen3.6-35b-a3b    # atlas run y atlas eval
ATLAS_MODEL=qwen/qwen3.6-35b-a3b atlas verify SRS-0001 atlas.openrouter.toml
```

`make eval-openrouter` procesa las muestras (solo la primera vez: luego no cambia nada) y
pasa los conjuntos de `[agents.eval]`. El informe queda en
`workspace-openrouter/<modelo>/reports/eval-assistant.json`.

Para comparar varios, `make compare-openrouter` hace lo mismo con cada uno y termina con una
tabla: respuestas correctas, mediana y total de segundos, tokens por caso, dólares en total
y por acierto (OpenRouter dice el coste de cada petición), acierto@3 y MRR de la búsqueda
híbrida, errores de la API y casos fallados. La tabla y cada caso de cada modelo (si pasó,
segundos, tokens y coste) quedan en `reports/compare-<fecha>.md` y `.json`, junto a la
configuración.

```bash
make compare-openrouter MODELS="google/gemma-4-26b-a4b-it qwen/qwen3.6-35b-a3b mistralai/mistral-small-2603"
python tools/compare_models.py MODELO... --no-run --out comparacion.md   # solo la tabla, de los informes que ya hay
```

Los modelos responden con algo de azar: para comparar con más seguridad, sube `runs` en
`[agents.eval]` (cada pregunta se hace esas veces).

Para comparar también cómo escriben código, el banco de encargos de `atlas develop`
(`examples/eval/develop.yaml`, un plan pequeño sobre una calculadora en Python) va aparte,
porque tarda minutos u horas por modelo:

```bash
make eval-develop ATLAS_MODEL=google/gemma-4-26b-a4b-it          # atlas eval --set examples/eval/develop.yaml
python tools/compare_models.py MODELO... --develop examples/eval/develop.yaml   # y una segunda tabla
```

La segunda tabla da, por modelo, las ejecuciones que llegaron a sus tests, los tests que pasan
al final, las ediciones (y cuántas fallaron), las compilaciones con errores, las llamadas, los
minutos y el coste.

Medido el 12 de octubre de 2026 con `make compare-openrouter`, sin razonamiento:

| Modelo | Asistente | Mediana | `atlas run` desde cero |
|---|---|---|---|
| `google/gemma-4-26b-a4b-it` | 24 de 24 | 5 s | 29 s |
| `qwen/qwen3.6-35b-a3b` | 24 de 24 | 5 s | 22 s |
| `google/gemma-4-31b-it` | 24 de 24 | 6 s | 33 s |

Como referencia, `gemma4:12b` en Ollama: 23 de 24, mediana 11 s, 62 s. Las tres pruebas, con
las comprobaciones previas, costaron 0,03 dólares. `examples/eval/assistant.yaml` ya no
distingue entre estos modelos: para compararlos hacen falta preguntas más difíciles o
tareas más largas (`atlas verify`, `comply`, `impact`, `coverage`).

Algunos candidatos, del catálogo del 12 de octubre de 2026 (precio en dólares por millón de
tokens de entrada / salida; el catálogo y sus precios cambian, ver
[openrouter.ai/models](https://openrouter.ai/models)):

| Modelo | Precio | Notas |
|---|---|---|
| `google/gemma-4-26b-a4b-it` | 0,08 / 0,26 | el del gateway (MoE, 4B activos); por defecto |
| `google/gemma-4-26b-a4b-it:free` | gratis | el mismo, con los límites de los `:free` |
| `google/gemma-4-31b-it` | 0,09 / 0,34 | gemma 4 denso, el mayor |
| `qwen/qwen3.6-35b-a3b` | 0,10 / 0,95 | MoE de 3B activos; razona por defecto |
| `mistralai/mistral-small-2603` | 0,15 / 0,60 | 24B |
| `openai/gpt-oss-120b` | 0,04 / 0,17 | razona siempre: `ATLAS_REASONING=low` |

## Razonamiento

`ATLAS_REASONING` (por defecto `none`) va a `[llm] reasoning_effort`, que usa el análisis
de documentos, y a `[agents.assistant]`, el agente de `atlas eval`. Los demás agentes de
serie apagan el razonamiento en su fichero; para probarlos con él, `reasoning_effort` en
`[agents.<nombre>]`. Valores: `none`, `minimal`, `low`, `medium`, `high`.

Cada modelo es distinto: gemma 4 no razona si no se le pide, qwen3.6 sí (y `none` lo apaga),
y gpt-oss razona siempre y rechaza `none`. `GET https://openrouter.ai/api/v1/models` lo dice
en el campo `reasoning` de cada modelo (`mandatory`, `default_enabled`, `supported_efforts`).
El workspace es el mismo con o sin razonamiento: un `eval` pisa el informe del anterior.

## Proveedores

OpenRouter reparte las peticiones de un modelo entre varios proveedores, que pueden usar otra
cuantización o tratar distinto las llamadas a herramientas. Para comparar con rigor, fija uno
con `[llm.extra_body]`, cuyos campos van tal cual en cada petición:

```toml
[llm.extra_body]
provider = { order = ["deepinfra"], allow_fallbacks = false }   # los de cada modelo, en su página
```

Con `provider = { data_collection = "deny" }` solo se usan proveedores que no guardan los
datos. Si OpenRouter responde «No endpoints found matching your data policy», la política de
datos de la cuenta ([openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy))
no deja usar ningún proveedor de ese modelo (pasa con algunos `:free`, cuyos proveedores
pueden guardar los datos).

## Límites y coste

- `atlas.openrouter.toml` limita el chat a 28 peticiones por minuto, como el gateway, para
  que los tiempos se parezcan a los de producción. Los modelos de pago no tienen otro límite
  en OpenRouter.
- Los `:free` admiten 20 por minuto (atlas baja el ritmo solo con el primer rechazo) y 50 al
  día, o 1000 si la cuenta ha comprado al menos 10 dólares de crédito.
- Un 429 puede venir del proveedor del modelo y no de la cuenta: «temporarily rate-limited
  upstream». atlas baja el ritmo igual; si se repite, fija otro proveedor (ver
  [Proveedores](#proveedores)) o espera un rato.
- Sin crédito, OpenRouter responde 402 y atlas se para con «the API asks for payment».
- El gasto de cada ejecución se ve en [openrouter.ai/activity](https://openrouter.ai/activity),
  o con:

  ```bash
  curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"
  ```
