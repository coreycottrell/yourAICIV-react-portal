import { useState, useEffect, useRef, useCallback } from 'react'
import { apiGet, apiPost } from '../../api/client'
import { fireFirstBoot } from '../../api/evolution'
import { AUTH_CHANGED_EVENT, RECONNECT_EVENT } from './claudeReconnect'
import './ClaudeAuthFlow.css'

interface AuthStatusResponse {
  authenticated: boolean
  managed?: boolean
  reason?: string
  account?: string | null
  expires_at?: number | null
  subscription?: string | null
}

interface StartResponse {
  started?: boolean
  already_authenticated?: boolean
  error?: string
}

interface UrlResponse {
  url: string | null
  ready: boolean
}

interface CodeResponse {
  injected?: boolean
  error?: string
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

/** How long a reconnect waits for NEW credentials before saying so. */
const RECONNECT_VERIFY_TIMEOUT_MS = 120_000

/**
 * The Connect Claude flow.
 *
 * Opens by itself when /api/auth/status says Claude is not signed in (first
 * sign-in after birth: success fires the first-boot awakening). Also opens on
 * demand from the "Reconnect Claude" button (RECONNECT_EVENT) while Claude IS
 * signed in, so the owner can sign in again or switch accounts. A reconnect
 * never fires the first-boot awakening, and it is only called done when the
 * status shows a NEW sign-in (the old, still-valid token does not count).
 */
export function ClaudeAuthFlow() {
  const [step, setStep] = useState<FlowStep>('checking')
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [authenticated, setAuthenticated] = useState(false)
  // Opened from the Reconnect Claude button (not because Claude is signed out).
  const [reconnectMode, setReconnectMode] = useState(false)

  const urlPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const statusPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Reconnect: the status snapshot taken when the flow opened, to tell a new
  // sign-in apart from the old token that is still valid.
  const baselineRef = useRef<AuthStatusResponse | null>(null)
  const reconnectRef = useRef(false)

  const clearPolls = useCallback(() => {
    if (urlPollRef.current) {
      clearInterval(urlPollRef.current)
      urlPollRef.current = null
    }
    if (statusPollRef.current) {
      clearInterval(statusPollRef.current)
      statusPollRef.current = null
    }
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
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
          setStep('idle')
        }
      })
      .catch(() => {
        if (!cancelled) setStep('idle')
      })
    return () => { cancelled = true }
  }, [])

  // "Reconnect Claude" button: open the flow even while signed in.
  useEffect(() => {
    const onReconnect = () => {
      clearPolls()
      setError(null)
      setAuthUrl(null)
      setCode('')
      baselineRef.current = null
      apiGet<AuthStatusResponse>('/api/auth/status')
        .then(res => {
          baselineRef.current = res
          // Signed out right now: this is an ordinary sign-in, not a reconnect.
          reconnectRef.current = !!res.authenticated
          setReconnectMode(!!res.authenticated)
        })
        .catch(() => {
          reconnectRef.current = true
          setReconnectMode(true)
        })
      reconnectRef.current = true
      setReconnectMode(true)
      setAuthenticated(false)
      setStep('idle')
    }
    window.addEventListener(RECONNECT_EVENT, onReconnect)
    return () => window.removeEventListener(RECONNECT_EVENT, onReconnect)
  }, [clearPolls])

  const closeFlow = useCallback(() => {
    clearPolls()
    // Leave the AI's own pane clean: close the login picker / code prompt
    // (cancel) or the "Press Enter to continue" screen (after success).
    apiPost('/api/auth/close').catch(() => {})
    reconnectRef.current = false
    setReconnectMode(false)
    setError(null)
    setAuthUrl(null)
    setCode('')
    setAuthenticated(true)
    setStep('idle')
  }, [clearPolls])

  const handleStart = useCallback(async () => {
    setError(null)
    setStep('starting')
    try {
      const res = await apiPost<StartResponse>('/api/auth/start')
      if (res.error) {
        setError(res.error)
        setStep('idle')
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
  }, [])

  const isNewSignIn = (s: AuthStatusResponse): boolean => {
    if (!s.authenticated) return false
    if (!reconnectRef.current) return true
    const base = baselineRef.current
    // Signed out when the reconnect began -> any sign-in is new.
    if (!base || !base.authenticated) return true
    // Signed in when it began: only fresh credentials count.
    return s.expires_at != null && s.expires_at !== base.expires_at
  }

  const handleSubmitCode = useCallback(async () => {
    if (!code.trim()) return
    setError(null)
    setStep('submitting-code')
    try {
      const res = await apiPost<CodeResponse>('/api/auth/code', { code: code.trim() })
      if (res.error) {
        setError(res.error)
        setStep('url-ready')
        return
      }
      if (res.injected) {
        setStep('verifying')
        const startedAt = Date.now()
        // Poll auth status
        statusPollRef.current = setInterval(async () => {
          try {
            const statusRes = await apiGet<AuthStatusResponse>('/api/auth/status')
            if (isNewSignIn(statusRes)) {
              if (statusPollRef.current) {
                clearInterval(statusPollRef.current)
                statusPollRef.current = null
              }
              window.dispatchEvent(new CustomEvent(AUTH_CHANGED_EVENT))
              if (reconnectRef.current) {
                // Reconnect is not a birth: never re-run the awakening.
                setStep('success')
                closeTimerRef.current = setTimeout(closeFlow, 2500)
                return
              }
              // Auth confirmed — fire evolution and dismiss immediately.
              // Do NOT wait for evolution to complete (takes 10+ min).
              // Human watches evolution in terminal/chat.
              fireFirstBoot().catch(() => {})
              setAuthenticated(true)
            } else if (reconnectRef.current && Date.now() - startedAt > RECONNECT_VERIFY_TIMEOUT_MS) {
              if (statusPollRef.current) {
                clearInterval(statusPollRef.current)
                statusPollRef.current = null
              }
              setError('The new sign-in was not confirmed. Open the authorization page again, copy the new code, and paste it here.')
              setStep('url-ready')
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
  }, [code, closeFlow])

  // Render nothing if authenticated (and no reconnect was asked for)
  if (authenticated) return null
  if (step === 'checking') return null

  const title = reconnectMode ? 'Reconnect Claude' : 'Connect Your Claude Account'
  const desc = reconnectMode
    ? 'Sign in to Claude again, or sign in with a different Claude account. Your AI keeps its memory, identity and files.'
    : 'Claude needs to authenticate before it can run. This takes about 2 minutes.'

  return (
    <div className="claude-auth-overlay" role="dialog" aria-modal="true" aria-label={title}>
      <div className="claude-auth-box">
        {reconnectMode && step !== 'success' && (
          <button
            type="button"
            className="claude-auth-close"
            onClick={closeFlow}
            aria-label="Cancel reconnect"
          >
            {'×'}
          </button>
        )}
        {step === 'success' ? (
          <div className="claude-auth-success">{'✅'} Claude is signed in again.</div>
        ) : (
          <>
            <div className="claude-auth-icon">{'🔐'}</div>
            <div className="claude-auth-title">{title}</div>
            <div className="claude-auth-desc">{desc}</div>
            <div className="claude-auth-note">
              You'll be redirected to claude.ai to authorize.
            </div>

            {step === 'idle' && (
              <button className="claude-auth-btn" onClick={handleStart}>
                {reconnectMode ? 'Start sign-in' : 'Authenticate Now'}
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

          </>
        )}
      </div>
    </div>
  )
}
