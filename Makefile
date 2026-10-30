PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

.PHONY: lint test ui-build ui-test

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest tests/ -v

ui-build:
	cd anthrouter/ui && npm run build

ui-test:
	cd anthrouter/ui && npm run lint && npm test
