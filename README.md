# anthrouter

A single-backend Anthropic proxy. It classifies each request and routes it to a
model tier, strips volatile system-prompt blocks that would otherwise break
prompt caching, records what it did to SQLite, and forwards the client's own
Anthropic credential untouched to `api.anthropic.com`. It manages no
credentials of its own.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/nj4x/anthrouter/main/install.sh | bash
```

From a clone:

```bash
./install.sh
```

The installer creates a venv under `~/.anthrouter`, installs anthrouter into
it, drops an `anthrouter` shim in `~/.local/bin`, starts the proxy on
`127.0.0.1:8083`, health-checks it, and only then wires it into Claude Code.

Two topologies are recognized, decided by `~/.claude/settings.json`:

| `env.ANTHROPIC_BASE_URL`      | Topology | What the installer writes                                                     |
| ----------------------------- | -------- | ----------------------------------------------------------------------------- |
| absent                        | direct   | `settings.json` points at anthrouter                                          |
| `http://127.0.0.1:8787/w/claude` | chained  | caveman's `providers.anthropic.base_url` and `CAVE_SSRF_ALLOWLIST` in your shell profile |
| anything else                 | —        | aborts, changes nothing                                                       |

In the chained topology the request path becomes Claude Code → caveman →
anthrouter → api.anthropic.com, and `settings.json` is left alone. A shell-level
`ANTHROPIC_BASE_URL` export is never rewritten — it governs non-Claude-Code
clients — but the installer points it out.

Every mutation is recorded in `~/.anthrouter/manifest.json` before it is made,
so uninstall reverses exactly what install did. A failure part-way through
rolls back automatically. Custom ports are not supported by the installer; the
wired address is always `127.0.0.1:8083`. Shell profile edits support bash and
zsh only.

### Manifest fields

`~/.anthrouter/manifest.json` is written by the installer and read by the uninstaller and update script. Key fields:

| Field | Description |
| ----- | ----------- |
| `upstreamUrl` | Git remote URL used to fetch updates. Clone installs record `REPO_URL`; local installs record the `origin` remote of the source checkout. `update.sh` reads this field and never re-derives it. |
| `sourceMode` | `clone` or `local` — which install topology was used |
| `topology` | `direct` or `chained` — how Claude Code routes to anthrouter |
| `completed` | `true` once install finishes; `false` in a partial install |

## Run

The installer starts anthrouter in the background and leaves it unsupervised —
there is no launchd or systemd unit. To start it again after a reboot:

```bash
anthrouter          # reads ~/.anthrouter/config.env
```

Settings live in `~/.anthrouter/config.env` (every one has a matching flag; see
`anthrouter --help`). Logs go to `~/.anthrouter/anthrouter.log`, requests to
`~/.anthrouter/anthrouter.db`, and the read-only UI is at
<http://127.0.0.1:8083/ui/>.

## Uninstall

```bash
~/.anthrouter/uninstall.sh            # keeps config.env and the request DB
~/.anthrouter/uninstall.sh --purge    # removes ~/.anthrouter entirely
```

Uninstall is idempotent: every restore reads the live file or the manifest, so
re-running it corrects a run that failed part-way.

## Updating

```bash
~/.anthrouter/update.sh
```

The update script performs an in-place update (ADR 0006): stops the running process, backs up the venv, fetches a fresh shallow clone from the upstream URL recorded in the manifest, upgrades the venv via pip, restarts the proxy, and health-checks it. On any failure, it restores the backup and restarts the old version. Configuration files, the request database, and logs are preserved.

## Admin UI

`--enable-ui` turns on the read-only `/admin/*` JSON API and serves the built
SPA at `/ui/`. Four views: the request log (searchable over prompt and response
text), routing decisions, sanitizer strip events, and the rate-limit window.
There are no runtime controls — with one backend there is nothing to switch.

Both surfaces are unauthenticated and expose conversation text, so keep the
server bound to loopback unless something else in front of it authenticates.
`--enable-ui` implies a DB: without `--db-path` it defaults to
`~/.anthrouter/anthrouter.db`.

## Development

```bash
make test           # pytest
make ui-test        # vitest + oxlint
make ui-build       # rebuild anthrouter/ui/dist (commit the result)
```

`anthrouter/ui/dist` is checked in, because the server ships it and the
installer only ever runs `pip install`. After changing anything under
`anthrouter/ui/src`, rebuild and commit the generated bundle. For UI work
against a live proxy, `npm run dev` in `anthrouter/ui` proxies `/admin` to
`127.0.0.1:8083`.

Design decisions are recorded in `docs/adr/`; installer behavior is
ADR-0001.
