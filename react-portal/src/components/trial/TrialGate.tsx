import { useEffect, type ReactNode } from 'react'
import { useTrialStore } from '../../stores/trialStore'
import { TRIAL_EXPIRED_EVENT } from '../../api/client'
import { FullPageSpinner } from '../common/LoadingSpinner'
import { TrialExpiredScreen } from './TrialExpiredScreen'

const REFRESH_MS = 60_000

/**
 * Outermost gate. Sits OUTSIDE auth so an expired trial never reaches the
 * login screen, and so a 402 never costs the customer their saved login.
 */
export function TrialGate({ children }: { children: ReactNode }) {
  const ready = useTrialStore(s => s.ready)
  const status = useTrialStore(s => s.status)
  const refresh = useTrialStore(s => s.refresh)
  const markExpired = useTrialStore(s => s.markExpired)

  useEffect(() => {
    void refresh()
    const interval = setInterval(() => void refresh(), REFRESH_MS)
    const onExpired = () => markExpired()
    window.addEventListener(TRIAL_EXPIRED_EVENT, onExpired)
    return () => {
      clearInterval(interval)
      window.removeEventListener(TRIAL_EXPIRED_EVENT, onExpired)
    }
  }, [refresh, markExpired])

  // Flip exactly at the expiry moment if the page is left open.
  useEffect(() => {
    if (!status.trial || status.expired || !status.expires_at) return
    const ms = new Date(status.expires_at).getTime() - Date.now()
    if (!Number.isFinite(ms) || ms <= 0 || ms > 2 ** 31 - 1) return
    const t = setTimeout(() => void refresh(), ms + 1000)
    return () => clearTimeout(t)
  }, [status.trial, status.expired, status.expires_at, refresh])

  if (!ready) return <FullPageSpinner />
  if (status.trial && status.expired) return <TrialExpiredScreen status={status} />
  return <>{children}</>
}
