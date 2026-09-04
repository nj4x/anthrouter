# Build and Test

## Test commands

```bash
make test           # pytest tests/ -v
make lint           # ruff check .
```

Single test: `python -m pytest tests/test_model_router.py::test_name -v`

## UI build and test

```bash
make ui-test        # cd anthrouter/ui && npm run lint (oxlint) && npm test (vitest run)
make ui-build       # cd anthrouter/ui && npm run build — rebuild anthrouter/ui/dist
```

`anthrouter/ui/dist` is checked in and must be rebuilt and committed after any change under `anthrouter/ui/src`. See the root `CLAUDE.md` § Core commands for the full constraint — that is the authoritative copy.

## Live UI development

For live UI development against a running proxy, run `npm run dev` in `anthrouter/ui`. This proxies `/admin` requests to `http://127.0.0.1:8083`, letting you test the UI against a real backend without a full rebuild.
