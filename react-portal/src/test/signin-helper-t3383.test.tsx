import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ClaudeAuthFlow } from '../components/auth/ClaudeAuthFlow'

type Json = Record<string, unknown>
let statuses: Json[] = []
let startBody: Json = {}
let codeBody: Json = {}
let calls: string[] = []

beforeEach(() => {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, opts: RequestInit = {}) => {
    const u = String(url)
    calls.push(`${(opts.method || 'GET').toUpperCase()} ${u}`)
    let body: Json = {}
    if (u === '/api/auth/status') body = statuses.length > 1 ? statuses.shift()! : statuses[0]
    else if (u === '/api/auth/start') body = startBody
    else if (u === '/api/auth/code') body = codeBody
    else if (u === '/api/auth/url') body = { url: 'https://claude.ai/oauth/authorize?x=1&state=nb', ready: true }
    else body = { status: 'fired' }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

const URL = 'https://claude.ai/oauth/authorize?code=true&state=helper1'

describe('sign-in through the helper window (t3383)', () => {
  it('established CIV with a running AI: signs in via the helper; first boot left to the server', async () => {
    statuses = [
      { authenticated: false, live_session: true, signin_mode: 'helper' },
      { authenticated: false, live_session: true, signin_mode: 'helper' },
      { authenticated: true },
    ]
    startBody = { started: true, url: URL, mode: 'helper' }
    codeBody = { injected: true, mode: 'helper', result: 'signed_in' }
    render(<ClaudeAuthFlow />)
    fireEvent.click(await screen.findByText('Sign in'))
    expect(await screen.findByText('Open Claude Authorization Page')).toHaveAttribute('href', URL)
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'abc#def' } })
    fireEvent.click(screen.getByText('Submit'))
    await waitFor(() => expect(screen.queryByText('Reconnect Claude')).not.toBeInTheDocument(), { timeout: 10000 })
    expect(calls).not.toContain('GET /api/auth/url')
    // first boot is called as on main; the server returns already_evolved / skipped for a non-newborn
    expect(calls.filter(c => c.startsWith('POST'))).toEqual(['POST /api/auth/start', 'POST /api/auth/code', 'POST /api/evolution/first-boot'])
  }, 15000)

  it('a failed helper code shows an error and offers a fresh sign-in', async () => {
    statuses = [{ authenticated: false, signin_mode: 'helper' }]
    startBody = { started: true, url: URL, mode: 'helper' }
    codeBody = { injected: true, mode: 'helper', result: 'failed' }
    render(<ClaudeAuthFlow />)
    fireEvent.click(await screen.findByText('Sign in'))
    fireEvent.change(await screen.findByPlaceholderText('eyJh...'), { target: { value: 'bad' } })
    fireEvent.click(screen.getByText('Submit'))
    expect(await screen.findByText(/didn't work/)).toBeInTheDocument()
    expect(screen.getByText('Sign in')).toBeInTheDocument()
  }, 15000)

  it('click-time re-check: page loaded as newborn, an AI started since -> helper path', async () => {
    statuses = [
      { authenticated: false, live_session: false },
      { authenticated: false, live_session: true, signin_mode: 'helper' },
      { authenticated: false, live_session: true, signin_mode: 'helper' },
      { authenticated: true },
    ]
    startBody = { started: true, url: URL, mode: 'helper' }
    codeBody = { injected: true, mode: 'helper', result: 'signed_in' }
    render(<ClaudeAuthFlow />)
    fireEvent.click(await screen.findByText('Authenticate Now'))
    expect(await screen.findByText('Reconnect Claude')).toBeInTheDocument()
    fireEvent.change(await screen.findByPlaceholderText('eyJh...'), { target: { value: 'abc' } })
    fireEvent.click(screen.getByText('Submit'))
    await waitFor(() => expect(screen.queryByText('Reconnect Claude')).not.toBeInTheDocument(), { timeout: 10000 })
    expect(calls).not.toContain('GET /api/auth/url')
  }, 15000)

  it('established CIV whose AI was not running: signed in, then a plain note says so', async () => {
    statuses = [
      { authenticated: false, live_session: false, signin_mode: 'helper' },
      { authenticated: false, live_session: false, signin_mode: 'helper' },
      { authenticated: true },
    ]
    startBody = { started: true, url: URL, mode: 'helper' }
    codeBody = { injected: true, mode: 'helper', result: 'signed_in', ai_running: false }
    render(<ClaudeAuthFlow />)
    fireEvent.click(await screen.findByText('Sign in'))
    fireEvent.change(await screen.findByPlaceholderText('eyJh...'), { target: { value: 'abc' } })
    fireEvent.click(screen.getByText('Submit'))
    expect(await screen.findByText(/wasn't running/, {}, { timeout: 10000 })).toBeInTheDocument()
    fireEvent.click(screen.getByText('Close'))
    await waitFor(() => expect(screen.queryByText(/wasn't running/)).not.toBeInTheDocument())
  }, 15000)

  it('newborn: main path, URL polled, first boot fired after sign-in', async () => {
    statuses = [
      { authenticated: false, reason: 'no_credentials', live_session: false },
      { authenticated: false, reason: 'no_credentials', live_session: false },
      { authenticated: true },
    ]
    startBody = { started: true, url: 'https://claude.ai/oauth/authorize?x=1&state=nb' }
    codeBody = { injected: true }
    render(<ClaudeAuthFlow />)
    fireEvent.click(await screen.findByText('Authenticate Now'))
    expect(await screen.findByText('Open Claude Authorization Page', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(calls).toContain('GET /api/auth/url')
    expect(screen.queryByText('Not now')).not.toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('eyJh...'), { target: { value: 'code' } })
    fireEvent.click(screen.getByText('Submit'))
    await waitFor(() => expect(calls).toContain('POST /api/evolution/first-boot'), { timeout: 10000 })
  }, 15000)
})
