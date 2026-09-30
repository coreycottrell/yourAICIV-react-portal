import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { ClaudeAuthFlow } from '../components/auth/ClaudeAuthFlow'
import { ReconnectClaudeButton } from '../components/auth/ReconnectClaudeButton'
import { CLAUDE_AUTH_STATUS_EVENT } from '../components/auth/claudeAuthStatus'

type Json = Record<string, unknown>

let statusBody: Json = { authenticated: true }
let reconnectBody: Json = { authenticated: false, reason: 'no_credentials', live_session: false }
let calls: { path: string; method: string }[] = []

function mockFetch() {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, opts: RequestInit = {}) => {
    const path = String(url)
    const method = (opts.method || 'GET').toUpperCase()
    calls.push({ path, method })
    let body: Json = {}
    if (path === '/api/auth/status') body = statusBody
    else if (path === '/api/auth/reconnect') body = reconnectBody
    else if (path === '/api/auth/start') body = { started: true }
    else if (path === '/api/auth/url') body = { url: null, ready: false }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
}

beforeEach(() => {
  statusBody = { authenticated: true }
  reconnectBody = { authenticated: false, reason: 'no_credentials', live_session: false }
  mockFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const startCalls = () => calls.filter(c => c.path === '/api/auth/start' || c.path === '/api/auth/prewarm')

describe('ClaudeAuthFlow (t3383)', () => {
  it('newborn / signed out with no live session: main sign-in dialog, unchanged', async () => {
    statusBody = { authenticated: false, reason: 'no_credentials', live_session: false }
    render(<ClaudeAuthFlow />)
    expect(await screen.findByText('Authenticate Now')).toBeInTheDocument()
    expect(screen.getByText('Connect Your Claude Account')).toBeInTheDocument()
    expect(startCalls()).toHaveLength(0) // nothing starts until the owner clicks
  })

  it('signed out while an established AI is running: plain note, no sign-in button, closable', async () => {
    statusBody = { authenticated: false, reason: 'expired_no_activity_since', live_session: true }
    render(<ClaudeAuthFlow />)
    expect(await screen.findByText(/still running/)).toBeInTheDocument()
    expect(screen.queryByText('Authenticate Now')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Close'))
    await waitFor(() => expect(screen.queryByText(/still running/)).not.toBeInTheDocument())
    expect(startCalls()).toHaveLength(0)
  })

  it('signed in: renders nothing', async () => {
    render(<ClaudeAuthFlow />)
    await waitFor(() => expect(calls.some(c => c.path === '/api/auth/status')).toBe(true))
    expect(screen.queryByText('Connect Your Claude Account')).not.toBeInTheDocument()
  })

  it('a status event after reconnect opens the normal dialog (no live session)', async () => {
    render(<ClaudeAuthFlow />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    act(() => {
      window.dispatchEvent(new CustomEvent(CLAUDE_AUTH_STATUS_EVENT, {
        detail: { authenticated: false, reason: 'no_credentials', live_session: false },
      }))
    })
    expect(await screen.findByText('Authenticate Now')).toBeInTheDocument()
    expect(startCalls()).toHaveLength(0)
  })

  it('a status event after reconnect shows the note when the AI is running', async () => {
    render(<ClaudeAuthFlow />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    act(() => {
      window.dispatchEvent(new CustomEvent(CLAUDE_AUTH_STATUS_EVENT, {
        detail: { authenticated: false, reason: 'no_credentials', live_session: true },
      }))
    })
    expect(await screen.findByText(/Claude has been signed out on your AI/)).toBeInTheDocument()
    expect(screen.queryByText('Authenticate Now')).not.toBeInTheDocument()
    expect(startCalls()).toHaveLength(0)
  })
})

describe('ReconnectClaudeButton (t3383)', () => {
  it('asks first; cancel does nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<ReconnectClaudeButton />)
    fireEvent.click(await screen.findByText('Reconnect Claude'))
    expect(calls.some(c => c.path === '/api/auth/reconnect')).toBe(false)
  })

  it('confirmed: calls only the reconnect endpoint and hands the status to the flow', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const seen: unknown[] = []
    const onEvt = (e: Event) => seen.push((e as CustomEvent).detail)
    window.addEventListener(CLAUDE_AUTH_STATUS_EVENT, onEvt)
    render(<ReconnectClaudeButton />)
    fireEvent.click(await screen.findByText('Reconnect Claude'))
    await waitFor(() => expect(seen).toHaveLength(1))
    window.removeEventListener(CLAUDE_AUTH_STATUS_EVENT, onEvt)
    const posts = calls.filter(c => c.method === 'POST').map(c => c.path)
    expect(posts).toEqual(['/api/auth/reconnect'])
    expect(startCalls()).toHaveLength(0)
  })

  it('hidden in the header when the engine is managed; "Not needed" in settings', async () => {
    statusBody = { authenticated: true, managed: true }
    const { container } = render(<ReconnectClaudeButton />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    expect(container.querySelector('.reconnect-claude-btn')).toBeNull()
    render(<ReconnectClaudeButton variant="settings" />)
    expect(await screen.findByText('Not needed')).toBeInTheDocument()
  })

  it('end to end: button + flow, no live session -> main dialog appears, nothing started', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    fireEvent.click(await screen.findByText('Reconnect Claude'))
    expect(await screen.findByText('Authenticate Now')).toBeInTheDocument()
    expect(startCalls()).toHaveLength(0)
  })

  it('reconnect held while the AI runs: plain note even though still signed in, nothing started', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    reconnectBody = { authenticated: true, reason: 'token_valid', live_session: true,
      reconnect: { moved: false, backup: null, held: 'live_session' } }
    render(<><ReconnectClaudeButton /><ClaudeAuthFlow /></>)
    fireEvent.click(await screen.findByText('Reconnect Claude'))
    expect(await screen.findByText(/Reconnect did not sign it out/)).toBeInTheDocument()
    expect(screen.queryByText('Authenticate Now')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Close'))
    await waitFor(() => expect(screen.queryByText(/Reconnect did not sign it out/)).not.toBeInTheDocument())
    expect(startCalls()).toHaveLength(0)
  })
})
