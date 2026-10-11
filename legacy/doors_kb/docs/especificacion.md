# Especificacion y estado del proyecto

**MCP y base de conocimiento RAG para IBM DOORS Classic**
Consolidacion de requisitos, arquitectura, decisiones de diseno y roadmap.

> Este documento es la **fuente de requisitos** del proyecto (identificadores `RF-*`, `RNF-*`, `CA-*`,
> `R-*`, `ADR-*`). Se versiona junto al codigo para que cualquier cambio de requisitos quede en la
> historia de git. Conversion fiel del original `Especificacion_Proyecto_DOORS_MCP_RAG.docx` (v1.0).
>
> El estado por requisito que aparece mas abajo es el del documento original. El estado **real de esta
> implementacion** esta en [`trazabilidad.md`](trazabilidad.md), que indica ademas en que modulo vive
> cada requisito y que test lo verifica.

---

| Version del documento | 1.0 |
|---|---|
| Fecha | 1 de septiembre de 2026 |
| Estado | Especificacion consolidada / documento vivo |
| Sistema objetivo | IBM Engineering Requirements Management DOORS Classic |
| Tecnologias principales | Python, MCP, pywin32/COM, DXL, SQLite; FTS5 y embeddings planificados |

Objetivo del documento: reunir en una única referencia los requisitos acordados y el conocimiento técnico acumulado durante el desarrollo.

## 1. Resumen ejecutivo

El proyecto construye una capa de acceso para inteligencia artificial sobre IBM DOORS Classic. La primera fase ofrece un servidor MCP local y de solo lectura que utiliza la interfaz Automation de DOORS y DXL para consultar módulos, requisitos, atributos y trazabilidad. La segunda fase desacopla las consultas de IA de DOORS mediante una copia local en SQLite, con sincronizacion segura y deteccion incremental de cambios. Sobre esa copia se añadiran búsqueda textual, embeddings, búsqueda híbrida y RAG.

DOORS se mantiene como fuente de verdad. La base local no pretende reemplazar el repositorio de requisitos, sino proporcionar una representación optimizada para búsqueda, análisis y uso por agentes de IA.

> Estado actual: El MCP avanzado y la base SQLite/sincronizador se encuentran implementados como prototipos funcionales. La capa FTS5, los embeddings, la búsqueda híbrida y el RAG todavía forman parte del roadmap.

### 1.1 Objetivos principales

- Permitir que un agente de IA consulte DOORS Classic mediante herramientas MCP seguras y estructuradas.
- Reducir la dependencia de consultas DXL repetidas mediante una copia local sincronizada.
- Conservar trazabilidad, atributos y contexto suficiente para análisis de impacto y RAG.
- Detectar cambios de forma incremental para regenerar únicamente los embeddings necesarios.
- Mantener una arquitectura modular, comprobable y preparada para trabajar con varios módulos.

### 1.2 Alcance actual

| Área | Alcance |
|---|---|
| MCP directo a DOORS | Consulta de sesión, módulos, atributos, requisitos, búsqueda y enlaces. |
| Sincronización local | Extracción paginada DOORS -> SQLite, hash de contenido, altas/cambios/bajas lógicas e historial. |
| Búsqueda local | Planificada: SQLite FTS5. |
| Embeddings | Planificados: generación incremental e índice vectorial local. |
| RAG | Planificado: búsqueda híbrida y expansión por trazabilidad. |

## 2. Contexto, restricciones y supuestos

La integración está orientada a DOORS Classic de escritorio. La experiencia durante el desarrollo demostró que una ventana abierta manualmente no necesariamente aparece como DOORS.Application en la Running Object Table de Windows. La solución adoptada es crear y conservar una sesión Automation propia mediante Dispatch("DOORS.Application") y abrir explícitamente el módulo requerido en esa sesión.

- El proceso Python debe ejecutarse en Windows, en la misma máquina donde está instalado DOORS Classic.
- La sesión Automation puede ser distinta de una sesión de DOORS abierta manualmente.
- El usuario debe poder iniciar sesión en la ventana Automation creada por Python.
- No se considera compatible la ejecución del cliente COM desde WSL, Dev Containers o Remote SSH.
- VS Code, Python y DOORS deben usar el mismo usuario de Windows y un nivel de privilegios compatible.
- El transporte MCP es stdio; stdout se reserva para el protocolo y los logs deben ir a stderr.
- El acceso del agente a DOORS debe ser de solo lectura.

