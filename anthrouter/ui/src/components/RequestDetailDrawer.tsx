import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import useSWR from 'swr'

import type { RequestDetail } from '../api'
import { fetchJson } from '../api'
import { count, estimateTokens, prettyPrintMaybeJson } from '../format'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <button
      onClick={handleCopy}
      className="ml-2 px-1.5 py-0.5 rounded text-xs text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700 border border-transparent hover:border-slate-400 dark:hover:border-slate-600 transition-colors"
    >
      {copied ? 'Copied!' : 'Copy'}
    </button>
  )
}

function cacheHitRatio(d: RequestDetail): string {
  const cr = d.request.cache_read_tokens ?? 0
  const inp = d.request.input_tokens ?? 0
  const denom = inp + cr
  if (denom === 0) return '—'
  return ((cr / denom) * 100).toFixed(1) + '%'
}


interface Props {
  requestId: number | null
  onClose: () => void
}

export function RequestDetailDrawer({ requestId, onClose }: Props) {
  const { data, error, isLoading } = useSWR<RequestDetail>(
    requestId != null ? `/admin/requests/${requestId}` : null,
    fetchJson,
  )

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  const isOpen = requestId != null
  const req = data?.request

  return createPortal(
    <>
      <div
        className={`fixed inset-0 bg-black z-40 transition-opacity duration-200 dark:opacity-50 ${isOpen ? 'opacity-25 dark:opacity-50' : 'opacity-0 dark:opacity-0 pointer-events-none'}`}
        onClick={onClose}
      />

      <div
        className={`fixed top-0 right-0 h-full w-[90vw] sm:w-[60vw] max-w-full bg-white dark:bg-slate-900 shadow-2xl z-50 overflow-y-auto transform transition-transform duration-200 ${isOpen ? 'translate-x-0' : 'translate-x-full'}`}
      >
        <div className="sticky top-0 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 px-5 py-3 flex items-center justify-between z-10">
          <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            {data
              ? `Request #${data.request.id} · ${count(data.request.input_tokens)} in / ${count(data.request.output_tokens)} out tok (DB)`
              : 'Request Detail'}
          </span>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xl leading-none w-8 h-8 flex items-center justify-center"
          >
            ×
          </button>
        </div>

        <div className="px-5 py-4 space-y-6 text-sm">
          {isLoading && <div className="text-slate-600 dark:text-slate-500">Loading…</div>}

          {error && !isLoading && (
            <div className="bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-200 p-4 rounded">
              {error.message}
            </div>
          )}

          {req && (
            <>
              {/* Summary */}
              <section>
                <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-3">
                  Summary
                </h3>
                <dl className="space-y-2">
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Time</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">
                      {new Date(req.request_ts).toLocaleString()}
                    </dd>
                  </div>
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Status</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs">
                      {req.status === 'success' && <span className="bg-emerald-100 dark:bg-emerald-900 text-emerald-800 dark:text-emerald-200 px-2 py-0.5 rounded">Success</span>}
                      {req.status === 'rate_limited' && <span className="bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 px-2 py-0.5 rounded">Rate Limited</span>}
                      {req.status === 'error' && <span className="bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200 px-2 py-0.5 rounded">Error</span>}
                    </dd>
                  </div>
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Duration</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs">
                      {req.duration_ms != null ? `${req.duration_ms.toLocaleString()}ms` : '—'}
                    </dd>
                  </div>
                  {req.error && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Error</dt>
                      <dd className="text-red-700 dark:text-red-200 text-xs">{req.error}</dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* Routing */}
              <section>
                <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-3">
                  Model Routing
                </h3>
                <dl className="space-y-2">
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Requested</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">{req.requested_model}</dd>
                  </div>
                  {req.routed_model && req.routed_model !== req.requested_model && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Routed to</dt>
                      <dd className="text-emerald-700 dark:text-emerald-200 text-xs font-mono bg-emerald-50 dark:bg-emerald-900/20 px-2 py-1 rounded">
                        {req.routed_model}
                      </dd>
                    </div>
                  )}
                  {req.classification && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Classification</dt>
                      <dd className="text-xs">
                        {req.classification === 'trivial' && <span className="bg-emerald-100 dark:bg-emerald-900 text-emerald-800 dark:text-emerald-200 px-2 py-0.5 rounded inline-block">trivial</span>}
                        {req.classification === 'standard' && <span className="bg-sky-100 dark:bg-sky-900 text-sky-800 dark:text-sky-200 px-2 py-0.5 rounded inline-block">standard</span>}
                        {req.classification === 'deep' && <span className="bg-violet-100 dark:bg-violet-900 text-violet-800 dark:text-violet-200 px-2 py-0.5 rounded inline-block">deep</span>}
                      </dd>
                    </div>
                  )}
                  {req.reason_code && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Reason</dt>
                      <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">{req.reason_code}</dd>
                    </div>
                  )}
                  {(req.user_prompt_score != null || req.system_prompt_score != null) && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Scores</dt>
                      <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">
                        u:{req.user_prompt_score != null ? Math.round(req.user_prompt_score) : '—'} s:{req.system_prompt_score != null ? Math.round(req.system_prompt_score) : '—'}
                        {req.routing_weighted_score != null && ` → blended ${Math.round(req.routing_weighted_score)}`}
                      </dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* Tokens & Cost */}
              <section>
                <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-3">
                  Tokens & Cost
                </h3>
                <dl className="space-y-2">
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Input / Output</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs">
                      {req.input_tokens?.toLocaleString() ?? '—'} / {req.output_tokens?.toLocaleString() ?? '—'}
                    </dd>
                  </div>
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Cache Read</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs">
                      {req.cache_read_tokens?.toLocaleString() ?? '—'} ({cacheHitRatio(data!)})
                    </dd>
                  </div>
                  <div className="flex justify-between items-start">
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Cost</dt>
                    <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">
                      ${req.cost_estimate?.toFixed(4) ?? '—'}
                    </dd>
                  </div>
                  {req.cache_savings_usd && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Cache Savings</dt>
                      <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">
                        ${req.cache_savings_usd.toFixed(4)}
                      </dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* Prompts */}
              <section>
                <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-3">
                  Prompt Hashes
                </h3>
                <dl className="space-y-2">
                  <div>
                    <dt className="text-xs text-slate-600 dark:text-slate-400 mb-1">System</dt>
                    <dd className="flex items-center gap-1">
                      <code className="text-xs font-mono text-slate-600 dark:text-slate-400 truncate bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded flex-1">
                        {req.system_prompt_sha256 ?? '—'}
                      </code>
                      {req.system_prompt_sha256 && <CopyButton text={req.system_prompt_sha256} />}
                    </dd>
                  </div>
                  {req.system_prompt_sanitized_sha256 && (
                    <div>
                      <dt className="text-xs text-slate-600 dark:text-slate-400 mb-1">Sanitized</dt>
                      <dd className="flex items-center gap-1">
                        <code className="text-xs font-mono text-slate-600 dark:text-slate-400 truncate bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded flex-1">
                          {req.system_prompt_sanitized_sha256}
                        </code>
                        <CopyButton text={req.system_prompt_sanitized_sha256} />
                      </dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* User prompt */}
              {req.user_prompt_text && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2 flex items-center justify-between">
                    <span>
                      User prompt{' '}
                      <span className="normal-case font-normal text-slate-400 dark:text-slate-500">
                        (~{estimateTokens(req.user_prompt_text).toLocaleString()} tok)
                      </span>
                    </span>
                    <CopyButton text={req.user_prompt_text} />
                  </h3>
                  <pre className="bg-slate-100 dark:bg-slate-800 p-3 rounded text-xs overflow-auto max-h-48 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-words">
                    {req.user_prompt_text}
                  </pre>
                </section>
              )}

              {/* Model response */}
              {req.response_text && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2 flex items-center justify-between">
                    <span>Model response</span>
                    <CopyButton text={req.response_text} />
                  </h3>
                  <pre className="bg-slate-100 dark:bg-slate-800 p-3 rounded text-xs overflow-auto max-h-48 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-words">
                    {req.response_text}
                  </pre>
                </section>
              )}

              {/* System prompt */}
              {req.system_prompt_content && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2 flex items-center justify-between">
                    <span>
                      System prompt (original){' '}
                      <span className="normal-case font-normal text-slate-400 dark:text-slate-500">
                        (~{estimateTokens(req.system_prompt_content).toLocaleString()} tok)
                      </span>
                    </span>
                    <CopyButton text={req.system_prompt_content} />
                  </h3>
                  <pre className="bg-slate-100 dark:bg-slate-800 p-3 rounded text-xs overflow-auto max-h-48 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-words">
                    {prettyPrintMaybeJson(req.system_prompt_content)}
                  </pre>
                </section>
              )}

              {/* Tools */}
              {req.tools_content && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2 flex items-center justify-between">
                    <span>
                      Tools{' '}
                      <span className="normal-case font-normal text-slate-400 dark:text-slate-500">
                        (~{estimateTokens(req.tools_content).toLocaleString()} tok)
                      </span>
                    </span>
                    <CopyButton text={req.tools_content} />
                  </h3>
                  <pre className="bg-slate-100 dark:bg-slate-800 p-3 rounded text-xs overflow-auto max-h-48 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-words">
                    {prettyPrintMaybeJson(req.tools_content)}
                  </pre>
                </section>
              )}

              {/* Stripped blocks */}
              {data?.sanitizer_events && data.sanitizer_events.length > 0 && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-3">
                    Stripped Blocks ({data.sanitizer_events.length})
                  </h3>
                  <div className="space-y-3">
                    {data.sanitizer_events.map((event) => (
                      <div
                        key={event.id}
                        className="border border-slate-200 dark:border-slate-700 rounded p-3 bg-slate-50 dark:bg-slate-800"
                      >
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-xs font-mono text-slate-600 dark:text-slate-400">
                            {event.block_type}
                          </span>
                          <span
                            className={`text-xs px-2 py-0.5 rounded ${
                              event.is_allowlisted
                                ? 'bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200'
                                : 'bg-slate-200 dark:bg-slate-700 text-slate-800 dark:text-slate-200'
                            }`}
                          >
                            {event.is_allowlisted ? 'stripped' : 'flagged'}
                          </span>
                        </div>
                        <pre className="text-xs bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-600 p-2 rounded max-h-24 overflow-auto text-slate-700 dark:text-slate-300 whitespace-pre-wrap break-words group cursor-text">
                          {prettyPrintMaybeJson(event.payload_full || event.payload_preview || '—')}
                          {(event.payload_full || event.payload_preview) && (
                            <CopyButton text={event.payload_full || event.payload_preview || ''} />
                          )}
                        </pre>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </>,
    document.body,
  )
}
