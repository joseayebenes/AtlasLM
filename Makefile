# Tareas habituales. En Windows sin make: ejecuta los comandos a mano.
PY ?= python3

install:        ## dependencias de desarrollo (sin ADK)
	$(PY) -m pip install -e ".[docs,code,mcp,dev]"

samples:        ## genera examples/samples/ (PDFs, Ada, doors.sqlite)
	$(PY) examples/make_samples.py

fake:           ## servidor OpenAI falso en :8770 para atlas.test.toml
	$(PY) tests/fakes/fake_api.py --port 8770 --control faults.json

test:           ## pruebas automáticas (sin modelo)
	$(PY) -m pytest -q

test-ollama:    ## prueba de agente ADK contra Ollama local (se omite si no está en marcha)
	$(PY) -m pytest -q -m ollama

run-test:       ## procesamiento incremental sobre las muestras (sin modelo)
	$(PY) -m atlas.cli run atlas.test.toml

run-dev:        ## procesamiento con Ollama local
	$(PY) -m atlas.cli run atlas.dev.toml

status:
	$(PY) -m atlas.cli status atlas.dev.toml

sections:       ## secciones de cada documento de atlas.dev.toml, sin llamar al modelo
	$(PY) -m atlas.cli sections atlas.dev.toml

check:          ## prueba de conexión con el modelo local
	$(PY) -m atlas.cli check atlas.dev.toml

clean:
	rm -rf workspace-dev workspace-test examples/samples .pytest_cache

.PHONY: install samples fake test test-ollama run-test run-dev status sections check clean