### 2.1 Principios de diseño

| Principio | Aplicación en el proyecto |
|---|---|
| DOORS como fuente de verdad | SQLite es una réplica de consulta; las modificaciones de requisitos siguen realizándose en DOORS. |
| Solo lectura por defecto | Los módulos se abren con read(...); no se expone una tool de DXL arbitrario. |
| Separación de responsabilidades | COM/DXL, sincronización, persistencia, búsqueda y MCP se separan en capas. |
| Fallos explícitos | Validación estricta de atributos, timeouts y mensajes controlados en lugar de resultados vacíos silenciosos. |
| Respuestas acotadas | Paginación, límites por atributo y límite global del JSON. |
| Incrementalidad | Hash por requisito para saber qué necesita actualizarse o re-embederse. |

## 3. Requisitos funcionales consolidados

Las tablas siguientes convierten las decisiones tomadas durante el desarrollo en requisitos verificables. "Implementado" significa que existe código prototipo; "Parcial" indica que la capacidad existe pero necesita consolidación o convergencia entre componentes; "Planificado" corresponde al roadmap acordado.

### 3.1 Acceso a DOORS y gestión de sesión

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-001 | El sistema deberá integrarse con IBM DOORS Classic mediante la interfaz Automation COM/OLE disponible como DOORS.Application. | Alta | Implementado |
| RF-002 | El sistema deberá crear y conservar una sesión Automation propia de DOORS en lugar de depender de GetActiveObject sobre una ventana abierta manualmente. | Alta | Implementado |
| RF-003 | El sistema deberá permitir que el usuario complete manualmente la autenticación cuando la sesión Automation muestre la ventana de login. | Alta | Implementado |
| RF-004 | El sistema deberá abrir los módulos formales mediante DXL en modo de lectura. | Alta | Implementado |
| RF-005 | El sistema deberá aceptar DOORS_MODULE_PATH como ruta de módulo predeterminada. | Alta | Implementado |
| RF-006 | Las tools del MCP avanzado deberán aceptar module_path opcional para consultar distintos módulos sin reiniciar el proceso. | Media | Implementado |
| RF-007 | El sistema deberá ofrecer una operación de estado que compruebe la sesión y la accesibilidad del módulo seleccionado. | Alta | Implementado |

### 3.2 Esquema y atributos

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-010 | El sistema deberá listar los atributos de objeto definidos en un módulo. | Alta | Implementado |
| RF-011 | La lista de atributos deberá incluir metadatos de tipo, ámbito, flags y valores de enumeración cuando estén disponibles. | Media | Implementado |
| RF-012 | Antes de una consulta de requisitos, el sistema deberá validar que todos los nombres de atributo solicitados existen como atributos de objeto. | Alta | Implementado |
| RF-013 | Cuando un atributo no exista, el sistema deberá devolver un error explícito y sugerencias de nombres próximos cuando sea posible. | Alta | Implementado |
| RF-014 | Object Heading y Object Text deberán ser los atributos de contenido predeterminados para lectura, búsqueda y sincronización. | Alta | Implementado |

### 3.3 Consulta de requisitos

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-020 | El MCP deberá permitir listar requisitos de forma paginada. | Alta | Implementado |
| RF-021 | El MCP deberá permitir obtener un requisito individual mediante Absolute Number y module_path. | Alta | Implementado |
| RF-022 | Las consultas deberán poder recorrer todo el módulo o, cuando se solicite, respetar el display set/vista visible. | Media | Implementado |
| RF-023 | Los objetos eliminados deberán excluirse por defecto, con opción explícita de inclusión en el MCP directo. | Media | Implementado |
| RF-024 | Los objetos internos de tablas nativas deberán excluirse por defecto, con opción explícita de inclusión en el MCP directo. | Media | Implementado |
| RF-025 | Cada requisito devuelto deberá incluir como mínimo Absolute Number, identifier, outline number y atributos solicitados. | Alta | Implementado |

