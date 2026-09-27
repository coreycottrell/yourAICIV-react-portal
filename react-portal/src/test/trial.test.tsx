import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { TrialGate } from '../components/trial/TrialGate'
import { TrialBadge } from '../components/trial/TrialBadge'
import { TrialExpiredScreen } from '../components/trial/TrialExpiredScreen'
import { OperatorOnly } from '../components/trial/OperatorOnly'
import { SettingsView } from '../components/settings/SettingsView'
import { useTrialStore } from '../stores/trialStore'
import { NOT_A_TRIAL, safePaymentUrl, type TrialStatus } from '../api/trial'
import { apiGet, TRIAL_EXPIRED_EVENT } from '../api/client'
import { AUTH_TOKEN_KEY } from '../utils/constants'

const PAY = 'https://buy.stripe.com/5kQeVe8Xe9D53GZdLb1Fe06'

const active: TrialStatus = {
  trial: true,
  day: 3,
  days_left: 5,
  duration_days: 7,
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  payment_url: PAY,
}
const expired: TrialStatus = { ...active, day: 7, days_left: 0, expired: true }

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  }
}

const mockFetch = vi.fn()

beforeEach(() => {
  mockFetch.mockReset()
  globalThis.fetch = mockFetch as unknown as typeof fetch
  localStorage.clear()
  useTrialStore.setState({ status: NOT_A_TRIAL, ready: false })
})

describe('safePaymentUrl', () => {
  it('accepts https only', () => {
    expect(safePaymentUrl(PAY)).toBe(PAY)
    expect(safePaymentUrl('javascript:alert(1)')).toBeNull()
    expect(safePaymentUrl('http://example.com')).toBeNull()
    expect(safePaymentUrl('')).toBeNull()
  })
})

describe('TrialBadge', () => {
  it('shows "Day N of 7" while the trial is active', () => {
    useTrialStore.setState({ status: active, ready: true })
    render(<TrialBadge />)
    expect(screen.getByText('Day 3 of 7')).toBeInTheDocument()
    expect(screen.getByRole('link')).toHaveAttribute('href', PAY)
  })

  it('renders nothing when not a trial', () => {
    useTrialStore.setState({ status: NOT_A_TRIAL, ready: true })
    const { container } = render(<TrialBadge />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('TrialExpiredScreen', () => {
  it('offers exactly one action: the payment link', () => {
    render(<TrialExpiredScreen status={expired} />)
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', PAY)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByText(/trial has ended/i)).toBeInTheDocument()
  })
})

describe('TrialGate', () => {
  it('renders the app when not a trial', async () => {
    mockFetch.mockResolvedValue(jsonResponse(NOT_A_TRIAL))
    render(<MemoryRouter><TrialGate><div>APP</div></TrialGate></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('APP')).toBeInTheDocument())
  })

  it('renders the app during an active trial', async () => {
    mockFetch.mockResolvedValue(jsonResponse(active))
    render(<MemoryRouter><TrialGate><div>APP</div></TrialGate></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('APP')).toBeInTheDocument())
  })

  it('replaces the whole app with the payment screen when expired', async () => {
    mockFetch.mockResolvedValue(jsonResponse(expired))
    render(<MemoryRouter><TrialGate><div>APP</div></TrialGate></MemoryRouter>)
    await waitFor(() => expect(screen.getByText(/trial has ended/i)).toBeInTheDocument())
    expect(screen.queryByText('APP')).not.toBeInTheDocument()
  })

  it('locks immediately when any request reports 402', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(active))
    render(<MemoryRouter><TrialGate><div>APP</div></TrialGate></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('APP')).toBeInTheDocument())
    mockFetch.mockResolvedValue(jsonResponse(expired))
    await act(async () => {
      window.dispatchEvent(new Event(TRIAL_EXPIRED_EVENT))
    })
    await waitFor(() => expect(screen.getByText(/trial has ended/i)).toBeInTheDocument())
  })

  it('never blocks on its own failure to load trial status', async () => {
    mockFetch.mockRejectedValue(new Error('network'))
    render(<MemoryRouter><TrialGate><div>APP</div></TrialGate></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('APP')).toBeInTheDocument())
  })
})

describe('apiFetch on 402', () => {
  it('signals expiry and keeps the saved login', async () => {
    localStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const onExpired = vi.fn()
    window.addEventListener(TRIAL_EXPIRED_EVENT, onExpired)
    mockFetch.mockResolvedValue(jsonResponse({ error: 'trial_expired', payment_url: PAY }, 402))
    await expect(apiGet('/api/status')).rejects.toMatchObject({ status: 402 })
    expect(onExpired).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe('tok')
    window.removeEventListener(TRIAL_EXPIRED_EVENT, onExpired)
  })
})

describe('apiFetch on 403', () => {
  it('rejects one action without signing the client out', async () => {
    localStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    mockFetch.mockResolvedValue(jsonResponse({ error: 'operator_tools_locked' }, 403))
    await expect(apiGet('/api/panes')).rejects.toMatchObject({ status: 403 })
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe('tok')
  })
})

describe('expired screen when the server failed closed', () => {
  it('tells an already-paying client who to contact', () => {
    render(<TrialExpiredScreen status={{ ...expired, expires_at: '', config_error: true }} />)
    expect(screen.getByText(/already subscribed/i)).toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(1)
  })
})

describe('Operator tools during a trial', () => {
  it('OperatorOnly renders the tool for a paid install', () => {
    useTrialStore.setState({ status: NOT_A_TRIAL, ready: true })
    render(<MemoryRouter><OperatorOnly><div>TERMINAL</div></OperatorOnly></MemoryRouter>)
    expect(screen.getByText('TERMINAL')).toBeInTheDocument()
  })

  it('OperatorOnly never mounts the tool during a trial', () => {
    useTrialStore.setState({ status: active, ready: true })
    render(<MemoryRouter><OperatorOnly><div>TERMINAL</div></OperatorOnly></MemoryRouter>)
    expect(screen.queryByText('TERMINAL')).not.toBeInTheDocument()
    expect(screen.getByText(/not available during the free trial/i)).toBeInTheDocument()
  })

  it('Settings lists Terminal, Sessions and Browser only outside a trial', () => {
    useTrialStore.setState({ status: NOT_A_TRIAL, ready: true })
    const { unmount } = render(<MemoryRouter><SettingsView /></MemoryRouter>)
    for (const label of ['Terminal', 'Sessions', 'Browser', 'Context window']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
    unmount()
    useTrialStore.setState({ status: active, ready: true })
    render(<MemoryRouter><SettingsView /></MemoryRouter>)
    for (const label of ['Terminal', 'Sessions', 'Browser']) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: 'Context window' })).toBeInTheDocument()
  })
})
