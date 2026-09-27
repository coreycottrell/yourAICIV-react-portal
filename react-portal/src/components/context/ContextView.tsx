import { useEffect, useState, useCallback } from 'react'
import { apiGet } from '../../api/client'
import { useIdentityStore } from '../../stores/identityStore'
import { formatUptime } from '../../utils/time'
import { LoadingSpinner } from '../common/LoadingSpinner'
import './ContextView.css'

interface ContextData {
  input_tokens: number
  cache_read: number
  cache_creation: number
  total_tokens: number
  max_tokens: number
  pct: number
  model?: string | null
  session_id?: string
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function pctColor(pct: number): string {
  if (pct < 50) return 'var(--status-success)'
  if (pct < 75) return 'var(--status-warning)'
  return 'var(--status-error)'
}

function pctLabel(pct: number): string {
  if (pct < 25) return 'Fresh'
  if (pct < 50) return 'Comfortable'
  if (pct < 75) return 'Working'
  if (pct < 90) return 'Getting Full'
  return 'Near Limit'
}

export function ContextView() {
  const [ctx, setCtx] = useState<ContextData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const status = useIdentityStore(s => s.status)

  const fetchContext = useCallback(async () => {
    try {
      const data = await apiGet<ContextData>('/api/context')
      setCtx(data)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch context')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchContext()
    const interval = setInterval(fetchContext, 15_000)
    return () => clearInterval(interval)
  }, [fetchContext])

  if (loading) {
    return <div className="ctx-loading"><LoadingSpinner size={32} /></div>
  }

  if (error) {
    return (
      <div className="ctx-view">
        <h2 className="ctx-title">Context Window</h2>
        <div className="ctx-error">{error}</div>
      </div>
    )
  }

  const pct = ctx?.pct ?? 0
  const total = ctx?.total_tokens ?? 0
  const max = ctx?.max_tokens ?? 200_000
  const free = Math.max(0, max - total)
  const inputPct = max > 0 ? (ctx?.input_tokens ?? 0) / max * 100 : 0
  const cacheReadPct = max > 0 ? (ctx?.cache_read ?? 0) / max * 100 : 0
  const cacheCreatePct = max > 0 ? (ctx?.cache_creation ?? 0) / max * 100 : 0

  return (
    <div className="ctx-view">
      <h2 className="ctx-title">Context Window</h2>

      {/* Hero gauge */}
      <div className="ctx-hero">
        <div className="ctx-ring-wrap">
          <svg className="ctx-ring" viewBox="0 0 120 120">
            <circle className="ctx-ring-bg" cx="60" cy="60" r="52" />
            <circle
              className="ctx-ring-fill"
              cx="60" cy="60" r="52"
              style={{
                strokeDasharray: `${pct * 3.267} 326.7`,
                stroke: pctColor(pct),
              }}
            />
          </svg>
          <div className="ctx-ring-label">
            <span className="ctx-ring-pct" style={{ color: pctColor(pct) }}>
              {pct.toFixed(1)}%
            </span>
            <span className="ctx-ring-sub">{pctLabel(pct)}</span>
          </div>
        </div>
        <div className="ctx-hero-stats">
          <div className="ctx-hero-stat">
            <span className="ctx-hero-stat-value">{formatTokens(total)}</span>
            <span className="ctx-hero-stat-label">Used</span>
          </div>
          <div className="ctx-hero-stat">
            <span className="ctx-hero-stat-value">{formatTokens(free)}</span>
            <span className="ctx-hero-stat-label">Free</span>
          </div>
          <div className="ctx-hero-stat">
            <span className="ctx-hero-stat-value">{formatTokens(max)}</span>
            <span className="ctx-hero-stat-label">Capacity</span>
          </div>
        </div>
      </div>

      {/* Stacked bar */}
      <div className="ctx-card">
        <h3 className="ctx-card-title">Token Breakdown</h3>
        <div className="ctx-card-body">
          <div className="ctx-stacked-bar">
            <div
              className="ctx-bar-seg ctx-bar-input"
              style={{ width: `${inputPct}%` }}
              title={`Input: ${formatTokens(ctx?.input_tokens ?? 0)}`}
            />
            <div
              className="ctx-bar-seg ctx-bar-cache-read"
              style={{ width: `${cacheReadPct}%` }}
              title={`Cache read: ${formatTokens(ctx?.cache_read ?? 0)}`}
            />
            <div
              className="ctx-bar-seg ctx-bar-cache-create"
              style={{ width: `${cacheCreatePct}%` }}
              title={`Cache creation: ${formatTokens(ctx?.cache_creation ?? 0)}`}
            />
          </div>
          <div className="ctx-legend">
            <div className="ctx-legend-item">
              <span className="ctx-legend-dot ctx-bar-input" />
              <span className="ctx-legend-text">Input</span>
              <span className="ctx-legend-value">{formatTokens(ctx?.input_tokens ?? 0)}</span>
            </div>
            <div className="ctx-legend-item">
              <span className="ctx-legend-dot ctx-bar-cache-read" />
              <span className="ctx-legend-text">Cache Read</span>
              <span className="ctx-legend-value">{formatTokens(ctx?.cache_read ?? 0)}</span>
            </div>
            <div className="ctx-legend-item">
              <span className="ctx-legend-dot ctx-bar-cache-create" />
              <span className="ctx-legend-text">Cache Creation</span>
              <span className="ctx-legend-value">{formatTokens(ctx?.cache_creation ?? 0)}</span>
            </div>
            <div className="ctx-legend-item">
              <span className="ctx-legend-dot ctx-bar-free" />
              <span className="ctx-legend-text">Free</span>
              <span className="ctx-legend-value">{formatTokens(free)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* System info */}
      <div className="ctx-grid">
        <div className="ctx-card">
          <h3 className="ctx-card-title">Session</h3>
          <div className="ctx-card-body">
            <div className="ctx-row">
              <span className="ctx-label">Session ID</span>
              <span className="ctx-value ctx-mono">{ctx?.session_id ?? 'N/A'}</span>
            </div>
            <div className="ctx-row">
              <span className="ctx-label">Claude</span>
              <span className="ctx-value">
                {status?.claude_running ? 'Running' : 'Stopped'}
              </span>
            </div>
            <div className="ctx-row">
              <span className="ctx-label">Uptime</span>
              <span className="ctx-value">
                {status?.uptime ? formatUptime(status.uptime) : 'N/A'}
              </span>
            </div>
          </div>
        </div>

        <div className="ctx-card">
          <h3 className="ctx-card-title">Capacity Planning</h3>
          <div className="ctx-card-body">
            <div className="ctx-row">
              <span className="ctx-label">Model</span>
              <span className="ctx-value ctx-mono">{ctx?.model ?? 'unknown'}</span>
            </div>
            <div className="ctx-row">
              <span className="ctx-label">Max Context</span>
              <span className="ctx-value">{formatTokens(max)} tokens</span>
            </div>
            <div className="ctx-row">
              <span className="ctx-label">Autocompact</span>
              <span className="ctx-value">
                {pct >= 80 ? 'Imminent' : pct >= 60 ? 'Approaching' : 'Not needed'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
