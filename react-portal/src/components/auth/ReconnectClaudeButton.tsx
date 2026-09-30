import { useEffect, useState } from 'react'
import { apiGet, apiPost } from '../../api/client'
import { CLAUDE_AUTH_STATUS_EVENT } from './claudeAuthStatus'
import type { ClaudeAuthStatus, ReconnectResponse } from './claudeAuthStatus'
import './ReconnectClaudeButton.css'

const CONFIRM_TEXT =
  'Reconnect Claude?\n\n' +
  "This signs your AI out of Claude so you can sign in again. Your AI's memory is kept."

/**
 * Reconnect Claude: moves the Claude sign-in file aside on the server (nothing
 * is typed into the AI's session and nothing is stopped), then hands the new
 * status to ClaudeAuthFlow, which shows the normal sign-in dialog, or a plain
 * note when the AI is still running. Hidden when the engine is managed.
 */
export function ReconnectClaudeButton({ variant = 'header' }: { variant?: 'header' | 'settings' }) {
  const [managed, setManaged] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    apiGet<ClaudeAuthStatus>('/api/auth/status')
      .then(s => { if (!cancelled) setManaged(!!s.managed) })
      .catch(() => { if (!cancelled) setManaged(false) })
    return () => { cancelled = true }
  }, [])

  if (managed === true && variant === 'settings') {
    return <span className="reconnect-claude-na">Not needed</span>
  }
  if (managed !== false) return null

  const handleClick = async () => {
    if (busy) return
    if (!window.confirm(CONFIRM_TEXT)) return
    setBusy(true)
    setError(null)
    try {
      const res = await apiPost<ReconnectResponse>('/api/auth/reconnect')
      if (res.error || typeof res.authenticated !== 'boolean') {
        setError('Could not reconnect. Please try again.')
        return
      }
      window.dispatchEvent(new CustomEvent<ClaudeAuthStatus>(CLAUDE_AUTH_STATUS_EVENT, { detail: res }))
    } catch {
      setError('Could not reconnect. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <span className={`reconnect-claude reconnect-claude--${variant}`}>
      <button
        type="button"
        className="reconnect-claude-btn"
        onClick={handleClick}
        disabled={busy}
        title="Sign your AI in to Claude again"
      >
        {busy ? 'Reconnecting…' : 'Reconnect Claude'}
      </button>
      {error && <span className="reconnect-claude-error" role="alert">{error}</span>}
    </span>
  )
}
