import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

/*
 * Second independent review (Witness ticket 3383, of commit 8b6c2b0).
 * Each test fails on 8b6c2b0 and passes after the fix.
 *   3  Start and Close carry the same attempt id (a Close that reaches the
 *      server first still cancels its Start)
 *   4  the Reconnect button never fires the first-boot awakening, even when
 *      Claude is signed out (an established AI whose sign-in expired); it
 *      tidies Claude's "Press Enter" screen instead
 *   4b a first sign-in whose awakening does not run tidies the pane too
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

describe('review round 3', () => {
  it('3: Start and Close carry the same attempt id', async () => {
    const late = deferred<unknown>()
    api.start = () => late.promise
    await openReconnect()
    fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel reconnect' }))
    await flush()
    const start = api.post.mock.calls.find(c => c[0] === '/api/auth/start')
    const close = api.post.mock.calls.find(c => c[0] === '/api/auth/close')
    const sa = (start?.[1] as { attempt?: string } | undefined)?.attempt
    const ca = (close?.[1] as { attempt?: string } | undefined)?.attempt
    expect(sa).toBeTruthy()
    expect(ca).toBe(sa)
    await act(async () => { late.resolve({ started: false, cancelled: true }) })
  })

  it('4: Reconnect on a signed-out established AI never fires first-boot and tidies the pane', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.status = { authenticated: false, reason: 'expired_api_reports_auth_failure', expires_at: 5 }
    render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    await flush()
    // The signed-out page load shows the Connect dialog; the owner uses the
    // Reconnect button (header) instead of the auto-opened prompt.
    fireEvent.click(screen.getByRole('button', { name: /not signed in/ }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: /Start sign-in|Authenticate Now/ }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    api.status = { authenticated: true, reason: 'token_valid', expires_at: 999 }
    api.verify = { confirmed: true, state: 'confirmed' }
    await act(async () => { await vi.advanceTimersByTimeAsync(6200) })
    expect(api.fire).not.toHaveBeenCalled()
    expect(api.post.mock.calls.filter(c => c[0] === '/api/auth/close').length).toBeGreaterThan(0)
  })

  it('4b: a first sign-in whose awakening does not run tidies Claude\'s Press Enter screen', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.status = { authenticated: false, reason: 'no_credentials_file', expires_at: null }
    render(<ClaudeAuthFlow />)
    await flush()
    api.fire.mockResolvedValue({ status: 'already_evolved' })
    fireEvent.click(screen.getByRole('button', { name: 'Authenticate Now' }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code#state' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await flush()
    api.status = { authenticated: true, reason: 'token_valid', expires_at: 999 }
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    await flush()
    expect(api.fire).toHaveBeenCalledTimes(1)
    expect(api.post.mock.calls.filter(c => c[0] === '/api/auth/close').length).toBe(1)
  })
})
