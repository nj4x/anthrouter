import useSWR from 'swr'

import type { StatusResponse } from '../api'
import { fetchJson } from '../api'
import { Metric, Panel } from '../components'
import { count, timestamp, usd } from '../format'

export function Usage() {
  const { data, error } = useSWR<StatusResponse>('/admin/status', fetchJson, {
    refreshInterval: 10000,
  })
  const rl = data?.ratelimit

  return (
    <div className="space-y-4">
      <Panel title="Rate-limit window">
        {error && <p className="text-sm text-red-400">{error.message}</p>}
        {data && !rl && (
          <p className="text-sm text-slate-400">
            No <code className="font-mono">anthropic-ratelimit-*</code> headers seen yet. They
            arrive on the first upstream response.
          </p>
        )}
        {rl && (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Metric label="Requests left" value={count(rl.ratelimit_requests_remaining)} />
              <Metric label="Tokens left" value={count(rl.ratelimit_tokens_remaining)} />
              <Metric label="Input tokens left" value={count(rl.ratelimit_input_tokens_remaining)} />
              <Metric label="Output tokens left" value={count(rl.ratelimit_output_tokens_remaining)} />
            </div>
            <p className="mt-3 text-sm text-slate-400">
              Window resets {timestamp(rl.ratelimit_reset_at)} · observed{' '}
              {timestamp(rl.request_ts)}
            </p>
          </>
        )}
        {/* Token expiry and quota are not derivable from a forwarded credential:
            OAuth tokens are opaque and the usage API needs a separate admin key. */}
        <p className="mt-3 text-xs text-slate-500">
          Rate-limit headers are the only usage signal a passthrough proxy can read. Token expiry
          and account quota need a credential anthrouter never holds.
        </p>
      </Panel>
      <Panel title="Totals">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Requests" value={count(data?.stats.requests)} />
          <Metric label="Sessions" value={count(data?.stats.sessions)} />
          <Metric label="Errors" value={count(data?.stats.errors)} />
          <Metric label="Rate limited" value={count(data?.stats.rate_limited)} />
          <Metric label="Input tokens" value={count(data?.stats.input_tokens)} />
          <Metric label="Output tokens" value={count(data?.stats.output_tokens)} />
          <Metric label="Cost" value={usd(data?.stats.cost_estimate)} />
          <Metric label="Cache savings" value={usd(data?.stats.cache_savings_usd)} />
        </div>
      </Panel>
      <Panel title="Configuration">
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <Setting label="Upstream" value={data?.upstream_base_url} />
          <Setting label="Auto model routing" value={data ? String(data.auto_model_routing) : undefined} />
          <Setting label="Sanitize system prompt" value={data?.sanitize_system_prompt} />
          <Setting label="Model baseline lock" value={data?.lock_requested_model} />
          <Setting
            label="DB retention"
            value={data ? (data.db_retention_days ? `${data.db_retention_days} days` : 'forever') : undefined}
          />
        </dl>
      </Panel>
    </div>
  )
}

function Setting({ label, value }: { label: string; value?: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-1 break-all font-mono text-xs text-slate-200">{value ?? '—'}</dd>
    </div>
  )
}
