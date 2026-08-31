import { useState } from 'react'
import useSWR from 'swr'

import type { RequestDetail, RequestRow, RequestsResponse } from '../api'
import { fetchJson } from '../api'
import { Empty, LoadState, Pager, Panel, StatusBadge, Table } from '../components'
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
          className="mb-3 w-full rounded border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
        />
        <LoadState error={error} loading={isLoading} />
        {data && data.requests.length === 0 && <Empty message="No requests recorded yet." />}
        {data && data.requests.length > 0 && (
          <>
            <Table headers={['Time', 'Session', 'Model', 'Status', 'Tokens', 'Cost', 'Took', 'Prompt']}>
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
      {selected !== null && <Detail requestId={selected} />}
    </div>
  )
}

function Row({ row, onSelect }: { row: RequestRow; onSelect: () => void }) {
  const routed = row.routed_model && row.routed_model !== row.requested_model
  return (
    <tr className="cursor-pointer hover:bg-slate-800" onClick={onSelect}>
      <td className="px-3 py-2 text-slate-400">{timestamp(row.request_ts)}</td>
      <td className="px-3 py-2 font-mono text-xs text-slate-400">{sessionLabel(row.session_id)}</td>
      <td className="px-3 py-2">
        {modelLabel(row.requested_model)}
        {routed && (
          <span className="ml-1 text-emerald-400">as {modelLabel(row.routed_model)}</span>
        )}
      </td>
      <td className="px-3 py-2"><StatusBadge value={row.status} /></td>
      <td className="px-3 py-2 text-slate-300">
        {count(row.input_tokens)} in / {count(row.output_tokens)} out
      </td>
      <td className="px-3 py-2 text-slate-300">{usd(row.cost_estimate)}</td>
      <td className="px-3 py-2 text-slate-400">{duration(row.duration_ms)}</td>
      <td className="max-w-md truncate px-3 py-2 text-slate-400">{row.user_prompt_text ?? '—'}</td>
    </tr>
  )
}

function Detail({ requestId }: { requestId: number }) {
  const { data, error, isLoading } = useSWR<RequestDetail>(
    `/admin/requests/${requestId}`,
    fetchJson,
  )
  return (
    <Panel title={`Request #${requestId}`}>
      <LoadState error={error} loading={isLoading} />
      {data && (
        <dl className="grid gap-3 text-sm md:grid-cols-2">
          <Field label="Session" value={data.request.session_id} mono />
          <Field label="Reason code" value={data.request.reason_code ?? '—'} />
          <Field label="Cache read" value={count(data.request.cache_read_tokens)} />
          <Field label="Cache savings" value={usd(data.request.cache_savings_usd)} />
          <Field label="System prompt" value={data.request.system_prompt_sha256 ?? '—'} mono />
          <Field
            label="Sanitized prompt"
            value={data.request.system_prompt_sanitized_sha256 ?? 'sanitizer did not run'}
            mono
          />
          <Field label="Error" value={data.request.error ?? '—'} />
          <Field label="Stripped blocks" value={String(data.sanitizer_events.length)} />
          <div className="md:col-span-2">
            <dt className="text-xs uppercase tracking-wide text-slate-400">Response</dt>
            <dd className="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap rounded bg-slate-800 p-3 text-slate-200">
              {data.request.response_text ?? '—'}
            </dd>
          </div>
        </dl>
      )}
    </Panel>
  )
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className={`mt-1 break-all text-slate-200 ${mono ? 'font-mono text-xs' : ''}`}>{value}</dd>
    </div>
  )
}
