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
  url?: string
  already_authenticated?: boolean
  cancelled?: boolean
  in_progress?: boolean
  busy?: boolean
  error?: string
}

interface UrlResponse {
  url: string | null
  ready: boolean
}

interface CodeResponse {
  injected?: boolean
  not_waiting?: boolean
  error?: string
}

interface VerifyResponse {
  confirmed: boolean
  state?: string
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

/** How long a sign-in waits for confirmation before saying so. */
const VERIFY_TIMEOUT_MS = 120_000
/** How long we poll for the authorization link after the server started. */
const URL_POLL_TIMEOUT_MS = 90_000

const NOT_CONFIRMED_MESSAGE =
  'The sign-in was not confirmed. Start the sign-in again to get a fresh link and code.'

/**
 * The Connect Claude flow.
 *
 * Opens by itself when /api/auth/status says Claude is not signed in (first
 * sign-in after birth: success fires the first-boot awakening). Also opens on
 * demand from the "Reconnect Claude" button (RECONNECT_EVENT) while Claude IS
 * signed in, so the owner can sign in again or switch accounts. A reconnect
 * never fires the first-boot awakening, and it is only called done when the
 * server confirms a REAL new sign-in (/api/auth/verify: Claude printed a new
 * "Login successful" AND wrote new credentials). A background token refresh
 * changes expires_at too, so expires_at is never used as proof.
 *
 * Every async result is tied to the flow that asked for it (flowRef): once the
 * owner closes the dialog, late answers are ignored and nothing restarts.
 */
export function ClaudeAuthFlow() {
  const [step, setStep] = useState<FlowStep>('checking')
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [authenticated, setAuthenticated] = useState(false)
  const [successText, setSuccessText] = useState('Claude is signed in again.')
  // Opened from the Reconnect Claude button (not because Claude is signed out).
  const [reconnectMode, setReconnectMode] = useState(false)

  const urlPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const statusPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectRef = useRef(false)
  // Bumped whenever the flow is opened, closed or restarted.
  const flowRef = useRef(0)

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

  // Initial auth check. Only a real "not signed in" answer opens the dialog;
  // a failed status request does not (it retries once, then stays closed —
  // the Reconnect Claude button is always there).
  useEffect(() => {
    let cancelled = false
    let retry: ReturnType<typeof setTimeout> | null = null
    const check = (attempt: number) => {
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
          if (cancelled) return
          if (attempt === 0) {
            retry = setTimeout(() => check(1), 3000)
          } else {
            setAuthenticated(true)
          }
        })
    }
    check(0)
    return () => {
      cancelled = true
      if (retry) clearTimeout(retry)
    }
  }, [])

  // "Reconnect Claude" button: open the flow even while signed in.
  useEffect(() => {
    const onReconnect = () => {
      clearPolls()
      const flow = ++flowRef.current
      setError(null)
      setAuthUrl(null)
      setCode('')
      reconnectRef.current = true
      setReconnectMode(true)
      setAuthenticated(false)
      setStep('idle')
      apiGet<AuthStatusResponse>('/api/auth/status')
        .then(res => {
          if (flow !== flowRef.current) return
          // Signed out right now: this is an ordinary sign-in, not a reconnect.
          reconnectRef.current = !!res.authenticated
          setReconnectMode(!!res.authenticated)
        })
        .catch(() => {})
    }
    window.addEventListener(RECONNECT_EVENT, onReconnect)
    return () => window.removeEventListener(RECONNECT_EVENT, onReconnect)
  }, [clearPolls])

  const closeFlow = useCallback(() => {
    clearPolls()
    flowRef.current += 1
    // Stops the server's sign-in flow, then leaves the AI's own pane clean:
    // close the login picker / code prompt (cancel) or the "Press Enter to
    // continue" screen (after success).
    apiPost('/api/auth/close').catch(() => {})
    reconnectRef.current = false
    setReconnectMode(false)
    setError(null)
    setAuthUrl(null)
    setCode('')
    setAuthenticated(true)
    setStep('idle')
  }, [clearPolls])

  /** Back to the start button with a message (never back to "paste a code"). */
  const restartWith = useCallback((message: string | null) => {
    clearPolls()
    setAuthUrl(null)
    setCode('')
    setError(message)
    setStep('idle')
  }, [clearPolls])

  const finishSignedIn = useCallback((text: string) => {
    clearPolls()
    window.dispatchEvent(new CustomEvent(AUTH_CHANGED_EVENT))
    if (reconnectRef.current) {
      // Reconnect is not a birth: never re-run the awakening.
      setSuccessText(text)
      setStep('success')
      closeTimerRef.current = setTimeout(closeFlow, 2500)
      return
    }
    // Auth confirmed — fire evolution and dismiss immediately.
    // Do NOT wait for evolution to complete (takes 10+ min).
    // Human watches evolution in terminal/chat.
    fireFirstBoot().catch(() => {})
    setAuthenticated(true)
  }, [clearPolls, closeFlow])

  const handleStart = useCallback(async () => {
    clearPolls()
    const flow = ++flowRef.current
    setError(null)
    setStep('starting')
    try {
      const res = await apiPost<StartResponse>('/api/auth/start')
      if (flow !== flowRef.current) return  // closed or restarted meanwhile
      if (res.cancelled) {
        restartWith(null)
        return
      }
      if (res.already_authenticated) {
        finishSignedIn('Claude is already signed in.')
        return
      }
      if (res.error || !res.started) {
        restartWith(res.error || 'The sign-in could not start. Try again.')
        return
      }
      if (res.url) {
        setAuthUrl(res.url)
        setStep('url-ready')
        return
      }
      setStep('polling-url')
      const startedAt = Date.now()
      urlPollRef.current = setInterval(async () => {
        if (flow !== flowRef.current) return
        if (Date.now() - startedAt > URL_POLL_TIMEOUT_MS) {
          restartWith('The authorization link did not appear. Try again.')
          return
        }
        try {
          const urlRes = await apiGet<UrlResponse>('/api/auth/url')
          if (flow !== flowRef.current) return
          if (urlRes.ready && urlRes.url) {
            if (urlPollRef.current) {
              clearInterval(urlPollRef.current)
              urlPollRef.current = null
            }
            setAuthUrl(urlRes.url)
            setStep('url-ready')
          }
        } catch {
          // Keep polling on transient errors
        }
      }, 2000)
    } catch (err) {
      if (flow !== flowRef.current) return
      restartWith(err instanceof Error ? err.message : 'Failed to start authentication')
    }
  }, [clearPolls, finishSignedIn, restartWith])

  const handleSubmitCode = useCallback(async () => {
    if (!code.trim()) return
    const flow = flowRef.current
    setError(null)
    setStep('submitting-code')
    try {
      const res = await apiPost<CodeResponse>('/api/auth/code', { code: code.trim() })
      if (flow !== flowRef.current) return
      if (res.not_waiting) {
        // The sign-in screen is gone: pasting again would type the code into
        // the AI's chat. Start over for a fresh link.
        restartWith(res.error || NOT_CONFIRMED_MESSAGE)
        return
      }
      if (res.error || !res.injected) {
        setError(res.error || 'The code could not be sent. Try again.')
        setStep('url-ready')
        return
      }
      setStep('verifying')
      const startedAt = Date.now()
      statusPollRef.current = setInterval(async () => {
        if (flow !== flowRef.current) return
        try {
          const verify = await apiGet<VerifyResponse>('/api/auth/verify')
          if (flow !== flowRef.current) return
          let done = verify.confirmed
          if (!done && !reconnectRef.current) {
            // First sign-in: signed out -> signed in is itself the proof.
            const s = await apiGet<AuthStatusResponse>('/api/auth/status')
            if (flow !== flowRef.current) return
            done = s.authenticated
          }
          if (done) {
            finishSignedIn('Claude is signed in again.')
          } else if (Date.now() - startedAt > VERIFY_TIMEOUT_MS) {
            restartWith(NOT_CONFIRMED_MESSAGE)
          }
        } catch {
          // Keep polling on transient errors
        }
      }, 3000)
    } catch (err) {
      if (flow !== flowRef.current) return
      setError(err instanceof Error ? err.message : 'Failed to submit code')
      setStep('url-ready')
    }
  }, [code, finishSignedIn, restartWith])

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
          <div className="claude-auth-success">{'✅'} {successText}</div>
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
