import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { modelLabel, sessionLabel, usd } from '../format'

const REQUEST = {
  id: 1,
  session_id: JSON.stringify({ session_id: 'abcdef01-2222-3333-4444-555555555555' }),
  request_ts: '2026-08-31T10:00:00.000Z',
  requested_model: 'claude-sonnet-4-5-20250929',
  routed_model: 'claude-haiku-4-5-20251001',
  classification: 'trivial',
  reason_code: 'classified',
  model_tier: 'haiku',
  applied: 1,
  estimated_input_tokens: 120,
  input_tokens: 100,
  output_tokens: 20,
  cache_read_tokens: 900,
  cache_creation_tokens: null,
  cost_estimate: 0.0012,
  cache_savings_usd: 0.002,
  net_savings_usd: 0.001,
  classifier_overhead_usd: 0.0001,
  classifier_model: 'claude-haiku-4-5-20251001',
  classifier_format: 'label',
  classifier_summary_json: '{"task":"trivial"}',
  classifier_raw_response: 'trivial',
  duration_ms: 420,
  status: 'success',
  error: null,
  system_prompt_sha256: 'aaa',
  system_prompt_sanitized_sha256: 'bbb',
  tools_sha256: null,
  user_prompt_text: 'fix a typo',
  response_text: 'done',
  system_prompt_content: '{"role":"system","text":"be helpful"}',
  system_prompt_sanitized_content: '{"role":"system","text":"be helpful"}',
  tools_content: '[{"name":"get_weather"}]',
}

const SANITIZER_EVENT = {
  id: 7, request_id: 1, event_ts: '2026-08-31T10:00:00.000Z',
  block_type: 'cc_prompt_id', is_allowlisted: 1,
  payload_preview: '{"cc_prompt_id":"0d1f"}',
  payload_full: '{"cc_prompt_id":"0d1f2e3a","ts":1234567890}',
  session_id: REQUEST.session_id,
  requested_model: REQUEST.requested_model,
  system_prompt_sha256: 'aaa', system_prompt_sanitized_sha256: 'bbb',
}

