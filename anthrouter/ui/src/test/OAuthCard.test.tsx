import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OAuthCard } from '../components/OAuthCard'

interface Token {
  burn_pct: number | null
  used_usd: number | null
  total_usd: number | null
  month_elapsed_pct: number | null
  workday_elapsed_pct: number | null
  calendar_elapsed_pct: number | null
  workday_timezone: string | null
  period_start: string | null
  period_end: string | null
  period_workday_count: number | null
  monthly_blocked: boolean
  eligible: boolean
  cooldown_remaining_seconds: number
  usage_age_seconds: number | null
  usage_stale: boolean
}

function makeToken(overrides: Partial<Token> = {}): Token {
  const base: Token = {
    burn_pct: 45,
    used_usd: 45.00,
    total_usd: 100.00,
    month_elapsed_pct: 50,
    workday_elapsed_pct: 48,
    calendar_elapsed_pct: 50,
    workday_timezone: 'America/Los_Angeles',
    period_start: '2026-09-01T00:00:00+00:00',
    period_end: '2026-10-01T00:00:00+00:00',
    period_workday_count: 23,
    monthly_blocked: false,
    eligible: true,
    cooldown_remaining_seconds: 0,
    usage_age_seconds: 60,
    usage_stale: false,
  }
  return { ...base, ...overrides }
}

// SWR's cache is module-global and keyed by URL, so without a fresh provider each
// test would re-read the previous test's token fixture.
function renderCard() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <OAuthCard />
    </SWRConfig>,
  )
}

function meterBar() {
  return screen.getByText('Monthly quota').parentElement?.querySelector('.relative')
}

