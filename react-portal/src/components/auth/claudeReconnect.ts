/** Window event: open the Connect Claude flow (the Reconnect Claude button). */
export const RECONNECT_EVENT = 'claude:reconnect'
/** Window event ClaudeAuthFlow fires after a sign-in is confirmed. */
export const AUTH_CHANGED_EVENT = 'claude:auth-changed'

/** Open the Connect Claude flow, even while Claude is signed in. */
export function openClaudeReconnect(): void {
  window.dispatchEvent(new CustomEvent(RECONNECT_EVENT))
}
