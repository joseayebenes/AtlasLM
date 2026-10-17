# API pública

Lo que se puede usar de atlas como librería sin leer su código. Cada módulo declara en
`__all__` lo que exporta; de eso, lo que está en esta tabla es lo que se promete: no cambia
sin anotarse en el [CHANGELOG](../CHANGELOG.md) («Al actualizar»). Mientras la versión sea
0.x, puede cambiar entre versiones menores, nunca dentro de una. El resto de `__all__` se
puede importar, pero cambia cuando haga falta.

Los ejemplos de [examples/library](../examples/library) usan solo nombres de esta tabla
(`tests/test_examples.py` lo comprueba, y que cada nombre está en el `__all__` de su
módulo).

| Módulo | Nombres | Para qué |
|---|---|---|
| `atlas` | `load`, `Config`, `ConfigError`, `__version__` | cargar y validar `atlas.toml`; el error de toda configuración que no vale |
| `atlas.config` | `load_config` | lo mismo que `atlas.load`, con el nombre que usan los README |
| `atlas.pipeline` | `Step`, `Runner`, `Ledger`, `FatalError`, `atomic_write`, `sha256_file` | el motor incremental con pasos propios ([README](../atlas/pipeline/README.md)) |
| `atlas.plugins` | `CodeAnalyzer`, `RequirementsAdapter`, `DocumentExtractor`, `Tool`, `CodeUnit`, `Requirement`, `Link`, `register` | escribir un plugin ([Añadir o quitar piezas](../README.md#añadir-o-quitar-piezas)) |
| `atlas.store` | `Store` | `atlas.db`: requisitos, enlaces, unidades de código y documentos |
| `atlas.docs` | `DocsOptions`, `preview`, `process_document` | un documento a Markdown por secciones ([README](../atlas/docs/README.md)) |
| `atlas.index` | `Index`, `cite` | buscar y responder con citas ([README](../atlas/index/README.md)) |
| `atlas.graph` | `KnowledgeGraph` | las consultas de trazabilidad ([README](../atlas/graph/README.md)) |
| `atlas.code` | `AdaAnalyzer`, `BuildTools`, `CodeTools` | analizar Ada; leer, editar y compilar código ([README](../atlas/code/README.md)) |
| `atlas.agents` | `run_agent`, `build_context` | un agente de Markdown; el paquete de contexto de un requisito ([README](../atlas/agents/README.md)) |
| `atlas.mcp` | `create_server` | el servidor MCP sobre una configuración ([README](../atlas/mcp/README.md)) |
| `atlas.cli` | `run` | la ingesta entera de una configuración, como `atlas run` |

## Lo que se promete además

- Una configuración que no puede funcionar (una clave desconocida, un certificado que no
  existe, un paquete que falta) lanza `ConfigError` con un texto que dice qué arreglar.
  Ninguna función de la librería termina el proceso (`sys.exit`): eso lo hace solo la
  línea de comandos.
- Los avisos van por `logging`, con loggers bajo `atlas` (`atlas.llm` para los rechazos
  por límite y los reintentos); los gobierna `[project] log_level` en la línea de comandos
  y la configuración de `logging` del programa que use la librería. Solo las acciones de la
  línea de comandos escriben en la salida, y siempre en UTF-8 (también en una consola de
  Windows o con la salida redirigida a un fichero).
- Los Markdown y JSON de cada documento, y todo lo que se escribe con `atomic_write`, son
  UTF-8 con saltos de línea `\n` en cualquier sistema: un workspace copiado de un sistema a
  otro no se reprocesa.

## Uso

```python
from atlas import load
from atlas.agents import run_agent
from atlas.cli import run
from atlas.graph import KnowledgeGraph
from atlas.index import Index, cite

cfg = load("atlas.dev.toml")
run(cfg)                                              # la ingesta (incremental)
g = KnowledgeGraph.from_config(cfg)
idx = Index.from_config(cfg)
print([cite(r) for r in idx.search("track deletion")])
print(run_agent("assistant", "Which tests verify SRS-0004?", cfg)["answer"])
```

Los ejemplos completos, que se ejecutan en las pruebas: `examples/library/pipeline.py`
(un paso propio), `documents.py`, `traceability.py` y `search.py`.
