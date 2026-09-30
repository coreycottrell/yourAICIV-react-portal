import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

/*
 * Review round 2 of the Reconnect Claude branch (Witness ticket 3383).
 * Each test fails on the reviewed commit a8d2edd and passes after the fix.
 *   a  closing the dialog while the server is still starting: nothing restarts
 *   b  "not waiting for a code" goes back to Start, never to "paste again"
 *   e  a failed status request does not pop the blocking Connect dialog
 *   g  already_authenticated closes cleanly instead of spinning forever
 *   h  verify timeout goes back to Start (a fresh link), not to the paste box
 */

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void }
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

const api = vi.hoisted(() => ({
  status: { authenticated: true, reason: 'token_valid', expires_at: 1000 } as Record<string, unknown>,
  statusFails: 0,
  verify: { confirmed: false, state: 'waiting' } as Record<string, unknown>,
  start: null as null | (() => Promise<unknown>),
  code: { injected: true } as Record<string, unknown>,
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
    if (path === '/api/auth/status') {
      if (api.statusFails > 0) { api.statusFails -= 1; throw new Error('network') }
      return { ...api.status }
    }
    if (path === '/api/auth/url') return { url: 'https://claude.ai/oauth/authorize?x=1&state=abc', ready: true }
    if (path === '/api/auth/verify') return { ...api.verify }
    return {}
  })
  api.post.mockImplementation(async (path: string) => {
    if (path === '/api/auth/start') return api.start ? api.start() : { started: true }
    if (path === '/api/auth/code') return { ...api.code }
    if (path === '/api/auth/close') return { closed: true, pressed: ['Escape'] }
    return {}
  })
  api.fire.mockResolvedValue({ status: 'fired' })
}

async function flush() {
  await act(async () => { await Promise.resolve() })
  await act(async () => { await Promise.resolve() })
}
const calls = (fn: typeof api.get, path: string) => fn.mock.calls.filter(c => c[0] === path).length

async function openReconnect() {
  const utils = render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
  await flush()
  fireEvent.click(screen.getByRole('button', { name: /Reconnect Claude/ }))
  await flush()
  return utils
}

beforeEach(() => {
  api.get.mockReset(); api.post.mockReset(); api.fire.mockReset()
  api.status = { authenticated: true, reason: 'token_valid', expires_at: 1000 }
  api.statusFails = 0
  api.verify = { confirmed: false, state: 'waiting' }
  api.start = null
  api.code = { injected: true }
  setupApi()
})
afterEach(() => { vi.useRealTimers() })

describe('review round 2', () => {
  it('a: closing while the server is still starting ignores the late answer (no URL polling, stays closed)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const late = deferred<unknown>()
    api.start = () => late.promise
    const { container } = await openReconnect()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel reconnect' }))
    await flush()
    await act(async () => { late.resolve({ started: true }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()
    expect(calls(api.get, '/api/auth/url')).toBe(0)
  })

  it('b: a code the server refuses (screen not waiting) goes back to Start, not to the paste box', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.code = { injected: false, not_waiting: true, error: 'The sign-in screen is not waiting for a code any more.' }
    await openReconnect()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    expect(screen.queryByPlaceholderText('eyJh...')).toBeNull()
    expect(screen.getByRole('button', { name: 'Start sign-in' })).toBeInTheDocument()
    expect(screen.getByText(/not waiting for a code/)).toBeInTheDocument()
  })

  it('e: a failing status request never pops the blocking Connect dialog', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.statusFails = 2
    const { container } = render(<ClaudeAuthFlow />)
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(3500) })
    await flush()
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()
  })

  it('g: already_authenticated closes cleanly instead of waiting for a link forever', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.start = async () => ({ started: true, already_authenticated: true })
    const { container } = await openReconnect()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    expect(screen.queryByText(/Waiting for authorization link/)).toBeNull()
    expect(screen.getByText(/already signed in/)).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(container.querySelector('.claude-auth-overlay')).toBeNull()
    expect(calls(api.get, '/api/auth/url')).toBe(0)
    expect(api.fire).not.toHaveBeenCalled()
  })

  it('h: an unconfirmed reconnect times out back to Start (fresh link), never the old paste box', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await openReconnect()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    // A token refresh lands meanwhile (new expires_at) — still not a sign-in.
    api.status = { authenticated: true, reason: 'token_valid', expires_at: 999999 }
    await act(async () => { await vi.advanceTimersByTimeAsync(125_000) })
    expect(screen.queryByText(/signed in again/)).toBeNull()
    expect(screen.queryByPlaceholderText('eyJh...')).toBeNull()
    expect(screen.getByRole('button', { name: 'Start sign-in' })).toBeInTheDocument()
  })
})
