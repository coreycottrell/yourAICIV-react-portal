import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

/*
 * Reconnect Claude button + Connect Claude flow (Witness ticket 3383).
 * The API is mocked: every test scripts what /api/auth/status returns.
 */

const api = vi.hoisted(() => ({
  status: { authenticated: true, reason: 'token_valid', expires_at: 1000 } as Record<string, unknown>,
  verify: { confirmed: false, state: 'waiting' } as Record<string, unknown>,
  get: vi.fn(),
  post: vi.fn(),
  fire: vi.fn(),
}))

vi.mock('../api/client', () => ({
  apiGet: (path: string) => api.get(path),
  apiPost: (path: string, body?: unknown) => api.post(path, body),
}))
vi.mock('../api/evolution', () => ({
  fireFirstBoot: () => api.fire(),
}))

import { ReconnectClaudeButton } from '../components/auth/ReconnectClaudeButton'
import { ClaudeAuthFlow } from '../components/auth/ClaudeAuthFlow'

function setupApi() {
  api.get.mockImplementation(async (path: string) => {
    if (path === '/api/auth/status') return { ...api.status }
    if (path === '/api/auth/url') return { url: 'https://claude.ai/oauth/authorize?x=1&state=abc', ready: true }
    if (path === '/api/auth/verify') return { ...api.verify }
    return {}
  })
  api.post.mockImplementation(async (path: string) => {
    if (path === '/api/auth/start') return { started: true }
    if (path === '/api/auth/code') return { injected: true }
    if (path === '/api/auth/close') return { closed: true, pressed: ['Escape'] }
    return {}
  })
  api.fire.mockResolvedValue({ status: 'fired' })
}

async function flush() {
  await act(async () => { await Promise.resolve() })
  await act(async () => { await Promise.resolve() })
}

function posted(path: string) {
  return api.post.mock.calls.filter(c => c[0] === path).length
}

beforeEach(() => {
  api.get.mockReset(); api.post.mockReset(); api.fire.mockReset()
  api.status = { authenticated: true, reason: 'token_valid', expires_at: 1000 }
  api.verify = { confirmed: false, state: 'waiting' }
  setupApi()
})
afterEach(() => { vi.useRealTimers() })

describe('ReconnectClaudeButton', () => {
  it('renders an always-visible Reconnect Claude button when signed in', async () => {
    render(<ReconnectClaudeButton />)
    await flush()
    const btn = screen.getByRole('button', { name: /Reconnect Claude/ })
    expect(btn).toBeInTheDocument()
    expect(btn).toHaveAttribute('data-claude-signal', 'signed-in')
  })

  it('shows signed-out state when Claude is not signed in', async () => {
    api.status = { authenticated: false, reason: 'no_credentials_file', expires_at: null }
    render(<ReconnectClaudeButton />)
    await flush()
    expect(screen.getByRole('button', { name: /not signed in/ })).toHaveAttribute('data-claude-signal', 'signed-out')
  })

  it('is hidden for a managed engine (no personal Claude login)', async () => {
    api.status = { authenticated: true, managed: true }
    const { container } = render(<ReconnectClaudeButton />)
    await flush()
    expect(container.querySelector('.reconnect-claude-btn')).toBeNull()
  })
})

describe('ClaudeAuthFlow', () => {
  it('signed out: shows the Connect prompt by itself', async () => {
    api.status = { authenticated: false, reason: 'no_credentials_file', expires_at: null }
    render(<ClaudeAuthFlow />)
    await flush()
    expect(screen.getByText('Connect Your Claude Account')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Authenticate Now' })).toBeInTheDocument()
  })

  it('signed in: renders nothing until Reconnect is clicked, then opens the Connect flow', async () => {
    const { container } = render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    await flush()
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Reconnect Claude/ }))
    await flush()
    expect(screen.getByRole('dialog', { name: 'Reconnect Claude' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    expect(posted('/api/auth/start')).toBe(1)
  })

  it('cancel closes the flow and tidies the live pane', async () => {
    const { container } = render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: /Reconnect Claude/ }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel reconnect' }))
    await flush()
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()
    expect(posted('/api/auth/close')).toBe(1)
  })

  it('reconnect: only a server-confirmed NEW sign-in counts (not an expires_at change); never re-runs first boot', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: /Reconnect Claude/ }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(screen.getByText('Open Claude Authorization Page')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    // Same expires_at as when the reconnect opened -> still verifying.
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    expect(screen.getByText(/Verifying/)).toBeInTheDocument()

    // A background token refresh changes expires_at: NOT a sign-in (review round 2, finding h).
    api.status = { authenticated: true, reason: 'token_valid', expires_at: 999999 }
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    expect(screen.getByText(/Verifying/)).toBeInTheDocument()

    // The server confirms a real new sign-in.
    api.verify = { confirmed: true, state: 'confirmed' }
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    expect(screen.getByText(/signed in again/)).toBeInTheDocument()
    expect(api.fire).not.toHaveBeenCalled()
  })

  it('first sign-in (signed out at load) still fires the first-boot awakening', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.status = { authenticated: false, reason: 'no_credentials_file', expires_at: null }
    const { container } = render(<ClaudeAuthFlow />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Authenticate Now' }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    api.status = { authenticated: true, reason: 'token_valid', expires_at: 5000 }
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    expect(api.fire).toHaveBeenCalledTimes(1)
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()
  })
})