### 3.4 Búsqueda y trazabilidad

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-030 | El MCP deberá buscar requisitos por texto literal sin necesidad de descargar previamente todo el módulo al agente. | Alta | Implementado |
| RF-031 | La búsqueda deberá poder limitarse a un atributo concreto o usar Object Heading y Object Text por defecto. | Alta | Implementado |
| RF-032 | La búsqueda literal deberá permitir configurar sensibilidad a mayúsculas/minúsculas. | Baja | Implementado |
| RF-033 | El MCP deberá admitir búsqueda mediante expresiones regulares DXL. | Media | Implementado |
| RF-034 | La respuesta de búsqueda deberá identificar en qué atributo se encontró la coincidencia y su posición cuando sea posible. | Media | Implementado |
| RF-035 | El MCP deberá obtener enlaces de trazabilidad entrantes, salientes o ambos para un requisito. | Alta | Implementado |
| RF-036 | Para enlaces entrantes completos, el sistema deberá poder cargar en lectura los módulos origen y reportar fallos de carga. | Media | Implementado |
| RF-037 | La versión actual de trazabilidad deberá declarar explícitamente que no incluye enlaces externos OSLC. | Media | Implementado |

### 3.5 Seguridad y control de respuestas

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-040 | El servidor MCP no deberá exponer una herramienta genérica que permita al agente ejecutar DXL arbitrario. | Alta | Implementado |
| RF-041 | Los valores introducidos por Python en DXL deberán escaparse antes de insertarse en el script generado. | Alta | Implementado |
| RF-042 | Las tools de lectura deberán declararse como read-only en sus anotaciones MCP. | Media | Implementado |
| RF-043 | El MCP deberá imponer un límite global configurable al tamaño serializado de las respuestas JSON. | Alta | Implementado |
| RF-044 | Cuando se supere el límite global, el sistema deberá reducir listas/textos o devolver un error de tamaño claro. | Alta | Implementado |

### 3.6 Copia local SQLite y sincronización

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-050 | El sistema deberá mantener una copia local SQLite de los requisitos para desacoplar las consultas de IA de DOORS. | Alta | Implementado |
| RF-051 | La base deberá incluir, como mínimo, tablas para módulos, requisitos, atributos personalizados, enlaces e historial de sincronizaciones. | Alta | Implementado |
| RF-052 | Cada requisito local deberá identificarse de forma única por (module_path, absolute_number). | Alta | Implementado |
| RF-053 | Los atributos personalizados deberán almacenarse de forma normalizada sin exigir una columna SQL específica por atributo de DOORS. | Alta | Implementado |
| RF-054 | El repositorio deberá calcular un hash SHA-256 de los datos relevantes de cada requisito. | Alta | Implementado |
| RF-055 | La sincronización deberá clasificar cada requisito como inserted, updated o unchanged. | Alta | Implementado |
| RF-056 | Un requisito previamente marcado como eliminado que reaparezca deberá reactivarse incluso si su contenido y hash coinciden con los anteriores. | Alta | Implementado |
| RF-057 | La sincronización completa deberá realizarse por páginas para evitar una única ejecución DXL masiva. | Alta | Implementado |
| RF-058 | La paginación de la sincronización deberá usar un cursor basado en el último objeto/Absolute Number visitado, evitando reescanear desde el inicio en cada página. | Alta | Implementado |
| RF-059 | El tamaño de página y el máximo de caracteres por atributo deberán ser configurables. | Media | Implementado |
| RF-060 | La sincronización deberá validar todos los atributos una vez antes de comenzar a escribir datos. | Alta | Implementado |
| RF-061 | Los requisitos ausentes solo podrán marcarse como eliminados tras completar correctamente todas las páginas de una sincronización completa. | Alta | Implementado |
| RF-062 | Cada sincronización deberá registrar estado, tiempos, requisitos vistos, insertados, actualizados, sin cambios, eliminados y error si lo hubiera. | Alta | Implementado |
| RF-063 | El sistema deberá permitir mapear opcionalmente un atributo del proyecto a source_last_modified. | Baja | Implementado |
| RF-064 | La ruta de la base SQLite deberá poder configurarse. | Media | Implementado |

