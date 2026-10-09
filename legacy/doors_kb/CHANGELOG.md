# Registro de cambios

El proyecto avanza por hitos. Cada entrada resume que aporta el hito y a que requisitos responde.

## [0.4.0] — Hitos H4 a H7: busqueda local

- Indice lexical FTS5 mantenido de forma incremental desde la clasificacion por hash, con
  reconstruccion sin consultar DOORS (RF-070, RNF-016).
- Embeddings incrementales contra una API compatible con OpenAI, con dos hashes por
  embedding para detectar tambien los cambios de perfil de atributos (RF-071 a RF-074).
- Busqueda vectorial con filtros estructurados aplicados antes de puntuar, y busqueda
  hibrida por fusion de rankings RRF (RF-075 a RF-077).
- Servidor MCP `doors-kb` sobre la copia local; todas sus respuestas informan de la frescura
  (RF-078, riesgo R-007).
- Recorrido opcional por display set en las consultas, prohibido al sincronizar (RF-022,
  ADR-012).
- Comandos `doors-embed` y `doors-search`.

## [0.3.0] — Hito H3: sincronizacion DOORS -> SQLite

- Servicio de sincronizacion con paginacion por cursor y marcado seguro de ausentes (RF-055..062).
- Cliente DOORS por Automation COM con worker de hilo unico, timeouts y deteccion de bloqueo
  (RF-001..007, RNF-002..006).
- Generador de DXL con escapado estricto y preambulo `pragma runLim` correcto (RF-041, RNF-008).
- CLI `doors-sync`.

## [0.2.0] — Hito H2: persistencia SQLite

- Esquema local de modulos, requisitos, atributos, enlaces e historial de sincronizaciones (RF-050..053).
- Hash SHA-256 de contenido y clasificacion `inserted` / `updated` / `unchanged` (RF-054, RF-055).
- Reactivacion de requisitos borrados que reaparecen (RF-056).

## [0.1.0] — Hitos H0 y H1: acceso y MCP directo

- Configuracion por variables de entorno (seccion 9.1 de la especificacion).
- Modelo de dominio y protocolo `RequirementsSource` con fuente falsa para pruebas (RNF-013).
- Servidor MCP de solo lectura con validacion de atributos, busqueda, trazabilidad y limites de
  respuesta (RF-010..044).
