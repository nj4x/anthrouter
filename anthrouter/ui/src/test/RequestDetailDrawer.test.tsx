import { fireEvent, render, screen } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RequestDetailDrawer } from '../components/RequestDetailDrawer'
import * as diff from '../diff'

const BASE = {
  id: 1, session_id: 's', request_ts: '2026-08-31T10:00:00.000Z',
  requested_model: 'claude-sonnet-4-5', routed_model: null, status: 'success',
  input_tokens: 1, output_tokens: 1, cache_read_tokens: 0,
  system_prompt_content: null, tools_content: null, response_text: null,
}

let request: Record<string, unknown> = BASE

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(
    new Response(JSON.stringify({ request, sanitizer_events: [] }), { status: 200 }),
  )))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function show(fields: Record<string, unknown>) {
  request = { ...BASE, ...fields }
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <RequestDetailDrawer requestId={1} onClose={() => {}} />
    </SWRConfig>,
  )
}

describe('RequestDetailDrawer proxy diff', () => {
  it('renders an unchanged user prompt as plain text with no badge', async () => {
    show({ user_prompt_text: 'fix a typo', user_prompt_dispatched_text: null })
    expect(await screen.findByText('fix a typo')).toBeInTheDocument()
    expect(screen.queryByText('changed by proxy')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'as sent' })).not.toBeInTheDocument()
  })

  it('strikes through text the proxy removed from the user prompt', async () => {
    const { container } = show({
      user_prompt_text: 'q <system-reminder>attr</system-reminder>',
      user_prompt_dispatched_text: 'q ',
    })
    expect(await screen.findByText('changed by proxy')).toBeInTheDocument()
    const removed = [...container.ownerDocument.querySelectorAll('del')].map((d) => d.textContent).join('')
    expect(removed).toContain('<system-reminder>attr</system-reminder>')
  })

  it('shows caveman edits as removed old text plus inserted new text in the system prompt', async () => {
    show({
      system_prompt_content: 'Write full sentences.',
      system_prompt_dispatched_content: 'Write fragments.',
    })
    expect(await screen.findByText('changed by proxy')).toBeInTheDocument()
    expect(document.querySelector('del')?.textContent).toContain('full sentences')
    expect(document.querySelector('ins')?.textContent).toContain('fragments')
    expect(screen.getByText(/^System prompt/)).toBeInTheDocument()
  })

  it('toggles to the plain dispatched text with "as sent"', async () => {
    show({ user_prompt_text: 'a b c', user_prompt_dispatched_text: 'a c' })
    fireEvent.click(await screen.findByRole('button', { name: 'as sent' }))
    expect(screen.getByText('a c')).toBeInTheDocument()
    expect(document.querySelector('del')).toBeNull()
    expect(screen.getByText(/User prompt \(as sent\)/)).toBeInTheDocument()
  })

  it('copies the original, not the dispatched text', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    show({ user_prompt_text: 'original text', user_prompt_dispatched_text: 'sent' })
    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('original text')
  })

  it('falls back to the plain original plus badge when the diff is skipped', async () => {
    vi.spyOn(diff, 'computeProxyDiff').mockReturnValue(null)
    show({ user_prompt_text: 'orig words', user_prompt_dispatched_text: 'sent words' })
    expect(await screen.findByText('orig words')).toBeInTheDocument()
    expect(screen.getByText('changed by proxy')).toBeInTheDocument()
    expect(document.querySelector('del')).toBeNull()
  })

  it('treats a dispatched text equal to the original as unchanged', async () => {
    show({ system_prompt_content: 'same', system_prompt_dispatched_content: 'same' })
    expect(await screen.findByText(/System prompt \(original\)/)).toBeInTheDocument()
    expect(screen.queryByText('changed by proxy')).not.toBeInTheDocument()
  })
})

describe('RequestDetailDrawer upstream request-id', () => {
  it('shows the upstream request-id when one was recorded', async () => {
    show({ status: 'rate_limited', error: '429 — rate_limit_error', upstream_request_id: 'req_011CXYZ' })
    expect(await screen.findByText('req_011CXYZ')).toBeInTheDocument()
    expect(screen.getByText('Upstream request-id')).toBeInTheDocument()
  })

  it('hides the row when no request-id was recorded', async () => {
    show({ upstream_request_id: null })
    expect(await screen.findByText('Success')).toBeInTheDocument()
    expect(screen.queryByText('Upstream request-id')).not.toBeInTheDocument()
  })
})

describe('RequestDetailDrawer opening system messages (ADR-0030)', () => {
  it('diffs the opening system text when the caveman rewrite changed it', async () => {
    const { container } = show({
      opening_system_content: 'Answer in full sentences.',
      opening_system_dispatched_content: 'Answer in fragments.',
    })
    expect(await screen.findByText('Opening system messages')).toBeInTheDocument()
    expect(screen.getByText('changed by proxy')).toBeInTheDocument()
    const removed = [...container.ownerDocument.querySelectorAll('del')].map((d) => d.textContent).join('')
    expect(removed).toContain('full sentences')
    fireEvent.click(screen.getByRole('button', { name: 'as sent' }))
    expect(screen.getByText('Answer in fragments.')).toBeInTheDocument()
  })

  it.each([
    ['both null', { opening_system_content: null, opening_system_dispatched_content: null }],
    ['original missing', { opening_system_content: null, opening_system_dispatched_content: 'x' }],
    ['dispatched missing', { opening_system_content: 'x', opening_system_dispatched_content: null }],
    ['equal', { opening_system_content: 'x', opening_system_dispatched_content: 'x' }],
  ])('shows no panel when %s', async (_name, fields) => {
    show({ ...fields, user_prompt_text: 'hello' })
    expect(await screen.findByText('hello')).toBeInTheDocument()
    expect(screen.queryByText('Opening system messages')).not.toBeInTheDocument()
  })
})

describe('RequestDetailDrawer attribution strip count (ADR-0030)', () => {
  it('shows the count on an unchanged user prompt', async () => {
    show({ user_prompt_text: 'later turn', user_prompt_dispatched_text: null, attribution_reminders_stripped: 3 })
    expect(await screen.findByText('3 attribution reminders stripped')).toBeInTheDocument()
    expect(screen.queryByText('changed by proxy')).not.toBeInTheDocument()
  })

  it('shows the count next to the changed-by-proxy badge', async () => {
    show({ user_prompt_text: 'q <r>', user_prompt_dispatched_text: 'q ', attribution_reminders_stripped: 1 })
    expect(await screen.findByText('1 attribution reminder stripped')).toBeInTheDocument()
    expect(screen.getByText('changed by proxy')).toBeInTheDocument()
  })

  it('shows no count badge when the count is null', async () => {
    show({ user_prompt_text: 'plain', attribution_reminders_stripped: null })
    expect(await screen.findByText('plain')).toBeInTheDocument()
    expect(screen.queryByText(/attribution reminders? stripped/)).not.toBeInTheDocument()
  })
})

describe('RequestDetailDrawer strip count without user text (ADR-0030)', () => {
  it('shows the count when the last user message has no text', async () => {
    show({ user_prompt_text: null, attribution_reminders_stripped: 2 })
    expect(await screen.findByText('2 attribution reminders stripped')).toBeInTheDocument()
  })
})
