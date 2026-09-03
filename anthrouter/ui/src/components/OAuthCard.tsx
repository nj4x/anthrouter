import { useState } from 'react'
import useSWR from 'swr'
import type { OAuthToken } from '../api'
import { fetchJson } from '../api'

type MeterMode = 'workdays' | 'calendar'

const STORAGE_KEY = 'anthrouter.oauthMeterMode'

function getStoredMode(): MeterMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'workdays' || stored === 'calendar') {
      return stored
    }
  } catch {
    // Safari private mode throws on access; the meter must still render.
  }
  return 'workdays'
}

function formatAge(ageSecs: number | null | undefined): string | null {
  if (ageSecs == null) return null
  if (ageSecs < 60) return 'just now'
  const m = Math.floor(ageSecs / 60)
  return `${m}m ago`
}

export function OAuthCard() {
  const { data } = useSWR<{ oauth_token: OAuthToken | null; message?: string; retry_after_seconds?: number }>(
    '/admin/oauth-usage',
    fetchJson,
    { refreshInterval: 5000 },
  )

  const [mode, setMode] = useState<MeterMode>(() => getStoredMode())

  const handleModeChange = (newMode: MeterMode) => {
    setMode(newMode)
    try {
      localStorage.setItem(STORAGE_KEY, newMode)
    } catch {
      // Losing the persisted choice is preferable to breaking the toggle.
    }
  }

  if (!data?.oauth_token) {
    return (
      <p className="text-xs text-slate-600 dark:text-slate-400">
        {data?.message ?? 'No OAuth token usage yet. Make a request with OAuth bearer auth to populate.'}
      </p>
    )
  }

  const token = data.oauth_token
  const pct = Math.min(token.burn_pct ?? 0, 100)
  const ageLabel = formatAge(token.usage_age_seconds)
  const statusLabel = token.monthly_blocked
    ? 'Spend cap reached'
    : token.cooldown_remaining_seconds > 0
      ? `Cooling down ${Math.ceil(token.cooldown_remaining_seconds)}s`
      : token.eligible
        ? 'Eligible'
        : 'Not eligible'
  const statusColor = token.monthly_blocked
    ? 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200'
    : token.cooldown_remaining_seconds > 0
      ? 'bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200'
      : token.eligible
        ? 'bg-emerald-100 dark:bg-emerald-900 text-emerald-800 dark:text-emerald-200'
        : 'bg-slate-100 dark:bg-slate-800 text-slate-800 dark:text-slate-200'

  const baseline = mode === 'workdays'
    ? token.workday_elapsed_pct
    : token.calendar_elapsed_pct

  const displayBaseline = baseline ?? token.month_elapsed_pct ?? null

  const periodLabel = (() => {
    if (!token.period_start || !token.period_end) return null
    const start = new Date(token.period_start)
    const month = start.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
    if (mode === 'workdays' && token.period_workday_count != null && token.workday_timezone) {
      return `${token.period_workday_count} workdays · ${token.workday_timezone} · ${month}`
    }
    if (mode === 'calendar') {
      return `Calendar month · ${month}`
    }
    return null
  })()

  return (
    <div className="bg-slate-50 dark:bg-slate-800 rounded-lg p-4 space-y-3 border border-slate-200 dark:border-slate-700">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
            Anthropic-OAuth token
          </span>
          {ageLabel && (
            <span className="text-xs text-slate-600 dark:text-slate-400">
              updated {ageLabel}
              {token.usage_stale ? ' (stale)' : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2" role="group" aria-label="Meter mode">
          <button
            type="button"
            className={`px-2 py-0.5 text-xs rounded font-medium border ${
              mode === 'workdays'
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-300 border-slate-300 dark:border-slate-600'
            }`}
            aria-pressed={mode === 'workdays'}
            onClick={() => handleModeChange('workdays')}
          >
            Workdays
          </button>
          <button
            type="button"
            className={`px-2 py-0.5 text-xs rounded font-medium border ${
              mode === 'calendar'
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-300 border-slate-300 dark:border-slate-600'
            }`}
            aria-pressed={mode === 'calendar'}
            onClick={() => handleModeChange('calendar')}
          >
            Calendar
          </button>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusColor}`}>
          {statusLabel}
        </span>
      </div>
      <div>
        <div className="text-xs text-slate-600 dark:text-slate-400 mb-1">Monthly quota</div>
        <div className="text-sm text-slate-900 dark:text-slate-100">
          {token.burn_pct != null ? `${token.burn_pct.toFixed(0)}% used` : '—'}
        </div>
        {token.used_usd != null && token.total_usd != null && (
          <div className="text-xs text-slate-600 dark:text-slate-400 mt-0.5">
            ${token.used_usd.toFixed(2)} of ${token.total_usd.toFixed(2)}
          </div>
        )}
        {token.burn_pct != null && (
          <div className="mt-1 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden relative">
            <div
              className={`absolute inset-y-0 left-0 transition-all ${
                token.burn_pct >= 100
                  ? 'bg-red-500'
                  : token.burn_pct >= 80
                    ? 'bg-amber-500'
                    : 'bg-blue-500'
              }`}
              style={{ width: `${pct}%` }}
            />
            {displayBaseline != null && token.burn_pct > displayBaseline && (
              <div
                className="absolute inset-y-0 bg-red-500"
                style={{
                  left: `${displayBaseline}%`,
                  width: `${token.burn_pct - displayBaseline}%`,
                }}
              />
            )}
            {displayBaseline != null &&
              displayBaseline - token.burn_pct > 0.5 && (
                <div
                  className="absolute inset-y-0 bg-emerald-500 opacity-40"
                  style={{
                    left: `${token.burn_pct}%`,
                    width: `${displayBaseline - token.burn_pct}%`,
                  }}
                />
              )}
          </div>
        )}
        {displayBaseline != null && (
          <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">
            pace {displayBaseline.toFixed(1)}%
            {periodLabel && <span className="ml-2 text-slate-400 dark:text-slate-500">· {periodLabel}</span>}
          </div>
        )}
      </div>
    </div>
  )
}
