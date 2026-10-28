# HephestoLM

Escribe y ejecuta el código de un proyecto que [atlas](../README.md) conoce. atlas sabe:
documentos, DOORS, el grafo y el código, que solo lee. hephesto hace: edita ficheros, compila,
pasa los tests, juzga cada cambio y lleva los planes de `hephesto develop`, en los que un agente
escribe el código encargo a encargo con los tests como juez.

hephesto depende de atlas (`atlaslm`) y lee el mismo `atlas.toml`. atlas nunca importa
hephesto: `atlas serve` no ofrece nunca una herramienta que cambie o ejecute el código.

## Instalación

Desde el repositorio, junto a atlas:

```bash
pip install -e ".[all,dev]" -e "./hephestolm[all]" -c constraints.txt
```

## Comandos

| Comando | Qué hace |
|---|---|
| `hephesto develop PLAN [atlas.toml] [--check] [--resume]` | un agente escribe el código encargo a encargo; lo que rompe un test se deshace y lo que no sabe arreglar va a una persona (un informe) |
| `hephesto hints PLAN [atlas.toml]` | borradores de pistas para los errores del compilador que llegaron sin pista en el último `hephesto develop` del plan |
| `hephesto tests ID\|MODULE [atlas.toml]` | los procedimientos de prueba de DOORS como tests en cada lenguaje, paso a paso, sin modelo |
| `hephesto eval [atlas.toml] [--compare] [--set FILE]` | las evaluaciones de `[agents.eval]`, con sus casos `develop` (que `atlas eval` deja fuera) |

## Módulos

| Fichero | Qué tiene |
|---|---|
| `edit.py` | `CodeTools`: un `CodeReader` de atlas que además cambia ficheros (`edit_file`, `write_file`, `replace_subprogram`, `add_subprogram`) |
| `build.py` | `BuildTools`: un `Toolchain` de atlas que ejecuta `check_code`, `run_test` y `run_program`, con las pistas (`hints.toml`) y las reglas (`rules.toml`) |
| `judge.py` | `JudgeTools`: `run_tests` y `judge_changes`, el juez de `develop` para el agente de un editor |
| `develop.py` | los planes de `hephesto develop` |
| `hints.py` | los borradores de pistas a partir de las llamadas de un `develop` |
| `procedures.py` | los procedimientos de prueba como tests (`hephesto tests`) |
| `evaluate.py` | los casos `develop` de un conjunto de evaluación |
| `cli.py` | la línea de órdenes `hephesto` |

Al importar `hephesto` se registran sus herramientas como plugins `tool` de atlas, para los
agentes de `develop`. Los agentes de código medidos (implementer, fixer, planner, tester) y el
plan de ejemplo están en [examples](examples/).

## Pruebas

Las pruebas de hephesto están en `hephestolm/tests` y usan las fixtures de las de atlas
(`tests/conftest.py`). `make test` pasa las dos.