const ROUTES: Record<string, unknown> = {
  '/admin/requests': { requests: [REQUEST], limit: 50, offset: 0, q: null },
  '/admin/requests/1': { request: REQUEST, sanitizer_events: [SANITIZER_EVENT] },
  '/admin/routing': {
    decisions: [REQUEST],
    summary: {
      total: 4, applied: 3, trivial: 2, standard: 1, deep: 1,
      net_savings_usd: 0.01, classifier_overhead_usd: 0.001,
    },
    limit: 50,
    offset: 0,
  },
  '/admin/sanitizer-events': {
    events: [{
      id: 7, request_id: 1, event_ts: '2026-08-31T10:00:00.000Z',
      block_type: 'cc_prompt_id', is_allowlisted: 1,
      payload_preview: 'cc_prompt_id: 0d1f', session_id: REQUEST.session_id,
      requested_model: REQUEST.requested_model,
      system_prompt_sha256: 'aaa', system_prompt_sanitized_sha256: 'bbb',
    }],
    summary: {
      total_events: 1, requests_with_events: 1, allowlisted: 1,
      distinct_block_types: 1, requests_changed: 1,
    },
    limit: 50,
    offset: 0,
  },
  '/admin/status': {
    upstream_base_url: 'https://api.anthropic.com',
    auto_model_routing: true,
    sanitize_system_prompt: 'strip',
    lock_requested_model: 'off',
    db_retention_days: 30,
    stats: {
      requests: 4, errors: 1, rate_limited: 0, sessions: 2,
      input_tokens: 400, output_tokens: 80, cache_read_tokens: 900,
      cache_creation_tokens: 0, cost_estimate: 0.02, cache_savings_usd: 0.004,
      first_request_ts: '2026-08-31T09:00:00.000Z',
      last_request_ts: '2026-08-31T10:00:00.000Z',
    },
    ratelimit: {
      request_ts: '2026-08-31T10:00:00.000Z',
      ratelimit_requests_remaining: 42,
      ratelimit_tokens_remaining: 13000,
      ratelimit_input_tokens_remaining: null,
      ratelimit_output_tokens_remaining: null,
      ratelimit_reset_at: '2026-08-31T12:00:00Z',
    },
  },
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: string) => {
    const path = input.split('?')[0]
    const body = ROUTES[path]
    if (body === undefined) {
      return Promise.resolve(new Response(
        JSON.stringify({ error: { message: `no stub for ${path}` } }),
        { status: 404 },
      ))
    }
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function switchTo(label: string, user = screen) {
  const { fireEvent } = await import('@testing-library/react')
  fireEvent.click(user.getByRole('button', { name: label }))
}

describe('formatters', () => {
  it('reads the nested session_id out of the metadata blob', () => {
    expect(sessionLabel(REQUEST.session_id)).toBe('abcdef01')
    expect(sessionLabel('not-json-at-all')).toBe('not-json')
  })

  it('drops the vendor prefix and date suffix from a model id', () => {
    expect(modelLabel('claude-haiku-4-5-20251001')).toBe('haiku-4-5')
    expect(modelLabel(null)).toBe('—')
  })

  it('renders sub-cent cost at five decimals so it is not shown as zero', () => {
    expect(usd(0.000123)).toBe('$0.00012')
    expect(usd(null)).toBe('—')
  })
})

describe('App', () => {
  it('shows the request log with the routed model called out', async () => {
    render(<App />)
    await switchTo('Requests')
    await waitFor(() => expect(screen.getByText('fix a typo')).toBeInTheDocument())
    expect(screen.getByText('as haiku-4-5')).toBeInTheDocument()
    expect(screen.getByText('success')).toBeInTheDocument()
  })

  it('shows the routing summary and each decision', async () => {
    render(<App />)
    await switchTo('Routing')
    await waitFor(() => expect(screen.getByText('Routing summary')).toBeInTheDocument())
    expect(await screen.findByText('classified')).toBeInTheDocument()
    // 3 of 4 decisions applied.
    expect(screen.getByText('75%')).toBeInTheDocument()
  })

  it('labels an allowlisted sanitizer event as stripped', async () => {
    render(<App />)
    await switchTo('Sanitizer')
    expect(await screen.findByText('cc_prompt_id')).toBeInTheDocument()
    expect(screen.getByText('stripped')).toBeInTheDocument()
  })

  it('shows the usage totals and the configuration', async () => {
    render(<App />)
    await switchTo('Usage')
    expect(await screen.findByText('Totals')).toBeInTheDocument()
    expect(screen.getByText('https://api.anthropic.com')).toBeInTheDocument()
  })

  it('opens the request-detail drawer with DB totals, token estimates and pretty-printed JSON', async () => {
    const { fireEvent } = await import('@testing-library/react')
    render(<App />)
    await switchTo('Requests')
    await waitFor(() => expect(screen.getByText('fix a typo')).toBeInTheDocument())
    fireEvent.click(screen.getByText('fix a typo'))

    // DB totals in the sticky header.
    expect(await screen.findByText('Request #1 · 100 in / 20 out tok (DB)')).toBeInTheDocument()

    // Per-heading token estimates (chars/4): system prompt is 37 chars, tools is 24 chars.
    expect(screen.getByText('(~10 tok)', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('(~6 tok)', { exact: false })).toBeInTheDocument()

    // No "sanitized" system-prompt section — only the original.
    expect(screen.getByText(/System prompt \(original\)/)).toBeInTheDocument()
    expect(screen.queryByText(/System prompt \(sanitized/)).not.toBeInTheDocument()

    // Pretty-printed JSON: indented, multi-line, not the single-line source.
    expect(screen.queryByText(REQUEST.system_prompt_content)).not.toBeInTheDocument()
    expect(screen.getByText(/"role": "system"/)).toBeInTheDocument()
    expect(screen.getByText(/"cc_prompt_id": "0d1f2e3a"/)).toBeInTheDocument()
  })
})
