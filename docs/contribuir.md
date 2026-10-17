# Contribuir

Cómo se trabaja en el repositorio: ramas, mensajes de commit, pruebas y la lista de cada
versión. El idioma de cada cosa (código en inglés, documentación en español) está en la
sección [Idioma](../README.md#idioma) del README.

## Ramas (gitflow, a mano)

| Rama | Sale de | Se fusiona en | Para qué |
|---|---|---|---|
| `main` | — | — | lo publicado; cada versión con su etiqueta `vX.Y.Z` |
| `develop` | `main` | — | lo que entrará en la próxima versión |
| `feature/<nombre>` | `develop` | `develop` | una funcionalidad, una mejora o un arreglo de lo no publicado |
| `release/<x.y>` | `develop` | `main` (con etiqueta) y `develop` | cerrar una versión: número, CHANGELOG, cifras |
| `hotfix/<nombre>` | `main` | `main` y `develop` | un arreglo urgente de lo publicado |

- `git checkout -b feature/<nombre> develop`; al terminar, `git merge --no-ff` en `develop`,
  en local y sin PR. El mensaje del merge dice qué trae: `Merge feature/<nombre> en
  develop: <resumen>`.
- Un commit por cada parte lógica, y cada commit deja las pruebas en verde.
- `develop` y `main` suelen estar abiertas en otro worktree (`git worktree list`): el merge se
  hace allí, con `git -C <worktree> merge --no-ff ...`, tras comprobar que está limpio.
- Se sube (`git push`) solo lo que se decide publicar.

## Mensajes de commit

`tipo(ámbito): resumen`, en español, en minúscula y sin punto final. El cuerpo, si hace
falta, explica el porqué y lo que se midió.

- **Tipos**: `feat` (algo nuevo), `fix` (un error), `docs`, `test`, `perf`, `refactor`,
  `build` (dependencias, empaquetado) y `chore` (la versión, limpieza).
- **Ámbitos**: el módulo o la pieza que cambia. Los usados: `code`, `agents`, `index`,
  `graph`, `tools`, `llm`, `mcp`, `docs`, y también `pipeline`, `cli`, `plugins`, `doors`,
  `config`, `release` y `plan` (`legacy/PLAN.md`).

```
feat(index): como mucho 3 resultados por documento, [search] y --k, --kind y --document
fix(mcp): atlas serve dice qué variables del entorno le faltan (el proxy, la clave)
```

## Pruebas

```bash
python examples/make_samples.py     # una vez: los datos de ejemplo
make test                           # python -m pytest -q: la suite entera, sin modelo
make test-fast                      # sin las marcadas slow (ejemplos, proyecto grande, kill -9, esperas)
make test-ollama                    # las de agentes contra Ollama (se omiten si no está en marcha)
```

- `pytest` imprime al final las 15 pruebas más lentas (`--durations=15` en
  `pyproject.toml`). Una prueba nueva que arranque procesos, espere o tarde varios segundos
  se marca `@pytest.mark.slow` (los marcadores están registrados en `pyproject.toml`); las de
  Ollama también lo son.
- Las pruebas no usan un modelo de verdad: van contra el servidor falso
  (`tests/fakes/fake_api.py`, el fixture `fake`), uno por sesión. Cada prueba empieza con
  su fichero de control vacío y los contadores a cero (`POST /reset`).
- Antes de fusionar, la suite entera. Para la calidad real (análisis, búsqueda, agentes) están
  los otros [niveles de modelo](../README.md#niveles-de-modelo): Ollama o LM Studio,
  OpenRouter y el gateway.
- Cada clave nueva de `atlas.toml` va en el esquema de `atlas/config.py` y en
  [configuracion.md](configuracion.md); cada cambio que se note, en «Sin publicar» del
  [CHANGELOG](../CHANGELOG.md).

## Lista de una versión

En una rama `release/<x.y>` desde `develop`:

1. `__version__` en `atlas/__init__.py` (el `Makefile` y el wheel la leen de ahí) y donde la
   citen el README y la [guía de instalación](instalacion.md).
2. CHANGELOG: «Sin publicar» pasa a `[x.y.z] — AAAA-MM-DD`, con un párrafo de lo que trae y
   «Al actualizar» con todo lo que obligue a tocar la configuración o reprocese datos.
3. Las cifras del README: el número de pruebas que imprime `pytest` y el tiempo de la suite.
4. `constraints.txt`: si cambiaron las dependencias, se regenera con `tools/constraints.py`
   ([Versiones fijadas](instalacion.md#versiones-fijadas)); si no, al menos su cabecera dice
   la versión nueva.
5. `make check-install` (instalación limpia desde el wheelhouse) y `make wheelhouse-windows`
   (lo que se entrega a los puestos Windows).
6. Merge `--no-ff` en `main`, etiqueta `git tag -a vX.Y.Z -m "versión X.Y.Z"` y merge de
   vuelta en `develop`.
7. Limpieza: borrar las ramas fusionadas (`git branch --merged develop`, salvo `main` y
   `develop`) y los worktrees cerrados (`git worktree list`, `git worktree remove`,
   `git worktree prune`).
