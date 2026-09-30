import { useState, useEffect, useRef, useCallback } from 'react'
import { apiGet, apiPost } from '../../api/client'
import { fireFirstBoot } from '../../api/evolution'
import { SUPPORT_URL, SUPPORT_LABEL } from '../../utils/brand'
import './ClaudeAuthFlow.css'

interface AuthStatusResponse {
  authenticated: boolean
  account?: string | null
  expires_at?: number | null
  subscription?: string | null
  /** Why the server answered as it did (e.g. "token_valid", "expired_no_activity_since"). */
  reason?: string
  /** Signed out, but an established AI is running right now. */
  live_session?: boolean
  /** 'helper': this CIV signs in in a separate window; its AI is never touched. */
  signin_mode?: 'helper'
}

interface StartResponse {
  started?: boolean
  error?: string
  url?: string
  mode?: 'helper'
}

interface UrlResponse {
  url: string | null
  ready: boolean
}

interface CodeResponse {
  injected?: boolean
  error?: string
  mode?: 'helper'
  /** helper sign-in only: 'signed_in' | 'failed' | 'pending' */
  result?: string
}

type FlowStep =
  | 'checking'
  | 'idle'
  | 'starting'
  | 'polling-url'
  | 'url-ready'
  | 'submitting-code'
  | 'verifying'
  | 'success'

