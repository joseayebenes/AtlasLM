# Workspaces de versiones publicadas

`atlas-<versión>.db` es el `atlas.db` que deja esa versión de atlas tras una ejecución
sobre sus muestras (`atlas.test.toml` sin los PDF, contra el servidor falso), compactado.
`test_db.py` abre cada uno con el atlas de ahora: debe migrarlo a `SCHEMA_VERSION`, dejar
la copia `atlas.db.bak-<n>` con los mismos datos, y `atlas status` y `atlas trace` deben
funcionar sobre él.

| Fichero | Versión del esquema |
|---|---|
| `atlas-0.1.0.db` | 0 (sin versión; la 0.2.0 tiene las mismas tablas) |

No se regeneran: son el pasado que atlas tiene que seguir abriendo. Solo cuando cambia
`SCHEMA_VERSION` (en `atlas/db.py`) se **añade** el de la última versión publicada antes del
cambio, para probar también la migración nueva desde un workspace real:

```bash
python tests/fixtures/make_workspace.py v0.3.0
```

El script saca la etiqueta en un `git worktree` temporal, genera sus muestras, arranca su
servidor falso, ejecuta `atlas run` y guarda el resultado en `tests/fixtures/atlas-0.3.0.db`
(unos 260 KB). Añade su fila a la tabla de arriba.