### 3.7 Búsqueda local, embeddings y RAG

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RF-070 | La copia SQLite deberá incorporar búsqueda textual local mediante SQLite FTS5. | Alta | Planificado |
| RF-071 | El sistema deberá generar embeddings de los requisitos para habilitar búsqueda semántica. | Alta | Planificado |
| RF-072 | El texto de embedding deberá incluir al menos identificador, heading, text y un conjunto configurable de atributos relevantes. | Alta | Planificado |
| RF-073 | El sistema deberá asociar cada embedding con el requisito, modelo de embeddings y hash de contenido utilizado. | Alta | Planificado |
| RF-074 | Solo deberán generarse o regenerarse embeddings para requisitos inserted o updated; unchanged no deberá recalcularse. | Alta | Planificado |
| RF-075 | El sistema deberá proporcionar búsqueda vectorial top-k sobre la copia local. | Alta | Planificado |
| RF-076 | El sistema deberá combinar búsqueda lexical y vectorial en una búsqueda híbrida. | Alta | Planificado |
| RF-077 | La búsqueda híbrida deberá poder combinar relevancia semántica con filtros estructurados por módulo y atributos. | Media | Planificado |
| RF-078 | El futuro MCP local deberá exponer herramientas para estadísticas, búsqueda textual, búsqueda semántica, búsqueda híbrida y consulta de requisito local. | Alta | Planificado |
| RF-079 | El RAG deberá poder expandir resultados relevantes utilizando enlaces de trazabilidad para análisis de impacto y contexto relacionado. | Media | Planificado |
| RF-080 | La base local y el índice vectorial deberán considerarse derivados y reconstruibles; DOORS seguirá siendo la fuente de verdad. | Alta | Planificado |

## 4. Requisitos no funcionales

| ID | Requisito | Prioridad | Estado |
|---|---|---|---|
| RNF-001 | Compatibilidad: el acceso Automation deberá ejecutarse en Windows y en la misma máquina donde está instalado DOORS Classic. | Alta | Implementado |
| RNF-002 | Concurrencia: todas las llamadas COM a DOORS deberán serializarse en un único hilo/apartamento COM. | Alta | Implementado |
| RNF-003 | Timeout de sesión: la espera para crear/iniciar la sesión Automation deberá tener timeout configurable (30 s por defecto). | Alta | Implementado |
| RNF-004 | Timeout DXL: las llamadas al worker deberán usar Future.result(timeout=N), con 90 s por defecto para DXL. | Alta | Implementado |
| RNF-005 | Tras un timeout del worker, el proceso deberá marcarse como potencialmente bloqueado y exigir reinicio antes de nuevas llamadas. | Alta | Implementado |
| RNF-006 | El cliente deberá reintentar temporalmente errores COM de DOORS ocupado/retry later. | Media | Implementado |
| RNF-007 | El watchdog interno de DXL deberá ser configurable mediante pragma runLim; en el sincronizador actual se usa 0 por defecto y se conserva el timeout externo de Python. | Media | Implementado |
| RNF-008 | El DXL generado deberá contener saltos de línea reales y disponer de una prueba específica que detecte secuencias \n literales en el preámbulo. | Media | Implementado |
| RNF-009 | Los logs deberán escribirse en stderr para no corromper MCP sobre stdio. | Alta | Implementado |
| RNF-010 | El tamaño duro de respuesta del MCP deberá ser configurable; valor actual por defecto 500000 caracteres y objetivo normal 150000. | Media | Implementado |
| RNF-011 | Las consultas largas deberán usar paginación y límites por atributo para proteger el contexto del agente. | Alta | Implementado |
| RNF-012 | La sincronización local deberá usar transacciones SQLite y WAL para robustez y concurrencia de lectura. | Media | Implementado |
| RNF-013 | La arquitectura deberá permitir probar la lógica de sincronización con una fuente falsa sin abrir DOORS. | Alta | Implementado |
| RNF-014 | El sistema deberá mantener separación clara entre cliente DOORS, servicio de sincronización, repositorio, búsqueda/embeddings y servidor MCP. | Alta | Parcial |
| RNF-015 | La paginación de sincronización deberá tener coste aproximadamente lineal con el número de objetos, evitando el patrón de offset que reescanea el prefijo del módulo. | Alta | Implementado |
| RNF-016 | El sistema deberá ser capaz de reconstruir la base local y los índices derivados a partir de DOORS. | Media | Planificado |
| RNF-017 | Las operaciones del agente no deberán modificar requisitos ni borrar información en DOORS. | Alta | Implementado |

## 5. Arquitectura técnica

### 5.1 Arquitectura objetivo