function mockFetch(response: unknown) {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(response), { status: 200 }))))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OAuthCard', () => {
  it('defaults to workdays mode with empty localStorage', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const workdaysBtn = screen.getByRole('button', { name: 'Workdays' })
    const calendarBtn = screen.getByRole('button', { name: 'Calendar' })

    expect(workdaysBtn).toHaveAttribute('aria-pressed', 'true')
    expect(calendarBtn).toHaveAttribute('aria-pressed', 'false')
  })

  it('switches to calendar mode on button click', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const calendarBtn = screen.getByRole('button', { name: 'Calendar' })
    fireEvent.click(calendarBtn)

    await waitFor(() => {
      expect(calendarBtn).toHaveAttribute('aria-pressed', 'true')
    })

    const workdaysBtn = screen.getByRole('button', { name: 'Workdays' })
    expect(workdaysBtn).toHaveAttribute('aria-pressed', 'false')
  })

  it('persists mode to localStorage', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const calendarBtn = screen.getByRole('button', { name: 'Calendar' })
    fireEvent.click(calendarBtn)

    await waitFor(() => {
      expect(localStorage.getItem('anthrouter.oauthMeterMode')).toBe('calendar')
    })
  })

  it('restores mode from localStorage on remount', async () => {
    localStorage.setItem('anthrouter.oauthMeterMode', 'calendar')
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const calendarBtn = screen.getByRole('button', { name: 'Calendar' })
    expect(calendarBtn).toHaveAttribute('aria-pressed', 'true')
  })

  it('falls back to workdays for corrupted stored value', async () => {
    localStorage.setItem('anthrouter.oauthMeterMode', 'garbage')
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const workdaysBtn = screen.getByRole('button', { name: 'Workdays' })
    expect(workdaysBtn).toHaveAttribute('aria-pressed', 'true')
  })

  it('does not crash when localStorage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('localStorage unavailable')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('localStorage unavailable')
    })

    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const workdaysBtn = screen.getByRole('button', { name: 'Workdays' })
    expect(workdaysBtn).toBeInTheDocument()

    vi.restoreAllMocks()
  })

  it('shows workday count and timezone in workdays mode period label', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(screen.getByText(/23 workdays/)).toBeInTheDocument()
    expect(screen.getByText(/America\/Los_Angeles/)).toBeInTheDocument()
  })

  it('shows calendar label in calendar mode', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const calendarBtn = screen.getByRole('button', { name: 'Calendar' })
    fireEvent.click(calendarBtn)

    await waitFor(() => {
      expect(screen.getByText(/Calendar month/)).toBeInTheDocument()
    })
  })

  it('handles null period_workday_count gracefully', async () => {
    mockFetch({
      oauth_token: makeToken({
        period_workday_count: null,
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(screen.queryByText(/null/)).not.toBeInTheDocument()
  })

  it('handles null workday_timezone gracefully', async () => {
    mockFetch({
      oauth_token: makeToken({
        workday_timezone: null,
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(screen.queryByText(/null/)).not.toBeInTheDocument()
  })

  it('renders meter bar with base fill', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(meterBar()).toBeInTheDocument()
    expect(meterBar()?.querySelector('.bg-blue-500')?.getAttribute('style')).toContain('width: 45%')
  })

  it('renders green underuse segment when baseline exceeds burn_pct', async () => {
    mockFetch({
      oauth_token: makeToken({
        burn_pct: 30,
        workday_elapsed_pct: 48,
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const underuse = meterBar()?.querySelector('.bg-emerald-500.opacity-40')
    expect(underuse?.getAttribute('style')).toContain('left: 30%')
    expect(underuse?.getAttribute('style')).toContain('width: 18%')
  })

  it('renders red overuse segment anchored at the workday baseline', async () => {
    mockFetch({ oauth_token: makeToken({ burn_pct: 60, workday_elapsed_pct: 48 }) })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const overuse = meterBar()?.querySelector('.bg-red-500')
    expect(overuse?.getAttribute('style')).toContain('left: 48%')
    expect(overuse?.getAttribute('style')).toContain('width: 12%')
    expect(meterBar()?.querySelector('.bg-emerald-500')).toBeNull()
  })

  it('switching mode moves the overlay geometry to the calendar baseline', async () => {
    mockFetch({
      oauth_token: makeToken({ burn_pct: 45, workday_elapsed_pct: 48, calendar_elapsed_pct: 50 }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(meterBar()?.querySelector('.bg-emerald-500')?.getAttribute('style')).toContain('width: 3%')

    fireEvent.click(screen.getByRole('button', { name: 'Calendar' }))

    await waitFor(() => {
      expect(meterBar()?.querySelector('.bg-emerald-500')?.getAttribute('style')).toContain('width: 5%')
    })
  })

  it('falls back to month_elapsed_pct when the server omits the new fields', async () => {
    mockFetch({
      oauth_token: makeToken({
        burn_pct: 45,
        month_elapsed_pct: 60,
        workday_elapsed_pct: null,
        calendar_elapsed_pct: null,
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(meterBar()?.querySelector('.bg-emerald-500')?.getAttribute('style')).toContain('width: 15%')

    fireEvent.click(screen.getByRole('button', { name: 'Calendar' }))

    await waitFor(() => {
      expect(meterBar()?.querySelector('.bg-emerald-500')?.getAttribute('style')).toContain('width: 15%')
    })
  })

  it('renders neither overlay when every baseline is null', async () => {
    mockFetch({
      oauth_token: makeToken({
        burn_pct: 45,
        month_elapsed_pct: null,
        workday_elapsed_pct: null,
        calendar_elapsed_pct: null,
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(meterBar()?.querySelector('.bg-emerald-500')).toBeNull()
    expect(meterBar()?.querySelector('.bg-red-500')).toBeNull()
    expect(meterBar()?.querySelector('.bg-blue-500')?.getAttribute('style')).toContain('width: 45%')
  })

  it('renders daily pace rate text', async () => {
    mockFetch({ oauth_token: makeToken() })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const quotaSection = screen.getByText('Monthly quota').closest('div')?.parentElement
    const paceDiv = quotaSection?.querySelector('.text-slate-500')
    expect(paceDiv).toBeInTheDocument()
    expect(paceDiv?.textContent).toMatch(/daily \d+\.?\d*%/)
  })

  it('renders segment dividers in workdays mode', async () => {
    mockFetch({ oauth_token: makeToken({ period_workday_count: 23 }) })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    const meterBarEl = meterBar()
    const segments = meterBarEl?.querySelectorAll('div[style*="border-right"]')
    expect(segments?.length).toBe(23)
  })

  it('renders period allowance text in workdays mode', async () => {
    mockFetch({ oauth_token: makeToken({ total_usd: 100.00, period_workday_count: 23 }) })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(screen.getByText(/Period allowance:/)).toBeInTheDocument()
    expect(screen.getByText(/\$100\.00 total/)).toBeInTheDocument()
    expect(screen.getByText(/\$4\.35\/workday/)).toBeInTheDocument()
  })

  it('renders period allowance text in calendar mode', async () => {
    mockFetch({
      oauth_token: makeToken({
        total_usd: 100.00,
        period_start: '2026-09-01T00:00:00+00:00',
        period_end: '2026-10-01T00:00:00+00:00',
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    fireEvent.click(screen.getByRole('button', { name: 'Calendar' }))
    await waitFor(() => {
      expect(screen.getByText(/Period allowance:/)).toBeInTheDocument()
      expect(screen.getByText(/\$100\.00 total/)).toBeInTheDocument()
      expect(screen.getByText(/\$3\.33\/calendar-day/)).toBeInTheDocument()
    })
  })

  it('renders days remaining in workdays mode', async () => {
    mockFetch({
      oauth_token: makeToken({
        workday_elapsed_pct: 48,
        period_workday_count: 23,
        period_start: '2026-09-01T00:00:00+00:00',
        period_end: '2026-10-01T00:00:00+00:00',
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    expect(screen.getByText(/Days remaining:/)).toBeInTheDocument()
    expect(screen.getByText(/\(workdays\)/)).toBeInTheDocument()
  })

  it('renders days remaining in calendar mode', async () => {
    mockFetch({
      oauth_token: makeToken({
        period_start: '2026-09-01T00:00:00+00:00',
        period_end: '2026-10-01T00:00:00+00:00',
      }),
    })
    renderCard()

    await screen.findByText('Anthropic-OAuth token')

    fireEvent.click(screen.getByRole('button', { name: 'Calendar' }))
    await waitFor(() => {
      expect(screen.getByText(/Days remaining:/)).toBeInTheDocument()
      expect(screen.getByText(/\(calendar\)/)).toBeInTheDocument()
    })
  })
})
