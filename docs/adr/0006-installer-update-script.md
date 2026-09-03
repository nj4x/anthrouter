---
artifact-type: adr
status: accepted
---

# In-place update mechanism (update.sh)

anthrouter installs as a pip snapshot into `~/.anthrouter/venv` — not a live git checkout — so the `git pull + unpatch/repatch` update pattern used by caveman-kit (ADR 0007 there) and peer-agent-kit (ADRs 0080–0084 there) does not transfer. Unlike those kits, anthrouter's config surfaces (`settings.json` / `caveman.yaml` wiring, shell-profile allowlist) are version-independent: they point at `127.0.0.1:8083` regardless of which version runs, so an update never needs an unpatch/repatch cycle. What *does* change across versions is the venv contents and, potentially, the DB schema (which the server already migrates itself via `PRAGMA user_version` on startup).

We add a standalone `update.sh` with this shape: stop the recorded PID → back up `venv/` to `venv.bak/` → fresh shallow clone from the upstream GitHub URL into a temp dir → `pip install --upgrade` into the existing venv from that clone → restart via the shim → poll `GET /health` (same 10s discipline as install) → on success delete `venv.bak/` and update the manifest; on any failure restore `venv.bak/` and restart the old version.

## Decisions and rejected alternatives

- **Separate script, not `install.sh --update`.** install.sh's re-run guard (abort on existing manifest, ADR 0001) is load-bearing; threading an update path through it would fork every step with conditionals. Both peer kits reached the same conclusion.
- **Always pull from GitHub, for both `sourceMode` values.** The `local` mode is a first-install convenience; treating the user's original checkout path as a durable update source fails silently when the checkout moves or goes stale. The upstream URL is recorded in the manifest at install time (`upstreamUrl` field) and read — never re-derived — by update.sh. If the field is absent (pre-existing install), update.sh aborts with an actionable error telling the user to add `upstreamUrl` to the manifest or re-run install.sh; it never substitutes the canonical repo URL, because a fork or mirror install would be silently rerouted to a source the user never chose — the same silent-substitution failure class this decision exists to avoid.
- **No version-comparison gate.** The peer kits compare a manifest `kitSha` against the remote HEAD to short-circuit no-op updates. We skip this, and we accept the full cost of doing so: a no-op run still pays the entire chain — process stop, ~50 MB venv copy, shallow clone, pip install, restart, health poll — not merely a redundant pip resolve. The justification is frequency and simplicity, not cheapness: update.sh is only ever invoked deliberately by a user who believes an update exists (no cron, no auto-update hook), so no-op runs are rare and user-initiated; the gate would add a remote SHA fetch, a manifest field that must be kept honest across releases, and a new failure mode (stale or unreachable remote wrongly short-circuiting a real update) to optimize that rare case. The chain is also safe to pay redundantly: backup/restore makes a no-op run land the proxy back in its starting state.
- **Venv backup over atomic swap.** A parallel-venv build with an atomic rename (build `venv.new`, health-check, swap) is the most robust option but the shim's absolute paths into the venv and the single-port process model erase most of the benefit. Copying `venv/` aside (~50 MB transient) before the in-place upgrade gives a reliable restore path at a fraction of the complexity. No-rollback (the peer kits' choice) was rejected because a half-upgraded venv, unlike a half-pulled git tree, is not recoverable by re-running.

## Consequences

- `config.env`, `anthrouter.db`, logs, and all Claude Code / caveman wiring survive updates untouched — the same preservation set as non-purge uninstall.
- The health-check-before-commit ordering keeps the ADR 0001 invariant: Claude Code is never left pointed at a proxy that isn't serving — *provided restore itself succeeds*. If venv restore fails (disk full, I/O error, corrupted backup) after a failed health check, update.sh exits non-zero and prints both the error and a recovery command (e.g., `~/.anthrouter/shim start`) so the operator can manually restart the old or new version and proceed. This explicit failure state, rather than silent retry or unattended exit, preserves the invariant via human intervention — the script does not attempt to hide a state it cannot recover from.
- If update.sh is killed between backup and restore, `venv.bak/` remains on disk; re-running update.sh must detect and reuse or clean it rather than layering a second backup over a possibly-broken venv.
- New `config.env` keys introduced by a version ship as in-code defaults, not config rewrites — update.sh never edits `config.env`.
