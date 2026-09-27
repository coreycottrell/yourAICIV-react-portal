import { useEffect, useState } from 'react'
import { apiGet } from '../../api/client'
import { formatUptime } from '../../utils/time'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { useTrialStore } from '../../stores/trialStore'
import { safePaymentUrl } from '../../api/trial'
import './StatusView.css'

interface StatusData {
  civ: string
  uptime: number
  tmux_session: string
  tmux_alive: boolean
  claude_running: boolean
  tg_bot_running: boolean
  ctx_pct: number | null
  version: string
  timestamp: number
}

interface BoopStatus {
  running: boolean
  pid?: number
}

interface AuthStatus {
  authenticated: boolean
  managed?: boolean
  account?: string | null
  expires_at?: number | null
  subscription?: string | null
}

function Dot({ ok }: { ok: boolean | undefined | null }) {
  return <span className={`status-dot-ind ${ok ? 'is-ok' : 'is-bad'}`} aria-hidden="true" />
}

export function StatusView() {
  const [status, setStatus] = useState<StatusData | null>(null)
  const [boop, setBoop] = useState<BoopStatus | null>(null)
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const trial = useTrialStore(s => s.status)

  useEffect(() => {
    const fetchAll = async () => {
      try {
        const [s, b, a] = await Promise.allSettled([
          apiGet<StatusData>('/api/status'),
          apiGet<BoopStatus>('/api/boop/status'),
          apiGet<AuthStatus>('/api/auth/status'),
        ])
        if (s.status === 'fulfilled') setStatus(s.value)
        if (b.status === 'fulfilled') setBoop(b.value)
        if (a.status === 'fulfilled') setAuth(a.value)
      } finally {
        setLoading(false)
      }
    }
    fetchAll()
    const interval = setInterval(fetchAll, 15_000)
    return () => clearInterval(interval)
  }, [])

  if (loading) {
    return <div className="status-loading"><LoadingSpinner size={32} /></div>
  }

  const ctxPct = status?.ctx_pct ?? 0
  const working = !!status?.claude_running
  const payUrl = safePaymentUrl(trial.payment_url)

  return (
    <div className="status-view">
      <h2 className="status-title">Status</h2>

      <div className={`status-hero ${working ? 'is-ok' : 'is-bad'}`}>
        <Dot ok={working} />
        <div>
          <div className="status-hero-title">
            {status?.civ || 'Your AI'} is {working ? 'online and working' : 'offline'}
          </div>
          <div className="status-hero-sub">
            {working
              ? `Up for ${status ? formatUptime(status.uptime) : '—'}. Message it any time in Chat.`
              : 'It may be restarting. If this lasts more than a few minutes, contact your support team.'}
          </div>
        </div>
      </div>

      <div className="status-grid">
        {trial.trial && (
          <div className="status-card">
            <h3 className="status-card-title">Free trial</h3>
            <div className="status-card-body">
              <div className="status-row">
                <span className="status-label">Progress</span>
                <span className="status-value">Day {trial.day} of {trial.duration_days || 7}</span>
              </div>
              <div className="status-row">
                <span className="status-label">Days left</span>
                <span className="status-value">{trial.days_left}</span>
              </div>
              <div className="status-row">
                <span className="status-label">Ends</span>
                <span className="status-value">{trial.expires_at ? new Date(trial.expires_at).toLocaleString() : '—'}</span>
              </div>
              {payUrl && (
                <a className="status-cta" href={payUrl} target="_blank" rel="noopener noreferrer">
                  Subscribe now
                </a>
              )}
            </div>
          </div>
        )}

        <div className="status-card">
          <h3 className="status-card-title">Connections</h3>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">AI engine</span>
              <span className="status-value"><Dot ok={working} /> {working ? 'running' : 'stopped'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Scheduled check-ins</span>
              <span className="status-value"><Dot ok={boop?.running} /> {boop?.running ? 'on' : 'off'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Telegram</span>
              <span className="status-value"><Dot ok={status?.tg_bot_running} /> {status?.tg_bot_running ? 'connected' : 'not connected'}</span>
            </div>
            {auth && !auth.managed && (
              <div className="status-row">
                <span className="status-label">Engine sign-in</span>
                <span className="status-value"><Dot ok={auth.authenticated} /> {auth.authenticated ? 'signed in' : 'needs sign-in'}</span>
              </div>
            )}
          </div>
        </div>

        <details className="status-card status-details">
          <summary className="status-card-title">Technical details</summary>
          <div className="status-card-body">
            <div className="status-row">
              <span className="status-label">Version</span>
              <span className="status-value">{status?.version ?? '—'}</span>
            </div>
            <div className="status-row">
              <span className="status-label">Session</span>
              <span className="status-value status-mono">{status?.tmux_session ?? '—'} ({status?.tmux_alive ? 'alive' : 'down'})</span>
            </div>
            <div className="status-row">
              <span className="status-label">Working memory used</span>
              <span className="status-value">{ctxPct > 0 ? `${ctxPct.toFixed(1)}%` : 'n/a'}</span>
            </div>
            <div className="status-ctx-bar">
              <div className="status-ctx-fill" style={{ width: `${Math.min(ctxPct, 100)}%` }} />
            </div>
            {auth?.account && (
              <div className="status-row">
                <span className="status-label">Engine account</span>
                <span className="status-value">{auth.account}</span>
              </div>
            )}
          </div>
        </details>
      </div>
    </div>
  )
}
