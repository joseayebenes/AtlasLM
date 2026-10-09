# doc-rag — RAG local para documentación técnica

Convierte documentos técnicos (PDF o Markdown) en una base de conocimiento consultable. Un
modelo de lenguaje (Gemma 4 u otro, a través de una API compatible con OpenAI) lee cada
sección y extrae su resumen, requisitos, definiciones, entidades y puntos abiertos. El
resultado se guarda como Markdown estructurado y se indexa en una base de datos con
búsqueda híbrida (vectorial + palabras clave + identificadores exactos). Después se puede
preguntar en lenguaje natural y obtener respuestas **basadas solo en los documentos y con
citas** a documento, sección y página, desde la línea de comandos o desde un agente vía MCP.

Está pensado para documentación grande (cientos de secciones), APIs con límite de
peticiones y redes corporativas con proxy y certificados propios, y para que cualquier
error, corte o Ctrl+C se pueda reanudar sin repetir trabajo.

## Contenido

1. [Componentes](#1-componentes)
2. [Instalación](#2-instalación)
3. [Inicio rápido](#3-inicio-rápido)
4. [Arquitectura](#4-arquitectura)
5. [Funcionamiento detallado](#5-funcionamiento-detallado)
6. [Peticiones a la IA](#6-peticiones-a-la-ia)
7. [Diagrama de clases](#7-diagrama-de-clases)
8. [Configuración de la conexión (`conexion.toml`)](#8-configuración-de-la-conexión-conexiontoml)
9. [Referencia de la línea de comandos](#9-referencia-de-la-línea-de-comandos)
10. [Ficheros generados y base de datos](#10-ficheros-generados-y-base-de-datos)
11. [Servidor MCP para agentes](#11-servidor-mcp-para-agentes)
12. [Rendimiento y ajuste](#12-rendimiento-y-ajuste)
13. [Solución de problemas](#13-solución-de-problemas)
14. [Limitaciones conocidas](#14-limitaciones-conocidas)

> Los diagramas están en [Mermaid](https://mermaid.js.org/). GitHub y GitLab los muestran
> como gráficos; en VS Code hace falta una extensión de vista previa de Mermaid.

---

## 1. Componentes

| Fichero | Función |
|---|---|
| `conexion.py` | Todo lo relativo a la conexión con las APIs: carga de configuración, proxy, certificados, límite de peticiones, reintentos, diagnóstico de errores. Ejecutable por sí solo (`init`, `show`, `check`). |
| `conexion.toml` | Configuración de la conexión: URLs, modelos, claves, red y límites. |
| `pdf_struct.py` | Extracción del texto, detección de la estructura, análisis de cada sección con el LLM y escritura de los Markdown estructurados. Se puede usar sin indexar. |
| `doc_rag.py` | Indexación (embeddings + SQLite), sesiones de ingesta con reanudación, búsqueda, respuestas citadas y servidor MCP. Usa los dos anteriores. |

Los tres `.py` deben estar en la misma carpeta.

## 2. Instalación

Requiere **Python 3.10 o superior** (3.11+ recomendado: lee el TOML sin dependencias extra).

```bash
pip install openai numpy "markitdown[pdf]"   # imprescindibles
pip install pymupdf      # recomendado: estructura a partir de los marcadores del PDF
pip install mcp          # solo para el modo servidor MCP
pip install truststore   # solo si usas ca_cert = "system"
pip install tomli        # solo con Python < 3.11
```

Necesitas un servidor con API compatible con OpenAI que ofrezca un modelo de chat y uno de
embeddings: Ollama, vLLM, llama.cpp server o un gateway corporativo. Con Ollama en local:

```bash
ollama pull gemma4            # o el tag de Gemma 4 que uses
ollama pull embeddinggemma
```

## 3. Inicio rápido

```bash
# 1. Crear y editar la configuración de conexión
python conexion.py init        # crea conexion.toml comentado
python conexion.py show        # configuración efectiva (secretos ocultos)
python conexion.py check       # llamada real al LLM y a los embeddings

# 2. Ver cómo se va a trocear un documento (sin llamar al modelo)
python pdf_struct.py manual.pdf --dry-run

# 3. Ingerir: entender, estructurar e indexar (uno o varios ficheros o carpetas)
python doc_rag.py ingest manual.pdf otros_docs/ --workers 4

# 4. Si algo falla o se corta
python doc_rag.py status       # qué quedó pendiente y por qué
python doc_rag.py retry        # reprocesa solo lo pendiente

# 5. Consultar
python doc_rag.py search "DELETE-X_2-MSG"
python doc_rag.py ask "¿Cuánto tarda en borrarse una traza?"

# 6. Dar acceso a un agente (OpenCode, Claude Code...)
python doc_rag.py mcp
```

Las opciones globales de `doc_rag.py` (`--db`, `--config`, `--proxy`, `--rpm`...) van
**antes** del comando: `python doc_rag.py --config otra.toml ask "..."`.

---

## 4. Arquitectura

### 4.1 Vista general

```mermaid
flowchart LR
    subgraph Entrada
        PDF["PDF / Markdown"]
    end
    subgraph pdf_struct.py
        EXT["Extracción<br/>markitdown + limpieza"] --> EST["Estructura<br/>marcadores · títulos · tamaño"]
        EST --> MAP["Análisis por sección<br/>(map, LLM)"]
        MAP --> RED["Resumen global<br/>(reduce, LLM)"]
        RED --> ESC["Consolidación y<br/>escritura Markdown"]
    end
    subgraph doc_rag.py
        IDX["Indexación<br/>registros + embeddings"] --> DB[("rag.db<br/>SQLite + FTS5")]
        DB --> BUS["Búsqueda híbrida<br/>RRF + contexto"]
        BUS --> RES["Respuesta citada<br/>(LLM)"]
    end
    subgraph conexion.py
        CX["Clientes OpenAI<br/>proxy · TLS · límite · reintentos"]
    end
    API[("API LLM +<br/>embeddings")]

    PDF --> EXT
    ESC --> MD["Markdown estructurado<br/>index · secciones · requisitos<br/>glosario · entidades · pendientes"]
    ESC --> IDX
    MAP -. peticiones .-> CX
    RED -. peticiones .-> CX
    IDX -. peticiones .-> CX
    BUS -. peticiones .-> CX
    RES -. peticiones .-> CX
    CX <--> API
    CLI["CLI: search / ask"] --> BUS
    AG["Agente vía MCP"] --> BUS
```

Toda petición a la IA pasa por `conexion.py`, que aplica la misma política de red, límite de
peticiones y reintentos tanto al modelo de chat como al de embeddings.

### 4.2 Flujo de una ingesta

```mermaid
flowchart TD
    A([ingest fichero]) --> B{¿al día?<br/>completo, mismo hash,<br/>mismos modelos, en la BD}
    B -- sí --> Z([se omite])
    B -- no --> C[Extraer texto<br/>markitdown]
    C --> D[Quitar cabeceras y<br/>pies de página repetidos]
    D --> E[Detectar estructura<br/>y preparar secciones]
    E --> F{¿sección en caché?}
    F -- sí --> H
    F -- no --> G[Analizar con el LLM<br/>reparar · corregir · dividir]
    G --> H[Guardar en caché<br/>y en estado.json]
    H --> I{¿quedan secciones?}
    I -- sí --> F
    I -- no --> J[Resumen global<br/>con caché]
    J --> K[Consolidar y escribir<br/>Markdown + estructura.json]
    K --> L[Construir registros<br/>multigranularidad]
    L --> M{¿vector en caché?}
    M -- no --> N[Embeddings por lotes<br/>guardados lote a lote]
    M -- sí --> O
    N --> O[Guardar documento<br/>en rag.db]
    O --> P([completo / incompleto])
```

Cada paso actualiza `estado.json` (por documento) y `<bd>_sesion.json` (por sesión). Los
resultados válidos se guardan en caché en cuanto se obtienen, así que relanzar solo procesa
lo que falta (ver [5.8](#58-estado-de-la-sesión-y-reanudación)).

---

## 5. Funcionamiento detallado

### 5.1 Extracción y limpieza

El texto se extrae con **markitdown** (que usa pdfminer). La entrada también puede ser
Markdown o texto plano. Antes de buscar la estructura se eliminan las **cabeceras y pies de
página repetidos**: líneas cortas (≤ 120 caracteres) entre las tres primeras o las tres
últimas de cada página que se repiten en al menos el 30 % de las páginas, comparadas con los
números sustituidos por `#` (así `Page 12 of 300` y `Page 13 of 300` cuentan como la misma
línea). Esto limpia los fragmentos y evita que un número de página se tome por un título.

El texto resultante se guarda en `<salida>/_texto_extraido.md` para poder revisarlo.

### 5.2 Detección de la estructura

La calidad de todo lo demás depende de trocear el documento por sus secciones reales. Con
`--estructura auto` (por defecto) se prueba en este orden:

```mermaid
flowchart TD
    A([texto extraído]) --> B["Reunir número y título separados<br/>3.2.1 + Track Management → 3.2.1 Track Management"]
    B --> C{¿PDF con marcadores?<br/>pymupdf instalado}
    C -- "sí, y se localiza en el texto<br/>al menos la mitad" --> D[Secciones por marcadores]
    C -- no --> E{¿títulos en el texto?<br/>Markdown # o numerados}
    E -- "≥ 2 títulos" --> F[Secciones por títulos]
    E -- no --> G[Bloques de tamaño fijo]
    D --> H{¿sección > max_chars?}
    F --> H
    H -- "sí y tiene títulos internos" --> I[Subdividir por esos títulos]
    H -- no --> J
    I --> J["Agrupar secciones cortas &lt; min_chars<br/>(mismo capítulo, sin pasar de max_chars)"]
    G --> J
    J --> K["Trocear lo que aún supere max_chars<br/>(por párrafos)"]
    K --> L([secciones 001…N])
```

- **Marcadores del PDF**: el índice navegable del documento. Es la fuente más fiable. Cada
  marcador se localiza en el texto de su página (con y sin el número delante); si se
  localizan menos de la mitad, se pasa a títulos.
- **Títulos numerados**: `2`, `2.1`, `3.4.1.2`, `A.1` seguidos de un título que empieza en
  mayúscula, más `Annex A` / `Appendix B` / `Anexo` / `Apéndice`. Se descartan entradas de
  índice (`1 Intro ........ 4`), líneas que acaban en puntuación, de más de 12 palabras o
  que contienen *shall/must/should*. pdfminer suele separar el número del título (incluso
  pone primero todos los números y luego todos los títulos): se vuelven a emparejar.
- **`--heading-regex`**: patrón propio para formatos de título distintos (grupo 1 = número,
  grupo 2 = título).
- **Agrupación** (`--min-chars`): las secciones cortas se unen con la anterior del mismo
  capítulo, para no gastar una petición en tres líneas. Los títulos agrupados quedan en
  `includes`.
- **Troceo** (`--max-chars`): lo que no cabe se parte por párrafos y se marca como
  `(parte k/n)`. Si una sección se parte en más de 5 trozos, el log lo avisa: casi siempre
  significa que no se han detectado sus títulos internos.

`python pdf_struct.py doc.pdf --dry-run` muestra las secciones resultantes, de dónde salió
la estructura, cuántas secciones habría con distintos `--min-chars` y, si hay problemas,
ejemplos de líneas numeradas aceptadas o rechazadas como título y el motivo.

### 5.3 Análisis de cada sección (map)

Cada sección se envía al LLM con instrucciones de extraer **solo lo que hay en esa sección**
y responder con un objeto JSON:

| Campo | Contenido |
|---|---|
| `summary` | 2-4 frases sobre lo que especifica la sección |
| `key_points` | hechos, valores o restricciones principales (máx. 8) |
| `requirements` | cada enunciado normativo (*shall/must/will/should* o etiquetado como requisito), con su texto **literal** y su identificador si aparece |
| `definitions` | términos, acrónimos y abreviaturas definidos |
| `entities` | sistemas, funciones, interfaces, mensajes, parámetros… más relevantes (máx. 15) |
| `references` | referencias a otras secciones, documentos o normas (máx. 10) |
| `open_issues` | TBD, TBC, TBA y puntos abiertos |

Si el servidor lo admite, se envía el esquema JSON (`response_format: json_schema`) para que
la salida sea válida por construcción; si lo rechaza, se sigue sin él. La respuesta se
procesa de forma tolerante (ver [6.2](#62-análisis-de-una-sección)): se repara si es un
detalle, se pide una corrección al modelo si no es JSON, y si la respuesta se cortó por
longitud la sección se **divide en dos** y se analiza cada mitad (hasta 8 trozos).

El resultado de cada sección se guarda en caché (`<salida>/.cache/`) en cuanto se obtiene.
La clave depende del modelo, el idioma, la versión del prompt y el texto exacto de la
sección: si nada de eso cambia, nunca se vuelve a pedir.

### 5.4 Resumen global (reduce)

Con los resúmenes de todas las secciones se pide un resumen del documento (`summary`,
`purpose`, `scope`, `main_topics`). Si no caben en una petición, se resumen por grupos y
después se resumen los grupos. El resultado también va a caché.

### 5.5 Consolidación y verificación

Se reúnen requisitos, glosario, entidades y pendientes de todo el documento, eliminando
duplicados. Cada requisito se **busca en el texto original** de su sección:

| Verificación | Significado |
|---|---|
| `literal` | el texto aparece tal cual (ignorando saltos de línea y puntuación) |
| `parcial` | coincide al menos el 85 % de las palabras |
| `no_encontrado` | el modelo lo ha reescrito o inventado: revisar a mano |

### 5.6 Indexación

Cada documento se indexa a **varias granularidades**, para que las preguntas generales
encuentren resúmenes y las concretas encuentren el texto exacto:

| Tipo de registro (`kind`) | Contenido |
|---|---|
| `overview` | resumen global del documento |
| `section_summary` | resumen y puntos clave de cada sección |
| `chunk` | fragmentos del texto (~1200 caracteres, 200 de solape, cortados por párrafos y frases) |
| `requirement` | cada requisito con su identificador |
| `definition` | cada término del glosario |
| `open_issue` | cada TBD/TBC |

**Recuperación contextual**: cada fragmento se convierte en vector junto con su ruta
(`Documento > 3 Capítulo > 3.2 Sección`) y el resumen de su sección, para que un fragmento
suelto no pierda su significado.

Los vectores se guardan normalizados en SQLite y además se indexa todo el texto en FTS5
(BM25). Los vectores calculados se guardan también en una **caché por texto**
(`embed_cache`), lote a lote: reindexar o reanudar no recalcula lo ya hecho.

Algunos modelos de embeddings esperan prefijos distintos para consultas y documentos; se
aplican automáticamente según el nombre del modelo:

| Modelo contiene | Prefijo de consulta | Prefijo de documento |
|---|---|---|
| `embeddinggemma` | `task: search result \| query: ` | `title: none \| text: ` |
| `nomic-embed` | `search_query: ` | `search_document: ` |
| `e5` | `query: ` | `passage: ` |

### 5.7 Búsqueda y respuesta

```mermaid
flowchart LR
    Q([pregunta]) --> RW["Reescritura (LLM)<br/>1-3 consultas, versión en inglés,<br/>palabras clave"]
    RW --> V["Vectores de todas las<br/>consultas (1 petición)"]
    V --> D1[Búsqueda vectorial]
    RW --> L1[BM25 / FTS5]
    RW --> R1[IDs exactos]
    D1 --> F["Fusión RRF<br/>(k = 60)"]
    L1 --> F
    R1 --> F
    F --> C["Contexto small-to-big<br/>fragmento + vecinos, ≤ 14000 car."]
    C --> A["Respuesta (LLM)<br/>solo con las fuentes, citas [n]"]
    A --> S([respuesta + fuentes])
```

- **Búsqueda híbrida**: la vectorial capta el significado; BM25 y la búsqueda exacta de
  identificadores encuentran códigos como `DELETE-X_2-MSG`, que los vectores manejan mal.
- **Reciprocal Rank Fusion** combina los rankings sin tener que calibrar puntuaciones.
- **Contexto ampliado**: cada fragmento encontrado se pasa con sus vecinos de la misma
  sección, sin duplicados, hasta 14000 caracteres.
- **Respuesta fundamentada**: el modelo solo puede usar las fuentes numeradas, debe citar
  cada afirmación con `[n]`, copiar identificadores y valores tal cual, decir cuándo la
  información no está y señalar contradicciones. Responde en el idioma de la pregunta.

### 5.8 Estado de la sesión y reanudación

| Nivel | Fichero | Contenido |
|---|---|---|
| Documento | `<salida>/estado.json` | etapas (`extraccion`, `analisis`, `resumen`, `escritura`, `embeddings`, `indice`), secciones con error o pendientes (con mensaje y número de fallos) y lotes de embeddings fallidos (registros, secciones, tipos, error) |
| Sesión | `<bd>_sesion.json` | un registro por fichero: estado, opciones de la ingesta, hash, modelos, pendientes y último error |

Ambos se escriben de forma atómica (fichero temporal + renombrado) tras cada sección y cada
lote. El estado de un fichero en la sesión sigue este ciclo:

```mermaid
stateDiagram-v2
    [*] --> pendiente: registrado en ingest
    pendiente --> en_curso: empieza
    en_curso --> completo: sin pendientes
    en_curso --> incompleto: secciones o lotes con error
    en_curso --> error: fallo propio del fichero<br/>(p. ej. PDF corrupto)
    en_curso --> interrumpido: error fatal o Ctrl+C
    incompleto --> en_curso: retry
    error --> en_curso: retry
    interrumpido --> en_curso: retry
    pendiente --> en_curso: retry
    completo --> [*]
```

- Un **error propio de un fichero** lo marca y se sigue con el siguiente.
- Un **error fatal** (sin conexión, credenciales rechazadas, límite agotado tras esperar) o
  **Ctrl+C** detiene la sesión; los ficheros sin empezar quedan `pendiente`.
- Los documentos con pendientes **se indexan igualmente** con lo que sí funcionó; las
  secciones sin análisis aparecen marcadas con ⚠ en sus Markdown.
- `retry` reprocesa los ficheros que no están `completo` con las mismas opciones con que se
  ingirieron. Gracias a las cachés solo se llama a la API para lo que falta.
- Un `ingest` repetido omite los ficheros al día (completos, mismo contenido, mismos
  modelos e indexados). `--force` los reprocesa.
- Si un documento cambia, su estado se reinicia, pero las secciones que no cambiaron siguen
  saliendo de la caché.
- La base de datos, la sesión y las carpetas de salida no dependen del directorio desde el
  que se lance el comando: por defecto van junto a los scripts (ver `--db`).

### 5.9 Control de peticiones y reintentos

Cada cliente HTTP lleva un **transporte propio** que aplica, en este orden, el límite de
ritmo, el envío y la política de reintentos. Los reintentos del SDK de OpenAI se desactivan
para que no se multipliquen.

- **Contador compartido** por servidor y clave de API: si LLM y embeddings van a la misma
  URL con la misma clave, comparten límite (manda el valor más restrictivo).
- **Peticiones por minuto** (`requests_per_minute`): ventana deslizante de 60 s **y**
  espaciado uniforme de 60/rpm segundos entre envíos, para cumplir también con servidores
  que miden a ritmo constante.
- **Tokens por minuto** (`tokens_per_minute`): estimados como caracteres/4 del cuerpo.
- **Simultáneas** (`max_concurrent`).
- **Adaptativo** (`adaptive`): cada rechazo por límite baja el ritmo un 30 % (como mucho
  una vez cada 10 s); tras un minuto sin rechazos sube 1 petición/min cada 30 s hasta el
  configurado. Sin `requests_per_minute`, descubre el límite real.
- **Cabeceras del servidor**: `Retry-After`, `retry-after-ms` y `x-ratelimit-remaining-*` /
  `x-ratelimit-reset-*` (y variantes) se respetan; si avisan de que no queda cupo, todos
  los hilos pausan antes de recibir un rechazo.
- **Rechazos por límite (409/429)**: no cuentan como fallos; se espera (Retry-After o
  tiempo creciente) hasta `rate_limit_max_wait` segundos en total por petición. En el
  primer rechazo se muestra la respuesta del servidor y sus cabeceras de límite.
- **Errores 5xx**: hasta `max_retries` reintentos con espera exponencial (máx.
  `max_backoff`).
- **Errores de conexión**: 2 reintentos cortos. **Errores TLS**: ninguno (esperar no los
  arregla).

---

## 6. Peticiones a la IA

### 6.1 Cuántas peticiones hace cada operación

| Operación | Chat (LLM) | Embeddings |
|---|---|---|
| `ingest` (por documento) | 1 por sección no cacheada, +1 si hay que pedir corrección, +2 por cada división de una sección cortada; +1 de resumen global (más si los resúmenes no caben en `max_chars`) | ⌈registros sin vector en caché / `batch`⌉ |
| `retry` | solo las secciones pendientes (+ resumen si cambió) | solo los registros sin vector |
| `search` | 0 (1 con `--rewrite`) | 1 |
| `ask` | 2: reescritura + respuesta (1 con `--no-rewrite`) | 1 (todas las consultas en una petición) |
| `check` | 1 | 1 |
| MCP `search_docs` | 0 | 1 |
| MCP `ask_docs` | 2 | 1 |
| MCP `get_section`, `get_requirement`, `list_documents` | 0 | 0 |

Ejemplo: un documento de 500 secciones que genera 6000 registros, con `batch = 64`, hace
unas 501 peticiones de chat y 94 de embeddings. Con un límite de 28 peticiones por minuto
eso son, como mínimo, unos 21 minutos.

### 6.2 Análisis de una sección

```mermaid
sequenceDiagram
    autonumber
    participant P as procesar()
    participant A as analizar_seccion()
    participant C as Caché (.cache/)
    participant L as LLM.json()
    participant T as Transporte (conexion.py)
    participant S as API chat

    P->>A: sección N
    A->>C: ¿clave (modelo, idioma, prompt, texto)?
    alt en caché
        C-->>A: análisis guardado
    else no está
        A->>L: instrucciones + texto de la sección (+ esquema JSON)
        L->>T: POST /chat/completions
        T->>S: envío respetando el límite
        S-->>T: respuesta + finish_reason
        T-->>L: respuesta
        alt JSON válido con las claves esperadas
            L-->>A: análisis
        else cortada por longitud (finish_reason = length)
            L-->>A: RespuestaTruncada
            Note over A: dividir la sección en 2 y analizar cada mitad<br/>(hasta 8 trozos), después fusionar
        else JSON reparable (comas sobrantes, texto alrededor, cortes)
            L-->>A: análisis reparado
        else no es JSON
            L->>T: misma conversación + respuesta errónea + "solo el JSON"
            T->>S: petición de corrección
            S-->>L: respuesta
            L-->>A: análisis, o error (si la sección es larga, se divide)
        end
        A->>C: guardar análisis
    end
    A-->>P: análisis
    P->>P: estado.json (ok / error) y log con duración
```

Las respuestas no válidas se guardan en `<salida>/_fallos_llm/<sección>_intentoN.txt` con su
`finish_reason`, para poder ver qué devolvió el modelo.

### 6.3 Embeddings por lotes

```mermaid
sequenceDiagram
    autonumber
    participant I as indexar()
    participant DB as rag.db
    participant E as Embedder
    participant T as Transporte (conexion.py)
    participant S as API embeddings

    I->>I: construir registros (overview, resúmenes, fragmentos,<br/>requisitos, definiciones, TBD)
    I->>DB: embed_cache, ¿qué vectores ya existen?
    DB-->>I: vectores en caché
    loop cada lote de "batch" registros sin vector
        I->>E: textos del lote (con prefijo de documento)
        E->>T: POST /embeddings
        T->>S: envío respetando el límite
        S-->>E: vectores
        E-->>I: vectores normalizados
        I->>DB: embed_cache (commit lote a lote)
    end
    I->>DB: documento + registros con vector + FTS5
    Note over I,DB: un lote fallido se anota en estado.json<br/>y el resto se indexa igualmente
```

### 6.4 Pregunta con respuesta citada (`ask`)

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuario o agente
    participant R as doc_rag.py
    participant DB as rag.db
    participant S as API (chat + embeddings)

    U->>R: ask "pregunta"
    opt salvo --no-rewrite
        R->>S: chat: reescribir la pregunta (JSON queries + keywords)
        S-->>R: 1-3 consultas (incluida una en inglés) + palabras clave
    end
    R->>S: embeddings: todas las consultas en una sola petición
    S-->>R: vectores
    R->>DB: vectorial + BM25 + IDs exactos por cada consulta
    DB-->>R: candidatos
    R->>R: fusión RRF, top k, añadir fragmentos vecinos
    R->>S: chat: fuentes numeradas + pregunta
    S-->>R: respuesta con citas [n]
    R-->>U: respuesta + fuentes (documento, sección, páginas, fichero)
```

### 6.5 Control de ritmo y reintentos de cada petición

```mermaid
sequenceDiagram
    autonumber
    participant C as Cliente OpenAI
    participant T as TransporteLimitado
    participant L as Limitador (compartido)
    participant S as Servidor

    C->>T: petición HTTP
    loop hasta obtener respuesta o agotar la espera
        T->>L: esperar(tokens)
        Note over L: ventana de 60 s + espaciado 60/rpm<br/>+ tokens/min + pausa común
        L-->>T: adelante
        T->>S: envío
        S-->>T: respuesta
        opt x-ratelimit-remaining = 0
            T->>L: pausar(hasta el reset)
        end
        alt 2xx o error no reintentable (400, 401...)
            T-->>C: respuesta
        else 409 / 429 (límite)
            T->>L: rechazo(), ritmo −30 %
            T->>L: pausar(Retry-After o espera creciente)
            Note over T: hasta rate_limit_max_wait en total
        else 5xx o conexión caída
            T->>L: pausar(espera exponencial)
            Note over T: hasta max_retries (conexión: 2)
        end
    end
```

Los errores tras los que no tiene sentido seguir (conexión, autenticación, permisos, límite
agotado) se tratan como **fatales**: se guarda el estado y se detiene la sesión. El resto
(un JSON imposible, un lote rechazado) se anotan como pendientes y el proceso continúa.

---

## 7. Diagrama de clases

```mermaid
classDiagram
    direction LR

    class Conexion {
        <<conexion.py>>
        +ServiceConfig llm
        +ServiceConfig embeddings
        +str fichero
    }
    class ServiceConfig {
        <<conexion.py>>
        +str base_url
        +str model
        +str api_key
        +float temperature
        +int max_tokens
        +int batch
        +NetConfig net
    }
    class NetConfig {
        <<conexion.py>>
        +str proxy
        +str ca_cert
        +str client_cert
        +str client_key
        +str no_proxy
        +bool insecure
        +float timeout
        +int requests_per_minute
        +int tokens_per_minute
        +bool adaptive
        +int max_concurrent
        +int max_retries
        +float max_backoff
        +float rate_limit_max_wait
        +list retry_statuses
    }
    class Limitador {
        <<conexion.py>>
        +float rpm
        +int tpm
        +bool adaptive
        +esperar(tokens)
        +pausar(segundos)
        +rechazo()
    }
    class TransporteLimitado {
        <<conexion.py>>
        +handle_request(request) Response
    }
    class LLM {
        <<pdf_struct.py>>
        +ServiceConfig cfg
        +OpenAI client
        +str model
        +bool schema_ok
        +json(system, user, schema, nombre, guardar_fallo) dict
        -_llamar(messages, schema, nombre) tuple
    }
    class Estado {
        <<pdf_struct.py>>
        +Path path
        +dict d
        +guardar()
        +etapa(nombre, estado)
        +seccion(sid, titulo, estado, error)
        +pendientes() dict
        +estado_global() str
    }
    class RespuestaTruncada {
        <<pdf_struct.py>>
        +dict parcial
    }
    class ErrorFatal {
        <<pdf_struct.py>>
    }
    class Embedder {
        <<doc_rag.py>>
        +OpenAI client
        +str model
        +int batch
        +str q_prefix
        +str d_prefix
        +clave(texto) str
        +documents(textos) ndarray
        +query(texto) ndarray
        +queries(textos) ndarray
    }
    class Store {
        <<doc_rag.py>>
        +Connection db
        +bool fts
        +guardar(doc, registros, vectores) int
        +cache_get(claves) dict
        +cache_put(items)
        +denso(qvec, n, permitidos) list
        +lexico(consulta, n, permitidos) list
        +por_ref(consulta, permitidos) list
        +registros(ids) list
        +vecinos(rec, radio) list
        +filtro(document, kinds) set
        +comprobar_embed_model(model)
    }
    class Sesion {
        <<doc_rag.py>>
        +Path path
        +dict d
        +entrada(ruta) dict
        +conocida(ruta) dict
        +actualizar(ruta, campos)
        +nombres_en_uso() dict
        +guardar()
    }

    Conexion "1" *-- "2" ServiceConfig
    ServiceConfig *-- NetConfig
    TransporteLimitado --> Limitador : usa
    TransporteLimitado ..> NetConfig : política de reintentos
    LLM ..> ServiceConfig : se crea con
    Embedder ..> ServiceConfig : se crea con
    LLM --> TransporteLimitado : vía cliente OpenAI
    Embedder --> TransporteLimitado : vía cliente OpenAI
    LLM ..> RespuestaTruncada : lanza
    Exception <|-- ErrorFatal
    Exception <|-- RespuestaTruncada
    Store ..> Embedder : guarda vectores de
    Sesion ..> Estado : resume
```

Un `Limitador` se crea por cada combinación de servidor y clave de API (`limitador_para`),
y lo comparten todos los clientes que coinciden en ambos.

**Funciones principales** (el resto del código son funciones, no clases):

| Módulo | Función | Qué hace |
|---|---|---|
| `conexion.py` | `cargar(args)` | Configuración efectiva: defaults < fichero < línea de comandos |
| | `crear_cliente(cfg)` | Cliente OpenAI con proxy, TLS, límite y reintentos |
| | `diagnosticar(e)` | Traduce errores de red/TLS/proxy/límite a una pista concreta |
| | `comprobar(conn)` | Llamada real de prueba al chat y a los embeddings |
| `pdf_struct.py` | `procesar(...)` | Pipeline completo de un documento con estado persistente |
| | `preparar_secciones(...)` | Estructura: marcadores, títulos o tamaño; subdivisión, agrupación y troceo |
| | `secciones_por_marcadores(pdf, texto)` | Secciones a partir del índice navegable del PDF |
| | `limpiar_cabeceras(texto)` | Quita cabeceras y pies de página repetidos |
| | `analizar_seccion(...)` | Análisis con caché, reparación, corrección y división |
| | `parse_json` / `reparar_json` | Lectura tolerante del JSON del modelo |
| | `consolidar` / `escribir_todo` | Fusión, verificación de requisitos y escritura de los Markdown |
| `doc_rag.py` | `ingest_archivos(...)` | Sesión de varios ficheros con errores por fichero y parada en fatales |
| | `indexar(...)` | Embeddings por lotes con caché y registro de lotes fallidos |
| | `construir_registros(datos)` | Registros multigranularidad con recuperación contextual |
| | `buscar(...)` | Búsqueda híbrida con reescritura opcional y RRF |
| | `responder(...)` | Contexto ampliado y respuesta citada |
| | `servir_mcp(...)` | Servidor MCP por stdio |

---

## 8. Configuración de la conexión (`conexion.toml`)

`python conexion.py init` crea un `conexion.toml` comentado con todas las opciones.

**Dónde se busca el fichero** (el primero que exista): `--config RUTA`, la variable
`RAG_CONFIG`, `conexion.toml` en el directorio actual, `conexion.toml` junto a los scripts.

**Prioridad de cada valor** (de menor a mayor): valores por defecto → variables de entorno
estándar → fichero → línea de comandos.

**Secretos**: cualquier valor admite `${VARIABLE}` y `${VARIABLE:-por_defecto}`, para no
escribir claves ni contraseñas en el fichero. Una clave mal escrita genera un aviso en lugar
de ignorarse en silencio.

### 8.1 Ejemplo: gateway corporativo con proxy y límite

```toml
[network]
proxy = "http://jose:${PROXY_PASS}@proxy.empresa:8080"
ca_cert = "C:/certs/raiz_corporativa.pem"     # o "system"
requests_per_minute = 28                       # el servidor permite 30

[llm]
base_url = "https://llm-gateway.empresa/v1"
model = "gemma-4"
api_key = "${GW_KEY}"
max_tokens = 8192

[embeddings]
model = "embeddinggemma"     # misma URL y clave que el LLM
batch = 64
```

### 8.2 Sección `[network]` (común a LLM y embeddings)

Se puede sobrescribir solo para un servicio en `[llm.network]` o `[embeddings.network]`.

| Clave | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `proxy` | texto | `RAG_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` o vacío | URL del proxy, con credenciales si hacen falta |
| `ca_cert` | texto | `RAG_CA_CERT` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` o vacío | PEM (o carpeta) con la CA corporativa, que **se añade** a las del sistema y certifi; `"system"` usa el almacén del sistema operativo (requiere `truststore`) |
| `client_cert` | texto | vacío | certificado cliente para TLS mutuo |
| `client_key` | texto | vacío | clave privada del certificado cliente; su contraseña se lee de `RAG_CLIENT_KEY_PASSWORD` |
| `no_proxy` | texto | `RAG_NO_PROXY` / `NO_PROXY` o `localhost,127.0.0.1,::1` | hosts que no pasan por el proxy; admite sufijos (`.intranet.corp`) y `*`; `""` = ninguno |
| `insecure` | bool | `false` | desactiva la verificación TLS; solo para diagnosticar |
| `timeout` | número | `600` | segundos máximos por petición |
| `requests_per_minute` | entero | `0` | peticiones por minuto; `0` = sin límite |
| `tokens_per_minute` | entero | `0` | tokens por minuto aproximados (caracteres/4); `0` = sin límite |
| `adaptive` | bool | `true` | bajar el ritmo al recibir rechazos por límite y recuperarlo después |
| `max_concurrent` | entero | `0` | peticiones simultáneas; `0` = sin límite |
| `max_retries` | entero | `6` | reintentos ante errores 5xx o de conexión |
| `max_backoff` | número | `60` | espera máxima entre reintentos cuando el servidor no indica `Retry-After` |
| `rate_limit_max_wait` | número | `900` | ante 409/429, espera total máxima por petición antes de darla por fallida |
| `retry_statuses` | lista | `[409, 429, 500, 502, 503, 504]` | códigos HTTP que se reintentan |

### 8.3 Sección `[llm]`

| Clave | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `base_url` | texto | `OPENAI_BASE_URL` o `http://localhost:11434/v1` | URL de la API (incluido `/v1`) |
| `model` | texto | `LLM_MODEL` o `gemma4` | nombre del modelo de chat en el servidor |
| `api_key` | texto | `OPENAI_API_KEY` o `local` | clave de la API |
| `temperature` | número | `0.1` | temperatura de generación |
| `max_tokens` | entero | `0` | tope de salida; `0` = el del servidor. Súbelo si las respuestas salen cortadas |

### 8.4 Sección `[embeddings]`

| Clave | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `base_url` | texto | vacío | vacío = la misma URL que el LLM |
| `model` | texto | `EMBED_MODEL` o `embeddinggemma` | modelo de embeddings |
| `api_key` | texto | vacío | vacío = la del LLM, **solo** si también se hereda la URL (nunca se envía la clave de un servidor a otro) |
| `batch` | entero | `32` | textos por petición de embeddings |

Una base de datos queda ligada al modelo de embeddings con que se creó; para cambiarlo hay
que usar otra base de datos (`--db`).

### 8.5 Variables de entorno

| Variable | Uso |
|---|---|
| `RAG_CONFIG` | ruta del fichero de configuración |
| `RAG_DB` | ruta de la base de datos (por defecto `rag.db` junto a los scripts) |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL`, `EMBED_MODEL` | valores por defecto de `[llm]` y `[embeddings]` |
| `RAG_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY` | proxy por defecto |
| `RAG_NO_PROXY`, `NO_PROXY` | excepciones del proxy por defecto |
| `RAG_CA_CERT`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` | CA por defecto |
| `RAG_CLIENT_KEY_PASSWORD` | contraseña de la clave del certificado cliente |

---

## 9. Referencia de la línea de comandos

### 9.1 Opciones de conexión (comunes)

Disponibles en los tres scripts; tienen prioridad sobre `conexion.toml`. En `doc_rag.py` van
antes del comando.

| Opción | Equivale a |
|---|---|
| `--config RUTA` | fichero de configuración |
| `--base-url`, `--model`, `--api-key`, `--temperature` | `[llm]` |
| `--embed-base-url`, `--embed-model`, `--embed-api-key` | `[embeddings]` (no en `pdf_struct.py`) |
| `--proxy`, `--ca-cert`, `--client-cert`, `--client-key`, `--no-proxy`, `--insecure`, `--timeout` | `[network]` |
| `--rpm`, `--tpm`, `--max-concurrent` | `requests_per_minute`, `tokens_per_minute`, `max_concurrent` |
| `--embed-proxy`, `--embed-ca-cert` | `[embeddings.network]` (no en `pdf_struct.py`) |

### 9.2 `conexion.py`

```
python conexion.py {init|show|check} [--force] [opciones de conexión]
```

| Acción | Descripción |
|---|---|
| `init` | crea `conexion.toml` de ejemplo (`--force` para sobrescribir) |
| `show` | configuración efectiva de LLM y embeddings, con secretos ocultos, proxy, TLS y límites |
| `check` | una llamada real al chat y otra a los embeddings; si fallan, explica la causa probable |

### 9.3 `pdf_struct.py` — estructurar sin indexar

```
python pdf_struct.py ENTRADA [opciones]
```

| Opción | Por defecto | Descripción |
|---|---|---|
| `ENTRADA` | | fichero `.pdf`, `.md` o `.txt` |
| `--out` | `<nombre>_estructurado` | carpeta de salida |
| `--lang` | idioma del documento | idioma de resúmenes y descripciones (p. ej. `Spanish`); requisitos y definiciones se copian siempre en el original |
| `--title` | detectado | título del documento |
| `--estructura` | `auto` | `marcadores`, `titulos`, `tamano` o `auto` (ver [5.2](#52-detección-de-la-estructura)) |
| `--heading-regex` | | regex propia de títulos (grupo 1 = número, grupo 2 = título) |
| `--max-chars` | `12000` | tamaño máximo de sección enviada al modelo |
| `--min-chars` | `300` | las secciones más cortas se agrupan con la anterior |
| `--workers` | `1` | secciones analizadas en paralelo |
| `--limit` | `0` | procesar solo las N primeras secciones (pruebas) |
| `--dry-run` | | mostrar secciones, estructura, estimaciones y diagnóstico de títulos sin llamar al modelo |
| `--no-source` | | no incluir el texto original en los Markdown de sección |
| `--no-cache` | | no usar ni guardar la caché de análisis |

Cambiar `--max-chars`, `--min-chars`, `--estructura` o `--heading-regex` cambia las
secciones y, con ello, invalida la caché de análisis de ese documento.

### 9.4 `doc_rag.py` — indexar y consultar

```
python doc_rag.py [--db RUTA] [opciones de conexión] COMANDO [opciones del comando]
```

`--db` (por defecto `RAG_DB` o `rag.db` junto a los scripts) determina también dónde está la
sesión (`<bd>_sesion.json`) y, para ficheros nuevos, dónde se crean las carpetas de salida.

| Comando | Opciones | Descripción |
|---|---|---|
| `ingest ENTRADAS...` | `--out`, `--name` (solo con un fichero), `--out-root`, `--lang`, `--workers`, `--max-chars`, `--min-chars`, `--heading-regex`, `--estructura`, `--force` | procesa e indexa ficheros y carpetas (busca `.pdf`, `.md`, `.markdown`, `.txt` recursivamente). Los ficheros ya conocidos conservan su carpeta y su nombre |
| `retry` | `--all`, `--workers N` | reprocesa los ficheros de la sesión que no están completos, con sus opciones originales (`--workers` las cambia) |
| `status` | `--resumen` | estado de la sesión: ficheros, secciones con error y lotes pendientes |
| `index CARPETA` | `--name` | indexa una carpeta ya generada por `pdf_struct.py` (no se registra en la sesión) |
| `search CONSULTA` | `-k 8`, `--doc`, `--kind`, `--rewrite` | pasajes relevantes; `--kind` filtra por `chunk`, `section_summary`, `requirement`, `definition`, `open_issue` u `overview` |
| `ask PREGUNTA` | `-k 8`, `--doc`, `--no-rewrite` | respuesta redactada solo con los documentos, con citas y lista de fuentes |
| `docs` | | documentos indexados |
| `mcp` | | servidor MCP por stdio (ver [11](#11-servidor-mcp-para-agentes)) |
| `check` | | igual que `conexion.py check` |

**Códigos de salida** de `ingest` y `retry`: `0` todo completo, `1` quedan pendientes o
ficheros con error, `2` sesión detenida por un error fatal, `130` interrumpida con Ctrl+C.

---

## 10. Ficheros generados y base de datos

### 10.1 Carpeta de salida de cada documento

```
<nombre>_estructurado/
├── index.md              resumen global, propósito, alcance, temas e índice de secciones
├── secciones/
│   └── 001-<titulo>.md   resumen, puntos clave, requisitos, definiciones, entidades,
│                         referencias, pendientes y texto original de cada sección
├── requisitos.md         todos los requisitos con su sección y su verificación
├── glosario.md           términos y definiciones, con las secciones donde aparecen
├── entidades.md          entidades agrupadas por tipo
├── pendientes.md         TBD / TBC / puntos abiertos
├── estructura.json       todo lo anterior en JSON (incluido el texto de cada sección)
├── estado.json           estado del procesamiento (ver 5.8)
├── _texto_extraido.md    texto extraído y limpio, tal como se ha troceado
├── _fallos_llm/          respuestas no válidas del modelo, para diagnóstico
└── .cache/               análisis por sección y resumen global ya calculados
```

Los Markdown llevan cabecera YAML (`id`, `title`, `path`, `pages`, `requirements`,
`analysis_status`...) para que otras herramientas puedan filtrarlos.

Ejemplo de `estado.json` de un documento con pendientes (abreviado):

```json
{
  "estado": "incompleto",
  "entrada": {"ruta": "C:/docs/spec.pdf", "hash": "af91ce02…", "bytes": 5242880},
  "modelo": "gemma-4",
  "opciones": {"lang": "", "max_chars": 12000, "min_chars": 2000, "estructura": "auto"},
  "etapas": {
    "extraccion": {"estado": "ok", "caracteres": 2650000, "cabeceras_quitadas": 1840},
    "analisis":   {"estado": "incompleto", "total": 640, "errores": 1},
    "resumen":    {"estado": "ok"},
    "escritura":  {"estado": "ok"},
    "embeddings": {"estado": "incompleto", "total": 9800, "sin_vector": 64},
    "indice":     {"estado": "incompleto", "documento": "spec", "registros": 9736}
  },
  "secciones": {
    "214": {"titulo": "4.3.2 Track deletion", "estado": "error", "fallos": 1,
            "error": "RuntimeError: el modelo no devolvió JSON válido ni tras pedir corrección"}
  },
  "lotes_embeddings": [
    {"lote": 61, "registros": 64, "tipos": ["chunk", "requirement"],
     "secciones": ["301", "302"], "error": "no procesado: límite de peticiones de la API…"}
  ]
}
```

### 10.2 Base de datos (`rag.db`)

Un único fichero SQLite. Junto a él se guarda la sesión (`rag_sesion.json`).

```mermaid
erDiagram
    documents ||--o{ records : contiene
    records ||--|| fts : "indexado en"
    records }o..o| embed_cache : "vector cacheado por texto"

    documents {
        INTEGER id PK
        TEXT name UK "nombre en el índice"
        TEXT title
        TEXT source "fichero original"
        TEXT out_dir "carpeta de salida"
        TEXT overview "resumen global (JSON)"
        TEXT ingested_at
    }
    records {
        INTEGER id PK
        INTEGER doc_id FK
        TEXT kind "overview, section_summary, chunk, requirement, definition, open_issue"
        TEXT section_id
        TEXT section_title
        TEXT path "ruta jerárquica"
        INTEGER page_start
        INTEGER page_end
        TEXT ref "ID de requisito o término"
        INTEGER seq "orden del fragmento en la sección"
        TEXT text "texto que se muestra y se cita"
        TEXT embed_text "texto contextualizado que se embebe"
        BLOB vector "float32 normalizado"
    }
    fts {
        TEXT embed_text "FTS5, BM25"
        TEXT ref
    }
    embed_cache {
        TEXT clave PK "sha256 de modelo + prefijo + texto"
        BLOB vector
    }
    meta {
        TEXT key PK "embed_model"
        TEXT value
    }
```

La búsqueda vectorial es exacta (producto escalar con numpy sobre todos los vectores en
memoria): para documentación, de decenas a cientos de miles de registros, es instantánea y
no requiere librerías nativas. Si el sqlite de tu Python no incluye FTS5, se avisa y se usa
solo la búsqueda vectorial.

---

## 11. Servidor MCP para agentes

`python doc_rag.py mcp` expone la base de conocimiento a cualquier cliente MCP por stdio.

| Herramienta | Peticiones a la API | Descripción |
|---|---|---|
| `list_documents()` | 0 | documentos indexados con su título y resumen |
| `search_docs(query, k=8, document="", kind="")` | 1 embeddings | búsqueda híbrida; devuelve texto, documento, sección, páginas y tipo |
| `get_section(document, section)` | 0 | Markdown completo de una sección |
| `get_requirement(req_id)` | 0 | texto exacto de un requisito por su ID |
| `ask_docs(question, document="")` | 2 chat + 1 embeddings | respuesta redactada con citas |

Ejemplo para OpenCode (`opencode.json` en la raíz del proyecto):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "doc-rag": {
      "type": "local",
      "command": ["python", "C:/ruta/doc_rag.py", "--db", "C:/ruta/rag.db", "mcp"],
      "enabled": true
    }
  }
}
```

Usa rutas absolutas: el agente lanza el servidor desde su propio directorio de trabajo.
Para un agente que ya es un LLM suele ser mejor `search_docs` + `get_section` que
`ask_docs`: razona él con las fuentes y gasta menos cupo.

El control de peticiones es por proceso: si el servidor MCP y una ingesta usan a la vez la
misma clave, cada uno lleva su contador. Deja margen en `requests_per_minute`.

---

## 12. Rendimiento y ajuste

El tiempo de una ingesta lo marca lo más lento de dos cosas: el **cupo de la API**
(peticiones por minuto) o la **latencia del modelo** (lo que tarda en generar cada
respuesta). El log lo indica: cada sección muestra su duración (`(45s, total 1200s)`).

- Si cada sección tarda bastante más que 60/rpm segundos, el límite es la latencia:
  **sube `--workers`**. Con 30 s por sección, 6 hilos se acercan a 12 peticiones/min; el
  limitador impide pasar de `requests_per_minute`.
- Si ya vas al tope del cupo, solo acelera **hacer menos peticiones**.

| Ajuste | Efecto | Coste |
|---|---|---|
| `--workers 4-8` | paralelismo hasta el cupo | ninguno (el limitador protege) |
| `batch = 64-128` en `[embeddings]` | divide las peticiones de embeddings | el servidor debe aceptar lotes grandes |
| `--min-chars 2000` | agrupa subsecciones: menos peticiones de análisis | invalida la caché del documento; con textos largos el modelo puede pasar por alto algún requisito (revisa la verificación) |
| `--max-chars` mayor | menos trozos en secciones largas | respuestas más largas, más cortes por longitud |
| `max_tokens` en `[llm]` | menos respuestas cortadas (y menos divisiones) | ninguno si el modelo lo admite |
| embeddings en local (Ollama + `embeddinggemma`) | todo el cupo del gateway queda para el chat | hay que reindexar en otra base de datos |
| `--estructura marcadores` + `pymupdf` | secciones reales, ni gigantes ni partidas | requiere que el PDF tenga marcadores |

`python pdf_struct.py doc.pdf --dry-run` estima cuántas secciones (y por tanto peticiones
de análisis) habría con distintos `--min-chars` antes de gastar nada.

---

## 13. Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `certificado del servidor no verificado` | proxy que inspecciona TLS con una CA corporativa | `ca_cert = "raiz.pem"` (exportada desde el navegador en Base64) o `ca_cert = "system"` |
| `el proxy pide autenticación` (407) | faltan credenciales | `proxy = "http://usuario:${PROXY_PASS}@proxy:puerto"` |
| `el servidor exige certificado cliente` | TLS mutuo | `client_cert` y `client_key` |
| `no se puede conectar` | servidor interno enviado al proxy, o externo sin proxy | revisa `no_proxy` y `proxy` con `python conexion.py show` |
| `ruta o modelo no encontrado` (404) | falta `/v1` en la URL o el modelo tiene otro nombre | revisa `base_url` y `model` |
| Muchas líneas `HTTP 409 (límite)` / `429` | se supera el cupo, o otro programa usa la misma clave | `requests_per_minute` por debajo del límite real; el primer rechazo muestra qué contestó el servidor |
| `límite de peticiones … durante todo rate_limit_max_wait` | cuota por hora o por día | esperar y `retry`; o subir `rate_limit_max_wait` |
| `el modelo no devolvió JSON válido` | respuesta en prosa, o cortada | revisa `<salida>/_fallos_llm/`: si pone `finish_reason=length`, sube `max_tokens` |
| Secciones de cientos de segundos | respuestas muy largas (secciones enormes) o esperas por límite | `--dry-run`: si hay secciones troceadas en muchas partes, ver la fila siguiente |
| `se trocea en N partes: no se detectan títulos` | títulos con un formato no reconocido | instala `pymupdf` (marcadores), revisa el diagnóstico de `--dry-run` o usa `--heading-regex` |
| `retry` dice que no hay sesión o empieza de cero | la ingesta se hizo con otra base de datos | `python doc_rag.py --db RUTA/rag.db retry` o `RAG_DB` |
| `La base de datos se creó con 'X' y ahora usas 'Y'` | cambio de modelo de embeddings | mismo modelo, u otra `--db` |
| `estructura.json sin texto de sección` (comando `index`) | carpeta generada por una versión antigua | volver a ejecutar `pdf_struct.py` (con la caché es inmediato) |
| `database is locked` | otro proceso escribe en la misma BD | esperar; se reintenta 30 s automáticamente |

---

## 14. Limitaciones conocidas

- **Tablas**: markitdown aplana las tablas (una celda por línea). El contenido se indexa y se
  encuentra, pero sin su estructura de filas y columnas.
- **PDF escaneados**: sin capa de texto no hay nada que extraer; hace falta OCR previo.
- **Páginas de subsecciones**: cuando una sección grande se subdivide por sus títulos
  internos, las subsecciones heredan el rango de páginas de la sección que las contiene.
- **Control de cupo por proceso**: dos procesos con la misma clave no se coordinan entre sí.
- **Verificación de requisitos**: comprueba que el texto existe en la sección, no que el
  modelo haya encontrado todos los requisitos. Para documentos con IDs de formato fijo, una
  extracción por expresión regular sería más exhaustiva.
- **Calidad dependiente del modelo**: resúmenes, entidades y respuestas dependen del modelo
  de chat; las citas permiten comprobar cada afirmación en su fuente.
