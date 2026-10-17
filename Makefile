# Tareas habituales. En Windows sin make: ejecuta los comandos a mano.
PY ?= python3
WIN_PY ?= 3.12          # versión de Python de los puestos Windows, para make wheelhouse-windows
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

test-ollama:    ## pruebas con modelo contra Ollama local (atlas.dev.toml; se omiten si atlas check falla)
	$(PY) -m pytest -q -m model

test-gateway:   ## las mismas contra el gateway de atlas.toml (proxy, CA, cupo); se omiten si atlas check falla
	ATLAS_TEST_CONFIG=atlas.toml $(PY) -m pytest -q -m model

run-test:       ## procesamiento incremental sobre las muestras (sin modelo)
	$(PY) -m atlas.cli run atlas.test.toml

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

wheel:          ## dist/atlaslm-<versión>-py3-none-any.whl y el .tar.gz
	rm -f $(WHEEL)
	$(PY) -m build --outdir dist .

wheelhouse: wheel     ## dist/wheelhouse: atlaslm y sus dependencias para este sistema y este Python, sin red al instalar
	$(PY) -m pip download "$(WHEEL)[all]" -c constraints.txt -d dist/wheelhouse
	cp constraints.txt dist/wheelhouse/

wheelhouse-windows: wheel   ## lo mismo para Windows de 64 bits con Python $(WIN_PY), desde cualquier sistema
	$(PY) -m pip download "$(WHEEL)[all]" -c constraints.txt -d dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY))) \
		--platform win_amd64 --python-version $(strip $(WIN_PY)) --implementation cp --only-binary=:all:
	cp constraints.txt dist/wheelhouse-win_amd64-py$(subst .,,$(strip $(WIN_PY)))/

check-install: wheelhouse   ## instalación limpia desde dist/wheelhouse, como dice docs/instalacion.md
	$(PY) tools/check_install.py dist/wheelhouse

clean:
	rm -rf workspace-dev workspace-test workspace-openrouter examples/samples examples/large .pytest_cache dist build *.egg-info

.PHONY: install samples fake test lint typecheck test-ollama test-gateway run-test large run-dev status sections check serve \
	check-openrouter run-openrouter eval-openrouter compare-openrouter \
	wheel wheelhouse wheelhouse-windows check-install clean