export function ClaudeAuthFlow() {
  const [step, setStep] = useState<FlowStep>('checking')
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [authenticated, setAuthenticated] = useState(false)
  // Established CIV: sign in through the portal's separate sign-in window, so
  // the running AI is never typed into or stopped (t3383).
  const [helperMode, setHelperMode] = useState(false)
  const [live, setLive] = useState(false)
  const [closed, setClosed] = useState(false)

  const urlPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const statusPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const clearPolls = useCallback(() => {
    if (urlPollRef.current) {
      clearInterval(urlPollRef.current)
      urlPollRef.current = null
    }
    if (statusPollRef.current) {
      clearInterval(statusPollRef.current)
      statusPollRef.current = null
    }
  }, [])

  // Cleanup on unmount
  useEffect(() => {
    return () => clearPolls()
  }, [clearPolls])

  // Initial auth check
  useEffect(() => {
    let cancelled = false
    apiGet<AuthStatusResponse>('/api/auth/status')
      .then(res => {
        if (cancelled) return
        if (res.authenticated) {
          setAuthenticated(true)
        } else {
          if (res.signin_mode === 'helper') setHelperMode(true)
          if (res.live_session) setLive(true)
          setStep('idle')
        }
      })
      .catch(() => {
        if (!cancelled) setStep('idle')
      })
    return () => { cancelled = true }
  }, [])

  const handleStart = useCallback(async () => {
    setError(null)
    setStep('starting')
    try {
      // Check again at the click: the page may have loaded before an AI started.
      let helper = helperMode
      try {
        const now = await apiGet<AuthStatusResponse>('/api/auth/status')
        if (now.authenticated) {
          setAuthenticated(true)
          return
        }
        if (now.signin_mode === 'helper') {
          helper = true
          setHelperMode(true)
        }
        if (now.live_session) setLive(true)
      } catch {
        // The server decides the sign-in path again itself.
      }
      const res = await apiPost<StartResponse>('/api/auth/start')
      if (res.mode === 'helper') {
        helper = true
        setHelperMode(true)
      }
      if (res.error) {
        setError(res.error)
        setStep('idle')
        return
      }
      if (res.started && helper && res.url) {
        setAuthUrl(res.url)
        setStep('url-ready')
        return
      }
      if (res.started) {
        setStep('polling-url')
        // Start polling for URL
        urlPollRef.current = setInterval(async () => {
          try {
            const urlRes = await apiGet<UrlResponse>('/api/auth/url')
            if (urlRes.ready && urlRes.url) {
              setAuthUrl(urlRes.url)
              setStep('url-ready')
              if (urlPollRef.current) {
                clearInterval(urlPollRef.current)
                urlPollRef.current = null
              }
            }
          } catch {
            // Keep polling on transient errors
          }
        }, 2000)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start authentication')
      setStep('idle')
    }
  }, [helperMode])

  const handleSubmitCode = useCallback(async () => {
    if (!code.trim()) return
    setError(null)
    setStep('submitting-code')
    try {
      const res = await apiPost<CodeResponse>('/api/auth/code', { code: code.trim() })
      if (res.error) {
        setError(res.error)
        setStep(res.mode === 'helper' ? 'idle' : 'url-ready')
        return
      }
      if (res.mode === 'helper' && res.result === 'failed') {
        setError("That code didn't work. Please start the sign-in again to get a new link.")
        setCode('')
        setAuthUrl(null)
        setStep('idle')
        return
      }
      if (res.injected) {
        const helper = helperMode || res.mode === 'helper'
        setStep('verifying')
        // Poll auth status
        statusPollRef.current = setInterval(async () => {
          try {
            const statusRes = await apiGet<AuthStatusResponse>('/api/auth/status')
            if (statusRes.authenticated) {
              if (statusPollRef.current) {
                clearInterval(statusPollRef.current)
                statusPollRef.current = null
              }
              // Auth confirmed — fire evolution and dismiss immediately.
              // Do NOT wait for evolution to complete (takes 10+ min).
              // Human watches evolution in terminal/chat.
              // Newborn only: an established CIV's AI keeps running and picks
              // up the new sign-in itself.
              if (!helper) fireFirstBoot().catch(() => {})
              setAuthenticated(true)
            }
          } catch {
            // Keep polling on transient errors
          }
        }, 3000)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit code')
      setStep('url-ready')
    }
  }, [code, helperMode])

  const handleClose = useCallback(() => {
    clearPolls()
    setClosed(true)
    apiPost('/api/auth/close').catch(() => {})
  }, [clearPolls])

  // triggerEvolution removed — fire-and-forget in submitCode, dismiss immediately

  // Render nothing if authenticated or skipped
  if (authenticated) return null
  if (step === 'checking') return null
  if (closed) return null

  return (
    <div className="claude-auth-overlay">
      <div className="claude-auth-box">
        {step === 'success' ? (
          <div className="claude-auth-success">{'\u2705'} Claude authenticated successfully!</div>
        ) : (
          <>
            <div className="claude-auth-icon">{'\uD83D\uDD10'}</div>
            {helperMode ? (
              <>
                <div className="claude-auth-title">Reconnect Claude</div>
                <div className="claude-auth-desc">
                  Your AI's Claude sign-in has expired or could not be confirmed. You can sign in
                  again here. {live ? 'Your AI keeps running while you do; nothing is interrupted.' : 'Nothing on your AI is changed or restarted.'}
                </div>
                <div className="claude-auth-note">
                  You'll be redirected to claude.ai to authorize. If your AI is answering you
                  normally, you can close this.
                </div>
              </>
            ) : (
              <>
                <div className="claude-auth-title">Connect Your Claude Account</div>
                <div className="claude-auth-desc">
                  Claude needs to authenticate before it can run. This takes about 2 minutes.
                </div>
                <div className="claude-auth-note">
                  You'll be redirected to claude.ai to authorize.
                </div>
              </>
            )}

            {step === 'idle' && (
              <button className="claude-auth-btn" onClick={handleStart}>
                {helperMode ? 'Sign in' : 'Authenticate Now'}
              </button>
            )}

            {step === 'starting' && (
              <button className="claude-auth-btn" disabled>
                Starting...
              </button>
            )}

            {step === 'polling-url' && (
              <div className="claude-auth-status">
                <span className="claude-auth-spinner" />
                Waiting for authorization link...
              </div>
            )}

            {(step === 'url-ready' || step === 'submitting-code') && (
              <>
                <div className="claude-auth-status">
                  Authorization link ready! Click below, then paste the code.
                </div>
                {authUrl && (
                  <a
                    className="claude-auth-btn claude-auth-link-btn"
                    href={authUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open Claude Authorization Page
                  </a>
                )}
                <div className="claude-auth-code-section">
                  <div className="claude-auth-code-label">Paste authorization code:</div>
                  <div className="claude-auth-code-row">
                    <input
                      className="claude-auth-code-input"
                      type="text"
                      value={code}
                      onChange={e => setCode(e.target.value)}
                      placeholder="eyJh..."
                      disabled={step === 'submitting-code'}
                    />
                    <button
                      className="claude-auth-code-submit"
                      onClick={handleSubmitCode}
                      disabled={step === 'submitting-code' || !code.trim()}
                    >
                      {step === 'submitting-code' ? 'Sending...' : 'Submit'}
                    </button>
                  </div>
                </div>
              </>
            )}

            {step === 'verifying' && (
              <div className="claude-auth-status">
                <span className="claude-auth-spinner" />
                Code submitted! Verifying...
              </div>
            )}

            {error && <div className="claude-auth-error">{error}</div>}

            {helperMode && step !== 'verifying' && (
              <button className="claude-auth-btn claude-auth-close" onClick={handleClose}>
                Not now
              </button>
            )}
            {helperMode && SUPPORT_URL && (
              <a className="claude-auth-link-inline" href={SUPPORT_URL} target="_blank" rel="noopener noreferrer">
                {SUPPORT_LABEL}
              </a>
            )}

          </>
        )}
      </div>
    </div>
  )
}
