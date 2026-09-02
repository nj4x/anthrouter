import { useState } from 'react'
import useSWR from 'swr'

import type { StatusResponse } from '../api'
import { fetchJson } from '../api'
import { Metric, Panel } from '../components'
import { ConfigModal } from '../components/ConfigModal'
import { OAuthCard } from '../components/OAuthCard'
import { count, usd } from '../format'

export function Usage() {
  const { data } = useSWR<StatusResponse>('/admin/status', fetchJson, {
    refreshInterval: 10000,
  })
  const [configModalOpen, setConfigModalOpen] = useState(false)

  return (
    <div className="space-y-4">
      <ConfigModal isOpen={configModalOpen} onClose={() => setConfigModalOpen(false)} />
      <Panel title="OAuth Usage">
        <OAuthCard />
        <p className="mt-3 text-xs text-slate-600 dark:text-slate-400">
          OAuth token usage is cached from the last seen bearer auth request. The card refreshes every 60 seconds
          or when a new token is seen.
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
        <div className="space-y-3">
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
          <button
            onClick={() => setConfigModalOpen(true)}
            className="rounded border border-slate-300 bg-slate-100 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
          >
            Edit configuration
          </button>
        </div>
      </Panel>
    </div>
  )
}

function Setting({ label, value }: { label: string; value?: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">{label}</dt>
      <dd className="mt-1 break-all font-mono text-xs text-slate-800 dark:text-slate-200">{value ?? '—'}</dd>
    </div>
  )
}