```text
                       +-------------------------+
                       |     IBM DOORS Classic   |
                       |     fuente de verdad    |
                       +------------+------------+
                                    |
                              COM + DXL lectura
                                    |
                       +------------v------------+
                       |      doors_client       |
                       |  worker COM / timeouts  |
                       +------------+------------+
                                    |
                      JSON / RequirementRecord
                                    |
                       +------------v------------+
                       |      sync_service       |
                       | altas/cambios/borrados  |
                       +------------+------------+
                                    |
                       +------------v------------+
                       |        SQLite           |
                       | reqs / attrs / links    |
                       | sync_runs / hashes      |
                       +-----+-------------+------+
                             |             |
                           FTS5        embeddings
                             |             |
                             +------v------+
                                    |
                          busqueda hibrida / RAG
                                    |
                       +------------v------------+
                       | MCP local para agente   |
                       +-------------------------+
```

El MCP directo contra DOORS sigue siendo útil para comprobar información actual, obtener trazabilidad o consultar un requisito concreto. Sin embargo, la arquitectura objetivo desplaza las búsquedas frecuentes y el RAG a SQLite para reducir latencia, dependencia de la GUI y coste DXL.

### 5.2 Capas y responsabilidades

| Componente | Responsabilidad | Dependencias |
|---|---|---|
| doors_client.py | Crear DOORS.Application, mantener COM en un hilo, abrir módulos, validar atributos y ejecutar DXL. | Windows, pywin32, DOORS Classic |
| sync_service.py | Orquestar páginas, convertir a RequirementRecord, aplicar reglas de sincronización segura y estadísticas. | Interfaz RequirementsSource, repository |
| repository.py | Persistencia SQLite, hashes, atributos, borrado lógico, historial y consultas locales. | sqlite3 |
| MCP avanzado | Exponer tools directas de lectura sobre DOORS con límites y validación. | mcp, pywin32, DXL |
| FTS / embeddings | Indexar la copia local y recuperar contexto relevante. | Planificado |

## 6. Modelo de datos local

La identidad lógica de un requisito es (module_path, absolute_number). El Absolute Number por sí solo no es suficiente porque puede repetirse entre módulos.

| Tabla | Propósito | Campos clave |
|---|---|---|
| modules | Registro de módulos sincronizados y marcas temporales de sincronización. | module_path, last_sync_at, last_full_sync_at |
| requirements | Datos principales del objeto DOORS y hash de contenido. | module_path, absolute_number, identifier, outline_number, heading, text, content_hash, is_deleted |
| requirement_attributes | Atributos arbitrarios del proyecto sin alterar el esquema SQL. | requirement_id, name, value_text |
| links | Trazabilidad entre requisitos/módulos. | source_module, source_abs_no, target_module, target_abs_no, link_module |
| sync_runs | Auditoría y diagnóstico de cada sincronización. | started_at, finished_at, status, counters, error |

### 6.1 Detección incremental de cambios

El hash SHA-256 se calcula sobre una representación canónica de los campos relevantes. La clasificación resultante será la base para actualizar el índice de embeddings.

```text
DOORS                 SQLite                 Acción
hash A        ==       hash A       ->       unchanged -> no embedding
hash B        !=       hash A       ->       updated   -> regenerar embedding
no existe local                        ->       inserted  -> crear embedding
desaparece tras sync completa          ->       deleted   -> retirar del índice
```

## 7. Algoritmo de sincronización

1. Crear la sesión Automation de DOORS en el worker COM.
1. Permitir autenticación manual si es necesaria.
1. Abrir el módulo solicitado en lectura.
1. Validar una sola vez todos los atributos seleccionados.
1. Leer páginas con cursor, empezando por el primer objeto y continuando después del último Absolute Number visitado.
1. Convertir cada objeto a RequirementRecord y hacer upsert dentro de una transacción SQLite.
1. Acumular seen_absolute_numbers y estadísticas inserted/updated/unchanged.
1. Solo cuando se alcanza correctamente el final del módulo, marcar como eliminados los registros locales que no hayan aparecido.
1. Cerrar el sync_run con success; ante cualquier excepción, registrar failed sin ejecutar el marcado de ausentes.

### 7.1 Paginación: decisión actual

La primera implementación utilizaba offset. Cada página volvía a recorrer todos los objetos anteriores, provocando un coste creciente y ventanas DXL Execution Timeout. El sincronizador se migró a un cursor basado en Absolute Number y next(Object), de forma que cada página continúa desde la anterior.

```text
Antes (offset):
página 1 -> recorre 1..25
página 2 -> recorre 1..50
página 3 -> recorre 1..75

Ahora (cursor):
página 1 -> 1..25  -> cursor 25
página 2 -> 26..50 -> cursor 50
página 3 -> 51..75 -> cursor 75
```

