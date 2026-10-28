# Tareas habituales. En Windows sin make: ejecuta los comandos a mano.
PY ?= python3
WIN_PY ?= 3.12          # versión de Python de los puestos Windows, para make wheelhouse-windows
CONFIG ?= atlas.test.toml   # configuración de make demo
EXTRAS ?= all           # extras del wheelhouse; all,dev añade pytest para las pruebas en el puesto
VERSION := $(shell $(PY) -c "import re; print(re.search(r'__version__ = \"(.+)\"', open('atlas/__init__.py').read())[1])")
WHEEL := dist/atlaslm-$(VERSION)-py3-none-any.whl

install:        ## atlas editable con todo (ADK incluido) y las versiones probadas
	$(PY) -m pip install -e ".[all,dev]" -c constraints.txt

samples:        ## genera examples/samples/ (PDFs, Ada, doors.sqlite)
	$(PY) examples/make_samples.py

fake:           ## servidor OpenAI falso en :8770 para atlas.test.toml
	$(PY) tests/fakes/fake_api.py --port 8770 --control faults.json

test:           ## pruebas automáticas (sin modelo)
	$(PY) -m pytest -q

lint:           ## ruff sobre todo el repositorio (la configuración, en pyproject.toml)
	$(PY) -m ruff check .

typecheck:      ## mypy sobre atlas; los módulos aún sin limpiar, en [[tool.mypy.overrides]]
	$(PY) -m mypy atlas

test-fast:      ## las mismas sin las marcadas slow (ejemplos, proyecto grande, kill -9, esperas, compilador)
	$(PY) -m pytest -q -m "not slow"

test-ollama:    ## pruebas con modelo contra Ollama local (atlas.dev.toml; se omiten si atlas check falla)
	$(PY) -m pytest -q -m model

test-gateway:   ## las mismas contra el gateway de atlas.toml (proxy, CA, cupo); se omiten si atlas check falla
	ATLAS_TEST_CONFIG=atlas.toml $(PY) -m pytest -q -m model

run-test:       ## procesamiento incremental sobre las muestras (sin modelo)
	$(PY) -m atlas.cli run atlas.test.toml

demo:           ## recorrido de las muestras con CONFIG (atlas.test.toml arranca el servidor falso): cada acción, su salida y su tiempo
	$(PY) examples/walkthrough.py $(CONFIG)

large:          ## proyecto sintético grande (examples/large) y tres ejecuciones medidas con el servidor falso
	$(PY) examples/large_project.py

run-dev:        ## procesamiento con Ollama local
	$(PY) -m atlas.cli run atlas.dev.toml

status:
	$(PY) -m atlas.cli status atlas.dev.toml

sections:       ## secciones de cada documento de atlas.dev.toml, sin llamar al modelo
	$(PY) -m atlas.cli sections atlas.dev.toml

check:          ## prueba de conexión con el modelo local
	$(PY) -m atlas.cli check atlas.dev.toml

serve:          ## servidor MCP (stdio) de atlas.dev.toml; normalmente lo arranca el editor
	$(PY) -m atlas.cli serve atlas.dev.toml

check-openrouter:   ## conexión con OpenRouter (OPENROUTER_API_KEY) y con los embeddings de Ollama
	$(PY) -m atlas.cli check atlas.openrouter.toml

run-openrouter:     ## las muestras con un modelo de OpenRouter: make run-openrouter ATLAS_MODEL=qwen/qwen3.6-35b-a3b
	$(PY) -m atlas.cli run atlas.openrouter.toml

eval-openrouter:    ## atlas run y atlas eval con un modelo de OpenRouter; informes en workspace-openrouter/<modelo>
	$(PY) -m atlas.cli run atlas.openrouter.toml || test $$? -eq 1      # 1: alguna unidad falló; 2: error fatal; 3: límite (docs/instalacion.md)
	$(PY) -m atlas.cli eval atlas.openrouter.toml

compare-openrouter: ## lo mismo con cada modelo de MODELS="a b c" y una tabla que los compara
	$(PY) tools/compare_models.py $(MODELS)

EVAL_CONFIG ?= atlas.openrouter.toml
eval-develop:       ## el banco de encargos de hephesto develop (hephestolm/examples/develop.yaml) con EVAL_CONFIG; atlas.test.toml y make fake, sin modelo
	$(PY) -m hephesto.cli eval $(EVAL_CONFIG) --set hephestolm/examples/develop.yaml

wheel:          ## dist/atlaslm-<versión>-py3-none-any.whl y el .tar.gz
	rm -f $(WHEEL)
	$(PY) -m build --outdir dist .

wheelhouse: wheel     ## dist/wheelhouse: atlaslm y sus dependencias para este sistema y este Python, sin red al instalar
	$(PY) -m pip download "$(WHEEL)[$(strip $(EXTRAS))]" -c constraints.txt -d dist/wheelhouse
	cp constraints.txt dist/wheelhouse/
	$(PY) tools/licenses.py --wheelhouse dist/wheelhouse --out dist/wheelhouse

wheelhouse-windows: wheel   ## lo mismo para Windows de 64 bits con Python $(WIN_PY), desde cualquier sistema
	$(PY) -m pip download "$(WHEEL)[$(strip $(EXTRAS))]" -c constraints.txt -d dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY))) \
		--platform win_amd64 --python-version $(strip $(WIN_PY)) --implementation cp --only-binary=:all:
	cp constraints.txt dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY)))/
	$(PY) tools/licenses.py --wheelhouse dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY))) \
		--out dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY)))

licenses:       ## dist/THIRD_PARTY_NOTICES.md y dist/sbom.cdx.json: licencia de cada dependencia de atlaslm[all] instalada
	$(PY) tools/licenses.py --out dist

audit:          ## vulnerabilidades conocidas en las versiones de constraints.txt, las de todos los Python (pip-audit, con red)
	@rm -rf dist/audit && mkdir -p dist/audit
	@# pip-audit evalúa los marcadores con este Python: sin ellos, y un fichero por versión de un mismo paquete
	awk '/^[A-Za-z0-9]/ { sub(/ *;.*/, ""); split($$0, p, "=="); n = ++seen[tolower(p[1])]; \
		print > ("dist/audit/pins-" n ".txt") }' constraints.txt
	for f in dist/audit/pins-*.txt; do \
		$(PY) -m pip_audit -r $$f --no-deps --disable-pip --progress-spinner off || exit 1; done

check-install: wheelhouse   ## instalación limpia desde dist/wheelhouse, como dice docs/instalacion.md
	$(PY) tools/check_install.py dist/wheelhouse

clean:
	rm -rf workspace-dev workspace-test workspace-openrouter examples/samples examples/large .pytest_cache dist build *.egg-info

.PHONY: install samples fake test test-fast lint typecheck test-ollama test-gateway run-test demo large run-dev status sections check serve \
	check-openrouter run-openrouter eval-openrouter compare-openrouter eval-develop \
	wheel wheelhouse wheelhouse-windows licenses audit check-install clean
