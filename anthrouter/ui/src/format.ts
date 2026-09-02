export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value < 0.01 && value > 0 ? `$${value.toFixed(5)}` : `$${value.toFixed(4)}`
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString()
}

export function timestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value.endsWith('Z') ? value : `${value}Z`)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

/** Session keys are the whole metadata.user_id blob; show only a readable head. */
export function sessionLabel(sessionId: string): string {
  try {
    const parsed = JSON.parse(sessionId) as { session_id?: string }
    if (parsed.session_id) return parsed.session_id.slice(0, 8)
  } catch {
    // Not JSON — fall through to the raw prefix.
  }
  return sessionId.slice(0, 8)
}

export function modelLabel(model: string | null | undefined): string {
  if (!model) return '—'
  return model.replace(/^claude-/, '').replace(/-\d{8}$/, '')
}

/** Rough token estimate matching the backend's chars/4 heuristic (model_router.py). */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4)
}

/** Pretty-prints JSON payloads for readability; returns the original text unchanged if it isn't JSON. */
export function prettyPrintMaybeJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}
