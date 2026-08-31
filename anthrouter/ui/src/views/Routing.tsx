import { useState } from 'react'
import useSWR from 'swr'

import type { RoutingResponse } from '../api'
import { fetchJson } from '../api'
import {
  ClassificationBadge,
  Empty,
  LoadState,
  Metric,
  Pager,
  Panel,
  Table,
} from '../components'
import { count, modelLabel, sessionLabel, timestamp, usd } from '../format'

const LIMIT = 50

export function Routing() {
  const [offset, setOffset] = useState(0)
  const { data, error, isLoading } = useSWR<RoutingResponse>(
    `/admin/routing?limit=${LIMIT}&offset=${offset}`,
    fetchJson,
    { refreshInterval: 5000 },
  )

  const summary = data?.summary
  const appliedShare =
    summary && summary.total > 0
      ? `${Math.round((100 * (summary.applied ?? 0)) / summary.total)}%`
      : '—'

  return (
    <div className="space-y-4">
      <Panel title="Routing summary">
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="Decisions" value={count(summary?.total)} />
          <Metric label="Applied" value={appliedShare} />
          <Metric label="Trivial" value={count(summary?.trivial)} />
          <Metric label="Standard" value={count(summary?.standard)} />
          <Metric label="Deep" value={count(summary?.deep)} />
          <Metric label="Net savings" value={usd(summary?.net_savings_usd)} />
        </div>
      </Panel>
      <Panel title="Routing decisions">
        <LoadState error={error} loading={isLoading} />
        {data && data.decisions.length === 0 && <Empty message="No routing decisions yet." />}
        {data && data.decisions.length > 0 && (
          <>
            <Table
              headers={['Time', 'Session', 'Requested', 'Routed', 'Class', 'Reason', 'Est. in', 'Classifier']}
            >
              {data.decisions.map((row) => (
                <tr key={row.id} className={row.applied ? '' : 'text-slate-500'}>
                  <td className="px-3 py-2 text-slate-400">{timestamp(row.request_ts)}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-400">
                    {sessionLabel(row.session_id)}
                  </td>
                  <td className="px-3 py-2">{modelLabel(row.requested_model)}</td>
                  <td className="px-3 py-2">
                    {row.applied ? (
                      <span className="text-emerald-400">{modelLabel(row.routed_model)}</span>
                    ) : (
                      <span>unchanged</span>
                    )}
                  </td>
                  <td className="px-3 py-2"><ClassificationBadge value={row.classification} /></td>
                  <td className="px-3 py-2 font-mono text-xs">{row.reason_code ?? '—'}</td>
                  <td className="px-3 py-2">{count(row.estimated_input_tokens)}</td>
                  <td className="px-3 py-2 text-xs">
                    {row.classifier_model ? modelLabel(row.classifier_model) : '—'}
                    {row.classifier_raw_response && (
                      <span className="ml-1 font-mono text-slate-500">
                        {row.classifier_raw_response.slice(0, 24)}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </Table>
            <Pager offset={offset} limit={LIMIT} rows={data.decisions.length} onChange={setOffset} />
          </>
        )}
      </Panel>
    </div>
  )
}
