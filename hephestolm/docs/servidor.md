# hephesto serve: el compilador, los tests y el juez para el editor

`hephesto serve atlas.toml` es el servidor MCP de hephesto: el de atlas (`atlas.mcp.create_server`) con `check_code`, `run_test`, `run_tests` y `judge_changes` (y `run_program` con `[sources.code] program_command`); con `--edit`, también las herramientas de ficheros (`list_files`, `grep`, `read_file`, `outline`, `read_subprogram`, `edit_file`, `write_file`, `replace_subprogram` y `add_subprogram`). Va junto al de atlas, que da lo que se sabe del proyecto: `hephesto init --vscode` lo añade a `.vscode/mcp.json` ([atlas en VS Code](../../docs/vscode.md)).

## El juez de develop en el editor

`hephesto develop` juzga cada cambio de sus agentes sin modelo: qué compila, qué tests pasan y
hasta dónde llegan los demás, y qué ha empeorado. Desde OpenCode o VS Code el agente del
editor cambia el código con sus herramientas, y estas dos le dan ese juicio. Ejecutan código
(los tests y el compilador), así que solo se ofrecen si se piden en `[mcp] tools`, y el
servidor las marca como destructivas para que el editor pida confirmación:

```toml
[mcp]
tools = ["search", "get_requirement", "trace", "build_context", "read_file", "check_code",
         "run_tests", "judge_changes"]
```

- `run_tests(plan)` ejecuta los tests de un plan de `hephesto develop` (`plan`: su TOML, relativo
  a la carpeta del servidor) o, sin plan, los que escribe `hephesto tests` (los `path` de
  `[tests.languages]`). Por test: si pasa y, si no, hasta qué paso llegó (con `progress` del
  plan), lo que dijo al fallar y el sitio del código que cita.
- `judge_changes(paths)` dice qué ficheros de las raíces del código cambiaron (frente al
  último commit si la carpeta es un repositorio de git; si no, desde la llamada anterior),
  si compilan (`check_code` sobre ellos, o sobre `paths` si se dan) y qué tests avanzaron o
  retrocedieron desde la última llamada a una de las dos. Los tests son los del último plan
  que recibió `run_tests`, o los de `[tests]`.

Lo que recuerdan entre llamadas (el hash de cada fichero, hasta dónde llegó cada test y el
último plan) está en `<workspace>/judge.json`. No cambian código: deshacer lo que hace
retroceder un test es cosa del agente del editor, al que las instrucciones del servidor se lo
piden. Para que sea `hephesto develop` quien encargue, juzgue y deshaga con un agente de fuera,
ver `agent_command` en [atlas/agents](../../atlas/agents/README.md#un-agente-de-fuera-bajo-el-mismo-juez).
