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
import { RequestDetailDrawer } from '../components/RequestDetailDrawer'
import { count, modelLabel, sessionLabel, timestamp, usd } from '../format'

const LIMIT = 50

export function Routing() {
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<number | null>(null)
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
        <div className="grid gap-3 sm:grid-cols-4 lg:grid-cols-7">
          <Metric label="Decisions" value={count(summary?.total)} />
          <Metric label="Applied" value={appliedShare} />
          <Metric label="Trivial" value={count(summary?.trivial)} />
          <Metric label="Standard" value={count(summary?.standard)} />
          <Metric label="Deep" value={count(summary?.deep)} />
          <Metric label="Net savings" value={usd(summary?.net_savings_usd)} />
          <Metric label="User-prompt classifier overhead" value={usd(summary?.classifier_overhead_usd)} />
        </div>
      </Panel>
      <Panel title="Routing decisions">
        <LoadState error={error} loading={isLoading} />
        {data && data.decisions.length === 0 && <Empty message="No routing decisions yet." />}
        {data && data.decisions.length > 0 && (
          <>
            <Table
              headers={['#', 'Time', 'Session', 'Requested', 'Routed', 'Class', 'Reason', 'Est. in', 'Classifier']}
            >
              {data.decisions.map((row) => (
                <tr
                  key={row.id}
                  className={`cursor-pointer hover:bg-slate-100 dark:hover:bg-slate-800 ${row.applied ? '' : 'text-slate-500 dark:text-slate-500'}`}
                  onClick={() => setSelected(selected === row.id ? null : row.id)}
                >
                  <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">#{row.id}</td>
                  <td className="px-3 py-2 text-slate-600 dark:text-slate-400">{timestamp(row.request_ts)}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">
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
                  <td className="px-3 py-2 font-mono text-xs">
                    {row.user_prompt_score != null && row.system_prompt_score != null ? (
                      <span>u:{Math.round(row.user_prompt_score)} s:{Math.round(row.system_prompt_score)}</span>
                    ) : row.classifier_raw_response ? (
                      <span className="text-slate-600 dark:text-slate-500">{row.classifier_raw_response.slice(0, 24)}</span>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              ))}
            </Table>
            <Pager offset={offset} limit={LIMIT} rows={data.decisions.length} onChange={setOffset} />
          </>
        )}
      </Panel>
      <RequestDetailDrawer requestId={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
