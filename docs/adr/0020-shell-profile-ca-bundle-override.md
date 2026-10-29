---
artifact-type: adr
lineage-rules: root
---

**Status**: Proposed

# Shell-profile `NODE_EXTRA_CA_CERTS` override for Claude Desktop

ADR-0019 made Claude Code trust anthrouter's certificate authority by writing `NODE_EXTRA_CA_CERTS` into `~/.claude/settings.json`. On the reference machine that fails. `~/.zshrc` already exports `NODE_EXTRA_CA_CERTS` to Netskope's bundle. Claude Desktop starts each Code tab session with the user's shell environment, and Bun reads `NODE_EXTRA_CA_CERTS` once, at process start. So the Netskope bundle is loaded, the `settings.json` value comes too late, and every decrypted `api.anthropic.com` call fails with `UNABLE_TO_VERIFY_LEAF_SIGNATURE`. We now give both places one combined bundle, rebuilt by anthrouter at every start, and override the shell value with a marked block in the shell profile. For installs made under this ADR, it replaces in ADR-0019: step 4 (trust scope), the Consequence "Nothing else trusts it" and the step-11 "new Code tab session" message, all three only when the profile block is written (step 4 below); and always, the `NODE_EXTRA_CA_CERTS` part of step 7, the `nodeExtraCaCerts` rules of step 8, the final "Then delete `~/.anthrouter/tls/`" bullet of step 8, and the Consequences "The CA bundle is a snapshot" and "uninstall deletes it". It also corrects ADR-0019's key-leak Consequence, rewritten in place in ADR-0019 because it was a factual error: an unconstrained CA key can impersonate any host to Claude Code, not only `api.anthropic.com`. Installs made under ADR-0019 alone keep ADR-0019's rules until they are re-installed.

## Context

Observed on 2026-10-28:

- The Desktop Code tab process (`claude-code/2.1.309`) had `NODE_EXTRA_CA_CERTS=/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem` in its environment, from `~/.zshrc:27`. `launchctl getenv` had no value.
- Its decrypted connections reached anthrouter, so `HTTPS_PROXY` from `settings.json` did apply. Then the client aborted the TLS handshake (`UNEXPECTED_EOF_WHILE_READING` on anthrouter's side).
- Bun 1.4.2 through the proxy: `401` from Anthropic with `NODE_EXTRA_CA_CERTS=<ca.pem>` in the process environment, and `UNABLE_TO_VERIFY_LEAF_SIGNATURE` without it.
- The terminal CLI (2.1.300) with the shell variable unset and `NODE_EXTRA_CA_CERTS` in a `--settings` file: full chain, `200`. So the `settings.json` value works only when the process starts without the variable.
- The Netskope bundle is owned by root and Netskope replaces it (`nscacert.pem` changed on Oct 27). anthrouter cannot append to it, and a one-time copy goes stale.
- The Netskope bundle the shell exports, `cacertbundle.pem`, was last replaced on Sep 12; its source certificate `nscacert.pem` on Oct 27. Rotation is rare.
- Not observed: whether Desktop reads the shell environment once at app launch or per session. The install message (step 9) asks for a Desktop restart, which covers both.
- Assumption, not observed directly: Desktop takes the session environment from a login, interactive shell. A plain interactive shell would also have read `~/.zshrc:27`. If Desktop uses an interactive-only shell, the zsh path is unaffected (both read `~/.zshrc`); on bash the step-6 check could pass on `~/.bash_profile` while Desktop reads only `~/.bashrc`, or the reverse.
- Name constraints are enforced. With a test CA carrying `nameConstraints=critical,permitted;DNS:api.anthropic.com` in `NODE_EXTRA_CA_CERTS`, both Bun 1.4.2 and Node accepted a leaf for `api.anthropic.com` and rejected a leaf for `localhost` signed by the same CA.

## Decision

1. **One combined bundle.** `~/.anthrouter/tls/ca-bundle.pem` holds every source bundle, in order, with duplicates dropped, followed by `ca.pem`. Both the shell-profile export (step 4) and `settings.json` `env.NODE_EXTRA_CA_CERTS` name this file. The sources are:
   - the effective shell value from pre-flight (step 3), and
   - the original `settings.json` value, if there was one (ADR-0019's `backed_up` path).

   The `settings.json` write records `nodeExtraCaCerts: created` when there was no earlier value and `backed_up` when there was, as in ADR-0019 step 7; in both cases the installed value is the bundle path.
2. **anthrouter rebuilds the bundle at every start.**
   - A new `Config.interception_ca_sources` (`--interception-ca-sources` / `ANTHROUTER_INTERCEPTION_CA_SOURCES`) holds the source paths, separated by `:`. Paths with spaces are allowed; paths with `:` are not.
   - The field stays out of `EDITABLE_FIELDS`: an admin-API write must not be able to add trust roots that every Node process in the shell then accepts.
   - The rebuild runs whenever the key is present, whether or not `connect_proxy` is on, because the profile export names the bundle either way. Startup writes the bundle to a temporary file in `tls/` and renames it into place, so a reader never sees a partial file.
   - A source counts as readable only when it parses as at least one PEM certificate and the file does not end inside a PEM block. A copy cut off mid-certificate is caught; a copy cut off exactly between certificates is not, and drops the missing roots until the next start.
   - When a bundle exists and any source is unreadable, or the write or rename fails, anthrouter logs a WARNING and keeps the existing bundle. It does not abort: the tunnels that other Claude Code traffic needs would go down with it.
   - When the field is not set (neither the flag nor `ANTHROUTER_INTERCEPTION_CA_SOURCES`), anthrouter neither creates nor rebuilds the bundle. Installs made under ADR-0019 alone have no such key: a `backed_up` install keeps its bundle (the user's roots plus `ca.pem`) as ADR-0019 left it, and a `created` install keeps using `ca.pem` directly.
   - Installs made under this ADR always write the key, as an empty string when there are no sources. Present and empty means the bundle is `ca.pem` alone, rebuilt at each start like any other value.
   - When the key is present and no bundle exists yet, anthrouter builds one from the sources it can read plus `ca.pem`. When that first write or rename fails, anthrouter logs an ERROR naming the export and the missing file, and starts anyway, for the same reason it never aborts on a rebuild failure. The install order (step 5) catches this at install time. A first build that skips an unreadable source logs a WARNING naming it.
3. **Pre-flight reads the effective shell value.**
   - The installer runs `$SHELL -ilc 'printf %s "$NODE_EXTRA_CA_CERTS"'`, a login and interactive shell, which is assumed to match what Desktop uses (Context).
   - A non-empty value becomes a bundle source. If it fails the step-2 readable test (at least one PEM certificate, not ending inside a PEM block), pre-flight aborts. The same test now also applies to the `settings.json` value, which ADR-0019 step 2 only checked for readability.
   - Pre-flight aborts, with the value printed, when the shell value or the `settings.json` value contains `:`, because `:` separates the sources.
   - Pre-flight aborts and changes nothing when the probe exits non-zero or runs longer than 10 seconds. The probe prints the value between two fixed sentinel strings, and the installer takes only the text between them, so profile output on stdout or stderr cannot change it. When the sentinels are missing, pre-flight aborts and shows the raw probe output.
   - When the shell value is non-empty and `$SHELL` is not bash or zsh, pre-flight aborts and names the shell, as ADR-0001 does for the allowlist.
   - These pre-flight rules run only when the ADR-0019 wiring runs (Darwin, Claude.app present, no `--no-desktop-proxy`).
   - Pre-flight aborts, prints the value and changes nothing when `launchctl getenv NODE_EXTRA_CA_CERTS` is set. The message names both ways out: `launchctl unsetenv NODE_EXTRA_CA_CERTS` (moving the value to the shell profile if it is still needed), or `--no-desktop-proxy`. A launchd value is assumed not to reach the shell probe, the profile block would not be written, and the original failure would return unseen. This is the same rule ADR-0019 step 2 applies to `HTTPS_PROXY`.
   - The sources are written to `config.env` as `ANTHROUTER_INTERCEPTION_CA_SOURCES`, inside the existing `desktop-proxy` block (ADR-0019 step 5).
4. **Profile block, only when the shell sets the variable.**
   - When the effective value is non-empty, the installer appends this block to the same `$SHELL`-derived profile file ADR-0001 uses (`~/.zshrc` or `~/.bashrc`):
     ```
     # >>> anthrouter >>> ca-bundle
     export NODE_EXTRA_CA_CERTS="<tls dir>/ca-bundle.pem"
     # <<< anthrouter <<< ca-bundle
     ```
   - The manifest records the absolute path of that profile file as `caBundleProfileFile`, before the write.
   - `<tls dir>` is the real TLS directory, written as an absolute path: `~/.anthrouter/tls` by default, or `ANTHROUTER_INTERCEPTION_TLS_DIR` when the install home is elsewhere (ADR-0019 step 5).
   - The user's own export line stays as it is, and the later export wins. ADR-0001 refused a second `CAVE_SSRF_ALLOWLIST` export because the later line would drop the earlier value. Here the bundle contains the earlier value, so nothing is lost.
   - When the effective value is empty, the profile is not touched. Desktop sessions then start without the variable, and the `settings.json` value applies. ADR-0019 step 4's trust scope then still holds.
   - On zsh, `-il` reads `~/.zshrc`. On bash, a login shell reads `~/.bash_profile`, which reads `~/.bashrc` only if it sources it. When it does not, the step-6 check fails and rolls back with that reason; the user then sources `~/.bashrc` from `~/.bash_profile` and re-runs.
   - Bash Desktop wiring is best effort: if Desktop reads a different bash startup file from the one the step-6 check exercises (Context), the check can pass while Desktop still misses the block. The installer prints both file names on bash installs.
5. **Install order.**
   - The installer writes `ANTHROUTER_INTERCEPTION_CA_SOURCES` to `config.env` before the fork, so anthrouter builds the first bundle at its first start, with the step-2 code. There is one bundle builder. After the proxy health check (ADR-0019 step 6), the installer checks that `<tls dir>/ca-bundle.pem` exists and ends with `ca.pem`; otherwise it rolls back.
   - It writes the profile block after the proxy health check (ADR-0019 step 6), next to the `settings.json` writes, and records `caBundleProfileFile` (step 4) in the manifest before the write. That field also marks that a block was written; there is no separate flag.
   - So the export never names a file that does not exist: a missing file would make every Node tool in the shell warn and drop the Netskope root.
6. **Verify after the write.**
   - After the block is written, the installer runs the step-3 command again. If the result is not the bundle path, it rolls back and prints which value won.
   - Example: `~/.zlogin` exports the variable again after `~/.zshrc`.
7. **Name constraint on the certificate authority.**
   - Every new install gives the CA `nameConstraints=critical,permitted;DNS:api.anthropic.com`, so it can only vouch for `api.anthropic.com`, even if `ca.key` leaks. It costs nothing on a fresh install. It is required when step 4 widens trust from Claude Code to every Node and Bun process in the shell, because an unconstrained CA key would then let any of them be impersonated for any host.
   - This adds to the ADR-0019 step 3 extensions. The leaf's single SAN, `DNS:api.anthropic.com`, satisfies the constraint.
8. **Uninstall order.**
   - Uninstall removes every `ca-bundle` profile block first, from the file in `caBundleProfileFile`, whatever `$SHELL` is at uninstall time.
   - Then it restores or removes `settings.json` `env.NODE_EXTRA_CA_CERTS` by ADR-0019 step 8's guard pattern (act only while the live value is the installed value; warn otherwise). The installed value comes from a manifest field `nodeExtraCaCertsValue`, which installs made under this ADR write; installs without it use ADR-0019's values (`ca.pem` for `created`, the bundle for `backed_up`). The CA property `caNameConstrained` is not reused as this marker. So uninstall never leaves `NODE_EXTRA_CA_CERTS` naming a file inside `tls/` that it wrote itself, and a following `install.sh` does not hit the unreadable-file abort.
   - Then it deletes `tls/`, but only when no line naming `<tls dir>/ca-bundle.pem` remains in `caBundleProfileFile`, `~/.zshrc` or `~/.bashrc`. If the manifest records `caBundleProfileFile` and such a line survives outside the markers (edited markers), uninstall keeps `tls/` and the manifest, prints the file and line, and asks the user to remove the line and run `uninstall.sh` again, which then deletes `tls/`.
   - Any other order could leave the export naming a deleted file. The markers are the guard for the profile, as they are for the `config.env` block.
9. **Messages and existing installs.**
   - On success, when the profile block was written, the installer says to quit and reopen Claude Desktop, so new sessions get the new environment.
   - Every install under this ADR records `caNameConstrained: true` in the manifest.
   - `update.sh` prints a re-install hint for every manifest with `desktopProxy: true` and no `caNameConstrained` field, whatever the shell value is. Every such install holds an unconstrained CA key that can sign for any host Claude Code reaches (MCP servers, WebFetch targets), not only `api.anthropic.com`; a one-time re-install replaces it with the constrained CA. `update.sh` runs no shell probe and never aborts on this check.
   - When `launchctl getenv NODE_EXTRA_CA_CERTS` is also set, the hint says to run `launchctl unsetenv NODE_EXTRA_CA_CERTS` first, because re-install would otherwise stop at the step-3 abort.

## Consequences

- **Wider trust scope, narrower authority** (only when the profile block is written).
  - Every Node or Bun process started from the user's shell trusts anthrouter's certificate authority, not only Claude Code. Examples: `npm`, `npx`, and `gh` extensions.
  - The name constraint limits that trust to `api.anthropic.com`.
  - Those tools reach `api.anthropic.com` through anthrouter only if they also use `HTTPS_PROXY`. Otherwise the extra root is unused.
- **The bundle is as fresh as the last start that could read every source** (a first build can skip an unreadable source, with a WARNING). While anthrouter is stopped the bundle is frozen; restarting anthrouter or running `uninstall.sh` fixes it, and editing the user's own export line does not help while the block is present. A Netskope rotation reaches Node tools after the next anthrouter restart, not at once. Between the rotation and that restart, Node tools in the shell trust only the old Netskope root. Given the observed rotation rate (Context), we accept this window over a file watcher.
- **Environment changes after install are not detected.** A later shell export placed after the block silently wins again. A later `launchctl setenv` brings the original failure back when no profile block was written; with a block, it matters only if Desktop reads launchd values ahead of the shell, which was not observed (Context). Re-running `install.sh` (after `uninstall.sh`) reports it through pre-flight and the step-6 check.
- **The profile now carries two `NODE_EXTRA_CA_CERTS` exports** (only when the profile block is written). Editing the user's own line has no effect while the block is present. The source path is read once at install, so a changed source needs `uninstall.sh` and then `install.sh`.
- **A broken bundle affects more than Claude Code.** The atomic write (step 2) and the install and uninstall order (steps 5 and 8) exist to stop the export ever naming a missing or partial file.
- **Desktop must be restarted** to pick up the profile change (only when the profile block is written). Sessions open at install time keep failing until then.

## Considered options

- **Rewrite the user's export line in place.** It edits a line the user owns, and restoring it on uninstall needs the exact original text. The appended block changes nothing the user wrote. Rejected.
- **Build the bundle inside the profile block at each shell start** (`cat source ca.pem > bundle`). No anthrouter config field and no staleness. Rejected: it runs on every interactive shell start, writes into `tls/` from any shell, and a partial write in one shell would break Node TLS in every other shell started at the same time.
- **`launchctl setenv NODE_EXTRA_CA_CERTS`.** Desktop takes its environment from the shell, not from launchd alone, and the shell export would still win. It also does not survive a reboot without a LaunchAgent. Rejected.
- **Rebuild the bundle when a source changes (file watch).** It adds a watcher thread for a file that changes a few times a year. Rebuilding at each start is enough. Rejected.
- **Trust the CA in the login keychain.** It reverses ADR-0019's keychain decision and needs a password prompt. The profile block reaches the same processes. Rejected.
