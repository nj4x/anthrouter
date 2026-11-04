import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import useSWR from 'swr'

import type { RequestDetail } from '../api'
import { fetchJson } from '../api'
import { computeProxyDiff } from '../diff'
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

/** The DB ``error`` of an expired-Thread 404 that the proxy sent to the client as a 400 (ADR-0031). */
const EXPIRED_THREAD_ERROR = '404 — not_found_error (sent to client as 400)'
const EXPIRED_THREAD_HINT = 'Upstream Thread not found, most likely expired (no request for more than 5 minutes). '
  + 'anthrouter sent it to the client as a 400 so the client replays the conversation. This is not a model fault.'

/** Drawer text for the ADR-0033 Thread re-classification reason codes (Decision 8). */
function threadReasonText(req: { reason_code: string | null, routed_model: string | null, thread_model?: string | null }): string | null {
  switch (req.reason_code) {
    case 'thread_reclassified': {
      const next = req.routed_model ?? 'another model'
      return `The new user prompt routes to ${next}, but this Thread runs on ${req.thread_model ?? 'another model'}. `
        + `anthrouter asked the client to start a new Thread on ${next}. No upstream call was made.`
    }
    case 'thread_replay_pinned':
      return 'Replay kept on the last Thread model: its final message had only tool results.'
    case 'thread_reclassified_replay':
      return 'Replay after a re-classification: routed to the model chosen for the earlier prompt.'
    default:
      return null
  }
}

const PRE_CLASS = 'bg-slate-100 dark:bg-slate-800 p-3 rounded text-xs overflow-auto max-h-48 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-words'

/**
 * A prompt panel that shows the original text, or a diff against what the
 * proxy dispatched when a stage changed it (ADR-0024).  ``dispatched`` null or
 * equal to ``original`` means sent unchanged.  Copy always copies the original.
 * ``note`` is an extra header badge shown whether or not the text changed.
 */
function PromptPanel({ title, unchangedTitle = title, original, dispatched, pretty, note }: {
  title: string
  unchangedTitle?: string
  original: string
  dispatched: string | null | undefined
  pretty: boolean
  note?: string | null
}) {
  const [asSent, setAsSent] = useState(false)
  const changed = dispatched != null && dispatched !== original
  const shownOriginal = pretty ? prettyPrintMaybeJson(original) : original
  const shownDispatched = changed ? (pretty ? prettyPrintMaybeJson(dispatched) : dispatched) : ''
  const segments = useMemo(
    () => (changed ? computeProxyDiff(shownOriginal, shownDispatched) : null),
    [changed, shownOriginal, shownDispatched],
  )

  return (
    <section>
      <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2 flex items-center justify-between">
        <span>
          {changed ? (asSent ? `${title} (as sent)` : title) : unchangedTitle}{' '}
          <span className="normal-case font-normal text-slate-400 dark:text-slate-500">
            (~{estimateTokens(changed && asSent ? dispatched : original).toLocaleString()} tok)
          </span>
          {changed && (
            <span className="ml-2 normal-case font-normal bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 px-2 py-0.5 rounded">
              changed by proxy
            </span>
          )}
          {note && (
            <span className="ml-2 normal-case font-normal bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 px-2 py-0.5 rounded">
              {note}
            </span>
          )}
        </span>
        <span className="flex items-center">
          {changed && (
            <button
              onClick={() => setAsSent((v) => !v)}
              aria-pressed={asSent}
              className="px-1.5 py-0.5 rounded text-xs normal-case text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700 border border-slate-300 dark:border-slate-600"
            >
              {asSent ? 'diff' : 'as sent'}
            </button>
          )}
          <CopyButton text={original} />
        </span>
      </h3>
      <pre className={PRE_CLASS}>
        {!changed
          ? shownOriginal
          : asSent
            ? shownDispatched
            : segments == null
              ? shownOriginal
              : segments.map((seg, i) =>
                seg.kind === 'removed' ? (
                  <del key={i} className="line-through text-red-700/70 dark:text-red-300/70 bg-red-50 dark:bg-red-900/20">{seg.text}</del>
                ) : seg.kind === 'added' ? (
                  <ins key={i} className="no-underline bg-emerald-100 dark:bg-emerald-900/40 text-emerald-900 dark:text-emerald-100">{seg.text}</ins>
                ) : (
                  <span key={i}>{seg.text}</span>
                ),
              )}
      </pre>
    </section>
  )
}

/** Badge text for the attribution strip count, which covers every user turn (ADR-0030). */
function stripCountNote(count: number | null | undefined): string | null {
  if (!count || count <= 0) return null
  return `${count} attribution reminder${count === 1 ? '' : 's'} stripped`
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
                      {req.status === 'reclassified' && <span className="bg-sky-100 dark:bg-sky-900 text-sky-800 dark:text-sky-200 px-2 py-0.5 rounded">Reclassification</span>}
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
                  {req.error === EXPIRED_THREAD_ERROR && (
                    <p className="text-xs text-slate-600 dark:text-slate-400">{EXPIRED_THREAD_HINT}</p>
                  )}
                  {req.upstream_request_id && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Upstream request-id</dt>
                      <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono select-all break-all">{req.upstream_request_id}</dd>
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
                  {threadReasonText(req) && (
                    <div className="flex justify-between items-start">
                      <dt className="text-xs text-slate-600 dark:text-slate-400">Thread</dt>
                      <dd className="text-slate-900 dark:text-slate-200 text-xs">{threadReasonText(req)}</dd>
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

              {/* User prompt */}
              {req.user_prompt_text ? (
                <PromptPanel
                  title="User prompt"
                  original={req.user_prompt_text}
                  dispatched={req.user_prompt_dispatched_text}
                  pretty={false}
                  note={stripCountNote(req.attribution_reminders_stripped)}
                />
              ) : stripCountNote(req.attribution_reminders_stripped) && (
                <section>
                  <h3 className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide mb-2">
                    User prompt{' '}
                    <span className="normal-case font-normal bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 px-2 py-0.5 rounded">
                      {stripCountNote(req.attribution_reminders_stripped)}
                    </span>
                  </h3>
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
                <PromptPanel
                  title="System prompt"
                  unchangedTitle="System prompt (original)"
                  original={req.system_prompt_content}
                  dispatched={req.system_prompt_dispatched_content}
                  pretty
                />
              )}

              {/* Opening system messages: only when the caveman rewrite changed them (ADR-0030) */}
              {req.opening_system_content != null
                && req.opening_system_dispatched_content != null
                && req.opening_system_content !== req.opening_system_dispatched_content && (
                <PromptPanel
                  title="Opening system messages"
                  original={req.opening_system_content}
                  dispatched={req.opening_system_dispatched_content}
                  pretty={false}
                />
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
            </>
          )}
        </div>
      </div>
    </>,
    document.body,
  )
}
