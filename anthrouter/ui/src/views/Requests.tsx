import { useState } from 'react'
import useSWR from 'swr'

import type { RequestRow, RequestsResponse } from '../api'
import { fetchJson } from '../api'
import { Empty, LoadState, Pager, Panel, StatusBadge, Table } from '../components'
import { RequestDetailDrawer } from '../components/RequestDetailDrawer'
import { count, duration, modelLabel, sessionLabel, timestamp, usd } from '../format'

const LIMIT = 50

export function Requests() {
  const [offset, setOffset] = useState(0)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<number | null>(null)

  const search = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) })
  if (query) search.set('q', query)
  const { data, error, isLoading } = useSWR<RequestsResponse>(
    `/admin/requests?${search}`,
    fetchJson,
    { refreshInterval: 5000 },
  )

  return (
    <div className="space-y-4">
      <Panel title="Request log">
        <input
          type="search"
          value={query}
          placeholder="Search prompt and response text…"
          onChange={(event) => {
            setQuery(event.target.value)
            setOffset(0)
          }}
          className="mb-3 w-full rounded border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-2 text-sm text-slate-900 dark:text-slate-100 placeholder:text-slate-500 dark:placeholder:text-slate-500"
        />
        <LoadState error={error} loading={isLoading} />
        {data && data.requests.length === 0 && <Empty message="No requests recorded yet." />}
        {data && data.requests.length > 0 && (
          <>
            <Table headers={['#', 'Time', 'Session', 'Model', 'Status', 'Tokens', 'Cost', 'Took', 'Prompt']}>
              {data.requests.map((row) => (
                <Row
                  key={row.id}
                  row={row}
                  onSelect={() => setSelected(selected === row.id ? null : row.id)}
                />
              ))}
            </Table>
            <Pager offset={offset} limit={LIMIT} rows={data.requests.length} onChange={setOffset} />
          </>
        )}
      </Panel>
      <RequestDetailDrawer requestId={selected} onClose={() => setSelected(null)} />
    </div>
  )
}

function Row({ row, onSelect }: { row: RequestRow; onSelect: () => void }) {
  const routed = row.routed_model && row.routed_model !== row.requested_model
  return (
    <tr className="cursor-pointer hover:bg-slate-100 dark:hover:bg-slate-800" onClick={onSelect}>
      <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">#{row.id}</td>
      <td className="px-3 py-2 text-slate-600 dark:text-slate-400">{timestamp(row.request_ts)}</td>
      <td className="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">{sessionLabel(row.session_id)}</td>
      <td className="px-3 py-2">
        {modelLabel(row.requested_model)}
        {routed && (
          <span className="ml-1 text-emerald-400">as {modelLabel(row.routed_model)}</span>
        )}
      </td>
      <td className="px-3 py-2"><StatusBadge value={row.status} /></td>
      <td className="px-3 py-2 text-slate-700 dark:text-slate-300">
        {count(row.input_tokens)} in / {count(row.output_tokens)} out
      </td>
      <td className="px-3 py-2 text-slate-700 dark:text-slate-300">{usd(row.cost_estimate)}</td>
      <td className="px-3 py-2 text-slate-600 dark:text-slate-400">{duration(row.duration_ms)}</td>
      <td className="max-w-md truncate px-3 py-2 text-slate-600 dark:text-slate-400">{row.user_prompt_text ?? '—'}</td>
    </tr>
  )
}

