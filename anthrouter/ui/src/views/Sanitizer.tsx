import { useState } from 'react'
import useSWR from 'swr'

import type { SanitizerResponse } from '../api'
import { fetchJson } from '../api'
import { Badge, Empty, LoadState, Metric, Pager, Panel, Table } from '../components'
import { count, modelLabel, sessionLabel, timestamp } from '../format'

const LIMIT = 50

export function Sanitizer() {
  const [offset, setOffset] = useState(0)
  const { data, error, isLoading } = useSWR<SanitizerResponse>(
    `/admin/sanitizer-events?limit=${LIMIT}&offset=${offset}`,
    fetchJson,
    { refreshInterval: 5000 },
  )

  const summary = data?.summary
  return (
    <div className="space-y-4">
      <Panel title="Sanitizer summary">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Events" value={count(summary?.total_events)} />
          <Metric label="Requests touched" value={count(summary?.requests_with_events)} />
          <Metric label="Requests changed" value={count(summary?.requests_changed)} />
          <Metric label="Block types" value={count(summary?.distinct_block_types)} />
        </div>
      </Panel>
      <Panel title="Strip events">
        <LoadState error={error} loading={isLoading} />
        {data && data.events.length === 0 && (
          <Empty message="No volatile system blocks seen yet." />
        )}
        {data && data.events.length > 0 && (
          <>
            <Table headers={['Time', 'Request', 'Session', 'Model', 'Block', 'Outcome', 'Preview']}>
              {data.events.map((event) => (
                <tr key={event.id}>
                  <td className="px-3 py-2 text-slate-600 dark:text-slate-400">{timestamp(event.event_ts)}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">#{event.request_id}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">
                    {sessionLabel(event.session_id)}
                  </td>
                  <td className="px-3 py-2">{modelLabel(event.requested_model)}</td>
                  <td className="px-3 py-2 font-mono text-xs">{event.block_type}</td>
                  <td className="px-3 py-2">
                    {event.is_allowlisted ? (
                      <Badge text="stripped" tone="bg-amber-900 text-amber-200" />
                    ) : (
                      <Badge text="flagged" tone="bg-slate-200 dark:bg-slate-700 text-slate-800 dark:text-slate-200" />
                    )}
                  </td>
                  <td className="max-w-md truncate px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">
                    {event.payload_preview ?? '—'}
                  </td>
                </tr>
              ))}
            </Table>
            <Pager offset={offset} limit={LIMIT} rows={data.events.length} onChange={setOffset} />
          </>
        )}
      </Panel>
    </div>
  )
}