> Divergencia a resolver: El sincronizador ya utiliza cursor. El MCP avanzado de consulta directa todavía conserva paginación por offset en list_requirements/search_requirements. Para recorridos grandes conviene migrar también esas tools al modelo de cursor.

## 8. Catálogo de herramientas MCP

| Tool | Finalidad | Estado |
|---|---|---|
| doors_configuration | Mostrar module path por defecto, ProgID, timeouts, límite duro y ejecutable Python. | Implementado |
| start_doors_session | Crear la sesión Automation y guiar al usuario para autenticarse. | Implementado |
| doors_status | Abrir/comprobar el módulo seleccionado en lectura. | Implementado |
| list_object_attributes | Listar atributos, tipos, ámbito y enumeraciones. | Implementado |
| validate_attributes | Validar nombres exactos y devolver metadatos/sugerencias. | Implementado |
| list_requirements | Obtener una página de requisitos. | Implementado |
| search_requirements | Buscar literal o regex en uno o varios atributos. | Implementado |
| get_requirement | Obtener un objeto por Absolute Number. | Implementado |
| get_requirement_links | Obtener trazabilidad entrante/saliente estándar. | Implementado |
| db_search / semantic_search / hybrid_search | Consultar la base local/índice sin tocar DOORS. | Planificado |
| sync_module / sync_status | Sincronizar y consultar frescura desde MCP. | Planificado |

## 9. Configuración y despliegue

### 9.1 Variables relevantes

| Variable | Valor actual por defecto | Uso |
|---|---|---|
| DOORS_MODULE_PATH | vacío | Módulo predeterminado; puede sobreescribirse por tool en MCP avanzado. |
| DOORS_PROG_ID | DOORS.Application | ProgID Automation de DOORS. |
| DOORS_START_TIMEOUT_SECONDS | 30 | Timeout de creación/inicio de sesión Automation. |
| DOORS_DXL_TIMEOUT_SECONDS | 90 | Timeout externo Python para una llamada DXL. |
| DOORS_HARD_MAX_RESPONSE_CHARS | 500000 | Límite duro de respuesta del MCP avanzado. |
| DOORS_DXL_RUN_LIMIT_CYCLES | 0 en sincronizador | Watchdog interno de DXL; 0 desactiva runLim y se conserva el timeout externo. |

### 9.2 Configuración MCP de VS Code

```text
{
  "servers": {
    "doors": {
      "type": "stdio",
      "command": "C:\\proyecto\\.venv\\Scripts\\python.exe",
      "args": ["C:\\proyecto\\doors_mcp_advanced.py"],
      "env": {
        "DOORS_MODULE_PATH": "/Proyecto/Requisitos/Modulo",
        "DOORS_START_TIMEOUT_SECONDS": "30",
        "DOORS_DXL_TIMEOUT_SECONDS": "90",
        "DOORS_HARD_MAX_RESPONSE_CHARS": "500000"
      }
    }
  }
}
```

## 10. Hallazgos y decisiones surgidas durante el desarrollo

| Problema | Causa/impacto | Decisión |
|---|---|---|
| Conexión a sesión existente | GetActiveObject("DOORS.Application") no veía la sesión abierta manualmente. | Crear una sesión Automation propia con Dispatch y trabajar sobre ella. |
| NO_MODULE | La sesión creada por COM no compartía el módulo abierto en la ventana manual. | Abrir explícitamente el módulo por fullName mediante DXL read(...). |
| Atributo mal escrito | objectAttributeText con noError podía ocultar el error devolviendo cadena vacía. | Validación previa estricta contra el esquema de AttrDef. |
| Hilo COM | Las tools MCP pueden ejecutarse desde hilos distintos. | Worker dedicado con CoInitialize y cola serializada. |
| Bloqueo indefinido | future.result() sin timeout podía dejar el MCP bloqueado. | Timeout configurable; cancelar future y marcar worker poisoned. |
| DXL Execution Timeout | La paginación offset reescaneaba el módulo y agotaba el watchdog DXL. | Paginación con cursor + pragma runLim configurable + timeout Python externo. |
| Error de pragma | El preámbulo generaba \n literal, rompiendo el parseo DXL. | Usar salto real \n y test_dxl_generation.py para regresión. |
| Respuestas enormes | limit x max chars podía saturar el contexto del agente. | Límite global de JSON y truncado controlado. |

