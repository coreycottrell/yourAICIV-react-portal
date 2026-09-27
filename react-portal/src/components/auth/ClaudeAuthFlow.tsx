import { useState, useEffect, useRef, useCallback } from 'react'
import { apiGet, apiPost } from '../../api/client'
import { fireFirstBoot } from '../../api/evolution'
import './ClaudeAuthFlow.css'

interface AuthStatusResponse {
  authenticated: boolean
  account?: string | null
  expires_at?: number | null
  subscription?: string | null
}

interface StartResponse {
  started?: boolean
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

export function ClaudeAuthFlow() {
  const [step, setStep] = useState<FlowStep>('checking')
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [authenticated, setAuthenticated] = useState(false)

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
              fireFirstBoot().catch(() => {})
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
  }, [code])

  // triggerEvolution removed — fire-and-forget in submitCode, dismiss immediately

  // Render nothing if authenticated or skipped
  if (authenticated) return null
  if (step === 'checking') return null

  return (
    <div className="claude-auth-overlay">
      <div className="claude-auth-box">
        {step === 'success' ? (
          <div className="claude-auth-success">{'\u2705'} Claude authenticated successfully!</div>
        ) : (
          <>
            <div className="claude-auth-icon">{'\uD83D\uDD10'}</div>
            <div className="claude-auth-title">Connect Your Claude Account</div>
            <div className="claude-auth-desc">
              Claude needs to authenticate before it can run. This takes about 2 minutes.
            </div>
            <div className="claude-auth-note">
              You'll be redirected to claude.ai to authorize.
            </div>

            {step === 'idle' && (
              <button className="claude-auth-btn" onClick={handleStart}>
                Authenticate Now
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
