.PHONY: lint test ui-build ui-test

lint:
	python -m ruff check .

test:
	python -m pytest tests/ -v

ui-build:
	cd anthrouter/ui && npm run build

ui-test:
	cd anthrouter/ui && npm run lint && npm test
