import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '../../api/client'
import { cn } from '../../utils/cn'
import { AUTH_CHANGED_EVENT, openClaudeReconnect } from './claudeReconnect'
import './ReconnectClaudeButton.css'

/**
 * "Reconnect Claude" — always visible, so the owner can sign in to Claude again
 * (expired login) or switch to a different Claude account without anyone
 * touching the server.
 *
 * It fires RECONNECT_EVENT; ClaudeAuthFlow listens for it and opens the normal
 * Connect Claude flow even while the AI is signed in. Reconnecting never
 * re-runs the first-boot awakening and never touches memory, identity or files.
 *
 * Hidden when the AI engine does not use a personal Claude login
 * (/api/auth/status reports managed:true, e.g. the MiniMax trial).
 */

interface ClaudeAuthStatus {
  authenticated: boolean
  managed?: boolean
  reason?: string
  expires_at?: number | null
  subscription?: string | null
}

type Signal = 'unknown' | 'signed-in' | 'signed-out' | 'managed'

function toSignal(s: ClaudeAuthStatus | null): Signal {
  if (!s) return 'unknown'
  if (s.managed) return 'managed'
  return s.authenticated ? 'signed-in' : 'signed-out'
}

interface Props {
  /** 'header' = compact pill for the top bar; 'settings' = full button. */
  variant?: 'header' | 'settings'
}

export function ReconnectClaudeButton({ variant = 'header' }: Props) {
  const [signal, setSignal] = useState<Signal>('unknown')

  const refresh = useCallback(() => {
    apiGet<ClaudeAuthStatus>('/api/auth/status')
      .then(s => setSignal(toSignal(s)))
      .catch(() => setSignal('unknown'))
  }, [])

  useEffect(() => {
    refresh()
    window.addEventListener(AUTH_CHANGED_EVENT, refresh)
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, refresh)
  }, [refresh])

  if (signal === 'managed') return null

  const stateText =
    signal === 'signed-in' ? 'Claude is signed in'
      : signal === 'signed-out' ? 'Claude is not signed in'
        : 'Claude sign-in status unknown'

  return (
    <button
      type="button"
      className={cn('reconnect-claude-btn', `reconnect-claude-${variant}`)}
      onClick={openClaudeReconnect}
      title={`${stateText}. Sign in again, or switch to a different Claude account.`}
      aria-label={`Reconnect Claude (${stateText})`}
      data-claude-signal={signal}
    >
      <span className={cn('reconnect-claude-dot', `reconnect-claude-dot-${signal}`)} aria-hidden="true" />
      <span className="reconnect-claude-label">Reconnect Claude</span>
      <span className="reconnect-claude-label-short" aria-hidden="true">Claude</span>
    </button>
  )
}
