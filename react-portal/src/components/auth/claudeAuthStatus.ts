/** Shape of GET /api/auth/status (and POST /api/auth/reconnect). */
export interface ClaudeAuthStatus {
  authenticated: boolean
  managed?: boolean
  account?: string | null
  expires_at?: number | null
  subscription?: string | null
  /** Why the server answered as it did (e.g. "token_valid", "no_credentials"). */
  reason?: string
  /** Signed out, but an established AI is running in its session right now. */
  live_session?: boolean
}

export interface ReconnectResponse extends ClaudeAuthStatus {
  reconnect?: { moved: boolean; backup: string | null }
  error?: string
}

/** Fired on window with a ClaudeAuthStatus as detail after a reconnect. */
export const CLAUDE_AUTH_STATUS_EVENT = 'youraiciv:claude-auth-status'
