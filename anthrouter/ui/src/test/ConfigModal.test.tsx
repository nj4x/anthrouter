import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfigModal } from '../components/ConfigModal'

const CONFIG_RESPONSE = {
  admin_token_configured: true,
  fields: {
    host: { restart_required: true, value: '127.0.0.1' },
    log_level: { restart_required: false, value: 'INFO' },
  },
}

function stubFetch(postHandler: (body: unknown) => Response) {
  vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return Promise.resolve(postHandler(JSON.parse(init.body as string)))
    }
    return Promise.resolve(new Response(JSON.stringify(CONFIG_RESPONSE), { status: 200 }))
  }))
}

beforeEach(() => {
  stubFetch(() => new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ConfigModal', () => {
  it('renders fetched fields as editable inputs', async () => {
    render(<ConfigModal isOpen onClose={() => {}} />)
    const input = await screen.findByDisplayValue('127.0.0.1') as HTMLInputElement
    expect(input.readOnly).toBe(false)
    fireEvent.change(input, { target: { value: '10.0.0.9' } })
    expect(input.value).toBe('10.0.0.9')
  })

  it('saves the full field map with the admin token header, and retains values without refetching', async () => {
    let capturedInit: RequestInit | undefined
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        capturedInit = init
        return Promise.resolve(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(CONFIG_RESPONSE), { status: 200 }))
    }))

    render(<ConfigModal isOpen onClose={() => {}} />)
    const hostInput = await screen.findByDisplayValue('127.0.0.1') as HTMLInputElement
    fireEvent.change(hostInput, { target: { value: '10.0.0.9' } })

    const tokenInput = screen.getByPlaceholderText('Enter admin token to enable configuration edits')
    fireEvent.change(tokenInput, { target: { value: 'sekret' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByText('Configuration saved.')).toBeInTheDocument())

    expect(capturedInit).toBeDefined()
    expect((capturedInit!.headers as Record<string, string>)['X-Admin-Token']).toBe('sekret')
    const body = JSON.parse(capturedInit!.body as string)
    expect(body).toEqual({ host: '10.0.0.9', log_level: 'INFO' })
    expect(body).not.toHaveProperty('admin_token_configured')

    // Edited value is still shown — no re-fetch happened after save.
    expect((screen.getByDisplayValue('10.0.0.9') as HTMLInputElement).value).toBe('10.0.0.9')

    // Restart-required notice appears for the file-editable field that was saved.
    expect(screen.getByText('Takes effect on next restart via the installed launcher.')).toBeInTheDocument()
  })

  it('surfaces a 403 error inline and keeps the modal open', async () => {
    stubFetch(() => new Response(
      JSON.stringify({ error: { message: 'Invalid or missing admin token' } }),
      { status: 403 },
    ))
    const onClose = vi.fn()
    render(<ConfigModal isOpen onClose={onClose} />)
    await screen.findByDisplayValue('127.0.0.1')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByText('Invalid or missing admin token')).toBeInTheDocument())
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('surfaces a 400 validation error inline', async () => {
    stubFetch(() => new Response(
      JSON.stringify({ error: { message: "field 'port' must be an integer, got 'nope'" } }),
      { status: 400 },
    ))
    render(<ConfigModal isOpen onClose={() => {}} />)
    await screen.findByDisplayValue('127.0.0.1')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(screen.getByText("field 'port' must be an integer, got 'nope'")).toBeInTheDocument())
  })

  it('disables Save when no admin token is configured', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({ ...CONFIG_RESPONSE, admin_token_configured: false }),
      { status: 200 },
    ))))
    render(<ConfigModal isOpen onClose={() => {}} />)
    await screen.findByDisplayValue('127.0.0.1')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})