## 11. Estrategia de pruebas y evidencias actuales

- Prueba de repositorio SQLite: primera ejecución inserted; segunda unchanged; modificación produce updated.
- FakeDoorsSource: simula alta, modificación, desaparición y reaparición sin necesitar una instalación real de DOORS.
- Regla de seguridad de borrados: una sincronización incompleta no ejecuta mark_missing_as_deleted.
- Prueba de generación DXL: verifica que el preámbulo pragma runLim termina con un salto de línea real y no contiene la secuencia \n literal.
- Validación sintáctica Python de los archivos generados mediante py_compile/ast.
- Pruebas manuales en DOORS: conexión Automation, apertura del módulo por fullName y detección de errores del intérprete DXL.

### 11.1 Criterios de aceptación recomendados para el hito de sincronización

| ID | Criterio |
|---|---|
| CA-001 | Una sincronización completa de un módulo real termina sin ventanas DXL Execution Timeout. |
| CA-002 | Dos sincronizaciones consecutivas sin cambios producen 0 inserted, 0 updated y N unchanged. |
| CA-003 | Una modificación de texto/atributo produce exactamente updated para ese objeto. |
| CA-004 | Un requisito nuevo produce inserted y uno eliminado se marca localmente solo tras una sync completa. |
| CA-005 | Un fallo provocado a mitad de sincronización deja sync_run=failed y no marca como eliminados los objetos no recorridos. |
| CA-006 | La duración por página permanece aproximadamente estable a medida que avanza el módulo. |
| CA-007 | Un atributo inexistente se rechaza antes de comenzar la extracción. |

## 12. Limitaciones y riesgos conocidos

| ID | Riesgo | Mitigación | Nivel |
|---|---|---|---|
| R-001 | La sesión Automation requiere interacción de login y posibles diálogos de licencia. | Mitigar con instrucciones claras, timeouts y detección de worker bloqueado. | Media |
| R-002 | Los enlaces entrantes dependen de cargar módulos origen; pueden ser costosos o fallar por permisos. | Reportar fallos y sincronizar trazabilidad por etapas. | Media |
| R-003 | Los enlaces externos OSLC no están cubiertos por la tool actual. | Tratar como extensión futura si el proyecto los necesita. | Baja/Media |
| R-004 | El MCP directo todavía usa offset para list/search y puede ser ineficiente en módulos grandes. | Migrar a cursor o favorecer la base local para búsquedas masivas. | Media |
| R-005 | El hash depende de los atributos elegidos para sincronización. | Definir un perfil de atributos estable por módulo antes de crear embeddings. | Alta |
| R-006 | Los embeddings pueden introducir dependencia de modelo/versión y coste de reindexación. | Guardar embedding_model + content_hash y permitir reconstrucción. | Media |
| R-007 | Un índice local desactualizado puede responder con información antigua. | Exponer last_sync/sync_status y frescura en respuestas del agente. | Alta |

## 13. Estado del desarrollo y roadmap

| Hito | Contenido | Estado |
|---|---|---|
| H0 - MCP básico | Conexión Automation, módulo explícito, lectura de requisitos. | Completado |
| H1 - MCP avanzado | Validación, búsqueda, tipos, enlaces, multi-módulo, límites, timeouts. | Prototipo completado |
| H2 - Persistencia SQLite | Modelo local, atributos, hashes, sync_runs. | Completado |
| H3 - Sync DOORS -> SQLite | Cliente separado, sync segura, cursor, fixes DXL. | En validación con DOORS real |
| H4 - FTS5 | Índice lexical local y API de búsqueda. | Siguiente paso |
| H5 - Embeddings | Generación incremental + almacenamiento/índice vectorial. | Planificado |
| H6 - Búsqueda híbrida | RRF/ponderación lexical + vectorial + filtros. | Planificado |
| H7 - MCP sobre Knowledge Base | Tools locales, sync_status y selección automática DOORS vs cache. | Planificado |
| H8 - Graph-RAG | Sincronizar enlaces y expandir contexto por trazabilidad. | Planificado |

### 13.1 Próximo paso recomendado

