import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfigModal } from '../components/ConfigModal'

const CONFIG_RESPONSE = {
  admin_token_configured: true,
  field_order: ['host', 'log_level'],
  fields: {
    host: {
      restart_required: true,
      value: '127.0.0.1',
      description: 'Network address the server binds to.',
      type: 'str',
      group: 'Server',
    },
    log_level: {
      restart_required: false,
      value: 'INFO',
      description: 'Minimum severity level for log messages.',
      type: 'str',
      enum: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
      group: 'Logging',
    },
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

  it('renders enum field as a select with correct options', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({
        admin_token_configured: true,
        field_order: ['log_level'],
        fields: {
          log_level: {
            restart_required: false,
            value: 'INFO',
            description: 'Minimum severity level for log messages.',
            type: 'str',
            enum: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
            group: 'Logging',
          },
        },
      }),
      { status: 200 },
    ))))
    render(<ConfigModal isOpen onClose={() => {}} />)
    const select = await screen.findByRole('combobox') as HTMLSelectElement
    expect(select).toBeInTheDocument()
    expect(select.value).toBe('INFO')
    const options = Array.from(select.options).map(opt => opt.value)
    expect(options).toEqual(['DEBUG', 'INFO', 'WARNING', 'ERROR'])
  })

  it('renders bool field as a checkbox', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({
        admin_token_configured: true,
        field_order: ['auto_model_routing'],
        fields: {
          auto_model_routing: {
            restart_required: false,
            value: 'false',
            description: 'Automatically route requests to a configured target model based on complexity.',
            type: 'bool',
            group: 'Model Routing',
          },
        },
      }),
      { status: 200 },
    ))))
    render(<ConfigModal isOpen onClose={() => {}} />)
    const checkbox = await screen.findByRole('checkbox') as HTMLInputElement
    expect(checkbox).toBeInTheDocument()
    expect(checkbox.checked).toBe(false)
    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(true)
  })

  it('renders numeric field with min/max attributes', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({
        admin_token_configured: true,
        field_order: ['db_retention_days'],
        fields: {
          db_retention_days: {
            restart_required: false,
            value: '30',
            description: 'Delete request rows older than this many days; 0 keeps rows forever.',
            type: 'int',
            min: 0,
            max: 365,
            group: 'Database',
          },
        },
      }),
      { status: 200 },
    ))))
    render(<ConfigModal isOpen onClose={() => {}} />)
    const input = await screen.findByRole('spinbutton') as HTMLInputElement
    expect(input).toBeInTheDocument()
    expect(input.type).toBe('number')
    expect(input.min).toBe('0')
    expect(input.max).toBe('365')
  })

  it('displays field description text', async () => {
    render(<ConfigModal isOpen onClose={() => {}} />)
    await screen.findByDisplayValue('127.0.0.1')
    // Check that description text is present
    expect(screen.getByText('Network address the server binds to.')).toBeInTheDocument()
  })

  it('renders group headings in field_order sequence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({
        admin_token_configured: true,
        field_order: ['log_level', 'log_file', 'upstream_base_url'],
        fields: {
          log_level: {
            restart_required: false,
            value: 'INFO',
            description: 'Log level description.',
            type: 'str',
            group: 'Logging',
          },
          log_file: {
            restart_required: true,
            value: '/tmp/anthrouter.log',
            description: 'Log file description.',
            type: 'str',
            group: 'Logging',
          },
          upstream_base_url: {
            restart_required: true,
            value: 'https://api.anthropic.com',
            description: 'Upstream URL description.',
            type: 'str',
            group: 'Upstream',
          },
        },
      }),
      { status: 200 },
    ))))
    render(<ConfigModal isOpen onClose={() => {}} />)
    await screen.findByDisplayValue('INFO')
    // Check group headings appear
    expect(screen.getByText('Logging')).toBeInTheDocument()
    expect(screen.getByText('Upstream')).toBeInTheDocument()
  })
})
