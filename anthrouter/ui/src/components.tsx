import type { ReactNode } from 'react'

export function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded border border-slate-700 bg-slate-900">
      <h2 className="border-b border-slate-700 px-4 py-2 text-sm font-semibold text-slate-200">
        {title}
      </h2>
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded border border-slate-700 bg-slate-800 px-3 py-2">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 text-lg font-semibold text-slate-100">{value}</div>
    </div>
  )
}

export function Badge({ text, tone }: { text: string; tone: string }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}>{text}</span>
  )
}

const CLASSIFICATION_TONE: Record<string, string> = {
  trivial: 'bg-emerald-900 text-emerald-200',
  standard: 'bg-sky-900 text-sky-200',
  deep: 'bg-violet-900 text-violet-200',
}

export function ClassificationBadge({ value }: { value: string | null }) {
  if (!value) return <span className="text-slate-500">—</span>
  return <Badge text={value} tone={CLASSIFICATION_TONE[value] ?? 'bg-slate-700 text-slate-200'} />
}

const STATUS_TONE: Record<string, string> = {
  success: 'bg-emerald-900 text-emerald-200',
  error: 'bg-red-900 text-red-200',
  rate_limited: 'bg-amber-900 text-amber-200',
}

export function StatusBadge({ value }: { value: string }) {
  return <Badge text={value} tone={STATUS_TONE[value] ?? 'bg-slate-700 text-slate-200'} />
}

export function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-slate-400">
          <tr>
            {headers.map((header) => (
              <th key={header} className="px-3 py-2 font-medium">{header}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">{children}</tbody>
      </table>
    </div>
  )
}

export function Empty({ message }: { message: string }) {
  return <p className="px-3 py-6 text-center text-sm text-slate-500">{message}</p>
}

export function LoadState({ error, loading }: { error?: Error; loading: boolean }) {
  if (error) {
    return <p className="px-3 py-6 text-center text-sm text-red-400">{error.message}</p>
  }
  if (loading) {
    return <p className="px-3 py-6 text-center text-sm text-slate-500">Loading…</p>
  }
  return null
}

export function Pager({
  offset,
  limit,
  rows,
  onChange,
}: {
  offset: number
  limit: number
  rows: number
  onChange: (offset: number) => void
}) {
  return (
    <div className="mt-3 flex items-center justify-between text-sm text-slate-400">
      <button
        type="button"
        className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        Previous
      </button>
      <span>
        {rows === 0 ? 'no rows' : `rows ${offset + 1}–${offset + rows}`}
      </span>
      <button
        type="button"
        className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40"
        disabled={rows < limit}
        onClick={() => onChange(offset + limit)}
      >
        Next
      </button>
    </div>
  )
}
