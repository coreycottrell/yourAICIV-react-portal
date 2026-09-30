import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ClaudeAuthFlow } from '../components/auth/ClaudeAuthFlow'

type Json = Record<string, unknown>
let statusBody: Json = { authenticated: true }
let calls: string[] = []

beforeEach(() => {
  statusBody = { authenticated: true }
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, opts: RequestInit = {}) => {
    calls.push(`${(opts.method || 'GET').toUpperCase()} ${String(url)}`)
    const body = String(url) === '/api/auth/status' ? statusBody : { started: true }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
})
afterEach(() => { vi.unstubAllGlobals() })

const flowCalls = () => calls.filter(c => c.startsWith('POST'))

describe('ClaudeAuthFlow honest status (t3383)', () => {
  it('newborn / signed out, no AI running: main sign-in dialog, unchanged, nothing started', async () => {
    statusBody = { authenticated: false, reason: 'no_credentials', live_session: false }
    render(<ClaudeAuthFlow />)
    expect(await screen.findByText('Authenticate Now')).toBeInTheDocument()
    expect(screen.getByText('Connect Your Claude Account')).toBeInTheDocument()
    expect(flowCalls()).toHaveLength(0)
  })

  it('response without live_session (older server) behaves exactly like main', async () => {
    statusBody = { authenticated: false }
    render(<ClaudeAuthFlow />)
    expect(await screen.findByText('Authenticate Now')).toBeInTheDocument()
  })

  it('signed out while an established AI runs: Reconnect dialog, closable, nothing started', async () => {
    statusBody = { authenticated: false, reason: 'expired_no_activity_since', live_session: true, signin_mode: 'helper' }
    render(<ClaudeAuthFlow />)
    expect(await screen.findByText('Reconnect Claude')).toBeInTheDocument()
    expect(screen.getByText(/keeps running/)).toBeInTheDocument()
    expect(screen.queryByText('Authenticate Now')).not.toBeInTheDocument()
    expect(flowCalls()).toHaveLength(0)
    fireEvent.click(screen.getByText('Not now'))
    await waitFor(() => expect(screen.queryByText('Reconnect Claude')).not.toBeInTheDocument())
    expect(flowCalls()).toEqual(['POST /api/auth/close'])
  })

  it('signed in: renders nothing', async () => {
    render(<ClaudeAuthFlow />)
    await waitFor(() => expect(calls).toContain('GET /api/auth/status'))
    expect(screen.queryByText('Connect Your Claude Account')).not.toBeInTheDocument()
    expect(screen.queryByText('Reconnect Claude')).not.toBeInTheDocument()
  })
})
