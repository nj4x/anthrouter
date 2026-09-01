import useSWR from 'swr'
import type { OAuthToken } from '../api'
import { fetchJson } from '../api'

function formatAge(ageSecs: number | null | undefined): string | null {
  if (ageSecs == null) return null
  if (ageSecs < 60) return 'just now'
  const m = Math.floor(ageSecs / 60)
  return `${m}m ago`
}

export function OAuthCard() {
  const { data } = useSWR<{ oauth_token: OAuthToken | null }>(
    '/admin/oauth-usage',
    fetchJson,
    { refreshInterval: 60000 },
  )

  if (!data?.oauth_token) {
    return (
      <p className="text-xs text-slate-600 dark:text-slate-400">
        No OAuth token usage yet. Make a request with OAuth bearer auth to populate.
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

  return (
    <div className="bg-slate-50 dark:bg-slate-800 rounded-lg p-4 space-y-3 border border-slate-200 dark:border-slate-700">
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
            {token.month_elapsed_pct != null && token.burn_pct > token.month_elapsed_pct && (
              <div
                className="absolute inset-y-0 bg-red-500"
                style={{
                  left: `${token.month_elapsed_pct}%`,
                  width: `${token.burn_pct - token.month_elapsed_pct}%`,
                }}
              />
            )}
            {token.month_elapsed_pct != null &&
              token.month_elapsed_pct - token.burn_pct > 0.5 && (
                <div
                  className="absolute inset-y-0 bg-emerald-500 opacity-40"
                  style={{
                    left: `${token.burn_pct}%`,
                    width: `${token.month_elapsed_pct - token.burn_pct}%`,
                  }}
                />
              )}
          </div>
        )}
      </div>
    </div>
  )
}
