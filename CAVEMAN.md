# Routing caveman proxy to a custom Anthropic upstream

This document describes how to configure the caveman proxy to route API requests to a custom Anthropic-compatible upstream (e.g., a corporate gateway, local mock server, or LiteLLM instance) instead of `api.anthropic.com`.

## Configuration

### 1. Create `~/.caveman/caveman.yaml`

```yaml
providers:
  anthropic:
    base_url: http://127.0.0.1:28082
mode: compress
```

Replace `http://127.0.0.1:28082` with your upstream URL. The proxy appends `/v1/messages` and other Anthropic routes automatically — do **not** include the path in `base_url`.

### 2. Allow loopback/private IPs via SSRF guard

The proxy's SSRF guard blocks all loopback and private addresses by default. Allowlist your upstream, and add it to `~/.zshrc` or `~/.bashrc` so it persists across shells:

```bash
export CAVE_SSRF_ALLOWLIST="127.0.0.1:28082"
```

For multiple upstreams, use comma-separated values:

```bash
export CAVE_SSRF_ALLOWLIST="127.0.0.1:28082,10.0.0.5:8000,localhost"
```

### 3. Install MCP recovery

Local compression elides response detail behind `<<ccr:handle>>` markers. Only an agent with the caveman MCP `caveman_retrieve` tool installed can expand those markers back to the original bytes. Without it, the proxy fails closed and stays byte-identical pass-through even in `compress` mode — this is intentional, not a bug.

Install the tool into your agent once:

```bash
caveman mcp install claude
```

This is a persistent, per-agent registration, not tied to any particular proxy invocation.

### 4. Start the proxy

Do **not** use `caveman start` — it hardcodes recovery off (a bare proxy has no bound agent identity, so it can't prove whoever connects can recover elided detail), so compression stays inert and the proxy gains nothing over plain pass-through. Run the compiled binary directly instead, with recovery armed:

```bash
CAVEMAN_RECOVERY=mcp \
CAVEMAN_HOME=~/.caveman \
CAVEMAN_LISTEN=127.0.0.1:8787 \
CAVEMAN_PROXY_OWNER=start \
~/.caveman/bin/caveman-proxy
```

The binary reads `CAVEMAN_RECOVERY` directly with no validation (`proxy/cmd/caveman-proxy/main.go:265`); `mode: compress` comes from `~/.caveman/caveman.yaml` (step 1).

**Warning:** only set `CAVEMAN_RECOVERY=mcp` if every client that will route through this proxy has the MCP tool installed (step 3). If a client without it connects, it will receive unrecoverable `<<ccr:handle>>` markers it has no way to expand — a corrupted response, not a graceful degrade.

The proxy will:
- Read the config from `~/.caveman/caveman.yaml`
- Apply the SSRF allowlist from the environment
- Listen on `127.0.0.1:8787` (or `CAVEMAN_LISTEN`)
- Route Anthropic requests to your custom upstream, with compression and recovery active

## Testing

Direct upstream (bypassing proxy):

```bash
curl http://127.0.0.1:28082/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

Through the proxy:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

Both should return valid Claude responses (authentication is not required if your upstream doesn't require it).

## Advanced

### 5. (Optional) Disable unused MCP tools to save context

`caveman mcp install` (step 3) registers five tools, not just the recovery one:

```
mcp__caveman__caveman_compress      188 tokens
mcp__caveman__caveman_retrieve      482 tokens
mcp__caveman__caveman_stats          70 tokens
mcp__caveman__caveman_toon_decode    95 tokens
mcp__caveman__caveman_toon_encode   137 tokens
```

Only `caveman_retrieve` is part of the proxy's recovery gate — the proxy checks for that exact tool name (matched by suffix, so it works under Claude Code's `mcp__caveman__` prefix) and nothing else. `caveman_compress`, `caveman_stats`, and the two `caveman_toon_*` tools are independent, agent-invoked convenience tools unrelated to the elision/recovery pipeline. There's no server-side flag to register only a subset — the MCP server always advertises all five — but Claude Code lets you deny individual tools by name without disabling the server. Add to `.claude/settings.json` or `.claude/settings.local.json`:

```json
{
  "permissions": {
    "deny": [
      "mcp__caveman__caveman_compress",
      "mcp__caveman__caveman_stats",
      "mcp__caveman__caveman_toon_encode",
      "mcp__caveman__caveman_toon_decode"
    ]
  }
}
```

Keep `mcp__caveman__caveman_retrieve` un-denied. Recovery stays fully intact; you just drop the ~490 tokens/turn of tool-schema overhead for the other four.

### 6. Disable the NATIVE_CORE session-start injection

```bash
caveman tools config set think.core off
```

By default, caveman injects a fixed, non-configurable text block (`NATIVE_CORE`) into every Claude Code session at `SessionStart`. This setting turns that injection off without touching any other caveman hook — command-output shrinking (`shrink-hook`) and proxy-routed prompt caching keep working. Takes effect on the next new session.

## Notes

- **Auth**: The proxy forwards all auth headers transparently (`Authorization`, `x-api-key`). It does not require or validate them.

## References

- Proxy config loader: `packages/cli/src/index.ts:3191` — reads `$CAVEMAN_CONFIG` or defaults to `~/.caveman/caveman.yaml`
- SSRF allowlist: `shared/platform/ssrf/ssrf.go` — blocks loopback/private by default; `CAVE_SSRF_ALLOWLIST` opts specific hosts back in
- Provider routing: `proxy/providers/anthropic/anthropic.go:29-32` — appends `/v1/messages` to `base_url`
- Why `caveman start` is avoided: `packages/cli/src/index.ts:9776-9778` (`startMcpRecoveryAvailable()` always returns `false`), stamped into the child env at `packages/cli/src/index.ts:2009-2014`
- Proxy binary reads `CAVEMAN_RECOVERY` directly with no validation: `proxy/cmd/caveman-proxy/main.go:265`
- `caveman claude` / `caveman wrap claude` arm recovery via real MCP binary detection instead, as an alternative to the standalone binary invocation above: `packages/cli/src/index.ts:5150-5160` (`probeMcpBinary()`)
- Research findings: `docs/technical/proxy-integration-claude-code.md`
