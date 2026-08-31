export interface RequestRow {
  id: number
  session_id: string
  request_ts: string
  requested_model: string
  routed_model: string | null
  classification: string | null
  reason_code: string | null
  model_tier: string | null
  applied: number | null
  estimated_input_tokens: number | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_creation_tokens: number | null
  cost_estimate: number | null
  cache_savings_usd: number | null
  net_savings_usd: number | null
  classifier_overhead_usd: number | null
  classifier_model: string | null
  classifier_format: string | null
  classifier_summary_json: string | null
  classifier_raw_response: string | null
  duration_ms: number | null
  status: string
  error: string | null
  system_prompt_sha256: string | null
  system_prompt_sanitized_sha256: string | null
  tools_sha256: string | null
  user_prompt_text: string | null
  response_text: string | null
}

export interface RateLimit {
  request_ts: string
  ratelimit_requests_remaining: number | null
  ratelimit_tokens_remaining: number | null
  ratelimit_input_tokens_remaining: number | null
  ratelimit_output_tokens_remaining: number | null
  ratelimit_reset_at: string | null
}

export interface Stats {
  requests: number
  errors: number | null
  rate_limited: number | null
  sessions: number
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_creation_tokens: number | null
  cost_estimate: number | null
  cache_savings_usd: number | null
  first_request_ts: string | null
  last_request_ts: string | null
}

export interface StatusResponse {
  upstream_base_url: string
  auto_model_routing: boolean
  sanitize_system_prompt: string
  lock_requested_model: string
  db_retention_days: number
  stats: Stats
  ratelimit: RateLimit | null
}

export interface RequestsResponse {
  requests: RequestRow[]
  limit: number
  offset: number
  q: string | null
}

export interface RoutingSummary {
  total: number
  applied: number | null
  trivial: number | null
  standard: number | null
  deep: number | null
  net_savings_usd: number | null
  classifier_overhead_usd: number | null
}

export interface RoutingResponse {
  decisions: RequestRow[]
  summary: RoutingSummary
  limit: number
  offset: number
}

export interface SanitizerEvent {
  id: number
  request_id: number
  event_ts: string
  block_type: string
  is_allowlisted: number
  payload_preview: string | null
  session_id: string
  requested_model: string
  system_prompt_sha256: string | null
  system_prompt_sanitized_sha256: string | null
}

export interface SanitizerSummary {
  total_events: number
  requests_with_events: number
  allowlisted: number | null
  distinct_block_types: number
  requests_changed: number
}

export interface SanitizerResponse {
  events: SanitizerEvent[]
  summary: SanitizerSummary
  limit: number
  offset: number
}

export interface RequestDetail {
  request: RequestRow
  sanitizer_events: SanitizerEvent[]
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.error?.message ?? `Request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}