Antes de añadir embeddings, conviene cerrar el hito de sincronización real y después implementar SQLite FTS5. FTS proporciona una línea base lexical rápida para identificadores, términos exactos y vocabulario técnico. Cuando exista esa línea base, se podrá medir de forma clara qué aporta la búsqueda semántica y diseñar una estrategia híbrida.

```text
Paso 3: FTS5
    SQLite -> requirements_fts -> textual_search("TCP timeout")

Paso 4: embeddings
    inserted/updated -> embed -> vector index

Paso 5: híbrido
    FTS + vector -> ranking fusion -> filtros estructurados

Paso 6: RAG
    top resultados -> expandir links -> contexto -> agente
```

## 14. Registro resumido de decisiones de arquitectura

| ID | Decisión | Motivación |
|---|---|---|
| ADR-001 | Usar DOORS Automation + DXL | Es la interfaz disponible para DOORS Classic de escritorio y permite ejecutar consultas dentro de una sesión controlada. |
| ADR-002 | Crear sesión Automation propia | La sesión manual no estaba disponible de forma fiable mediante GetActiveObject. |
| ADR-003 | Solo lectura y sin run_dxl genérico | Reduce el riesgo de que un agente modifique la base de requisitos. |
| ADR-004 | SQLite como réplica local | Sencillo, portable, transaccional y suficiente para comenzar FTS/RAG sin infraestructura pesada. |
| ADR-005 | Hash de contenido | Permite sincronización incremental y evita recalcular embeddings sin necesidad. |
| ADR-006 | Cursor para sincronización | Evita reescaneos por offset y reduce la probabilidad de timeout DXL. |
| ADR-007 | FTS antes de embeddings | Permite tener una línea base lexical y separar problemas de datos de problemas vectoriales. |
| ADR-008 | Separar cliente, sync, repository y MCP | Mejora testabilidad y facilita sustituir la fuente o el índice en el futuro. |

## 15. Base documental consolidada

Este documento consolida el conocimiento generado durante el desarrollo, incluyendo las siguientes piezas internas:

- doors_mcp_advanced.py - versión MCP con validación estricta, búsqueda, tipos de atributos, trazabilidad, multi-módulo, límites globales y timeouts.
- README_DOORS_MCP.md - instrucciones de funcionamiento y uso del MCP.
- README_STEP1.md / repository.py / models.py - diseño de la copia SQLite y detección de cambios mediante hash.
- README_STEP2.md / doors_client.py / sync_service.py / sync_doors.py - sincronización real DOORS -> SQLite y seguridad ante fallos parciales.
- README_TIMEOUT_FIX.md - análisis del DXL Execution Timeout y migración de paginación a cursor.
- README_DXL_PARSE_FIX.md / test_dxl_generation.py - corrección del salto de línea en pragma runLim y prueba de regresión.

> Documento vivo: Al completar FTS5, embeddings y búsqueda híbrida, esta especificación debería actualizar el estado de RF-070 a RF-080 y añadir decisiones sobre modelo de embeddings, estrategia de chunking, índice vectorial, ranking híbrido y política de frescura.

## Anexo A. Resumen de comandos de desarrollo

```text
# Crear entorno
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# Dependencias DOORS/MCP
python -m pip install pywin32 "mcp[cli]>=2,<3"

# Sincronización real (recomendación actual de prueba)
$env:DOORS_DXL_RUN_LIMIT_CYCLES = "0"
$env:DOORS_DXL_TIMEOUT_SECONDS = "90"
python .\sync_doors.py `
  --module "/Proyecto/Requisitos/Requisitos del sistema" `
  --page-size 25 `
  --max-attribute-chars 20000

# Tests sin DOORS
python .\test_sync_fake.py
python .\test_dxl_generation.py
```

## Anexo B. Definición de terminado para la fase RAG

- La copia local se sincroniza de forma reproducible y reporta su frescura.
- FTS5 resuelve búsquedas exactas, identificadores y términos técnicos con latencia local baja.
- Los embeddings se generan solo para requisitos nuevos o modificados y registran modelo/hash.
- La búsqueda semántica devuelve resultados relevantes en preguntas conceptuales donde no coinciden las palabras exactas.
- La búsqueda híbrida supera o iguala FTS y vector por separado en un conjunto de consultas de prueba.
- El agente puede recuperar requisito, atributos y trazabilidad desde la Knowledge Base y verificar en DOORS cuando sea necesario.
- Existe una política clara de sincronización y el agente informa si la copia local está desactualizada.

