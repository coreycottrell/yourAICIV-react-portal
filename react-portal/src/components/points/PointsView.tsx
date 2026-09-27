import { useEffect, useState, useCallback } from 'react'
import { apiGet } from '../../api/client'
import { LoadingSpinner } from '../common/LoadingSpinner'
import './PointsView.css'

interface ReactionSummary {
  total_reactions: number
  net_score: number
  human_score: number
  civ_score: number
  loose_sentiment: string
  sentiment_breakdown: Record<string, number>
  top_emojis: { emoji: string; count: number }[]
  recent: {
    timestamp: string
    emoji: string
    emoji_name: string
    weight: number
    reactor: string
    msg_role: string
    msg_preview: string
  }[]
}

const EMOJI_MAP: Record<string, string> = {
  'thumbs-up': '\u{1F44D}', 'thumbs-down': '\u{1F44E}', 'rocket': '\u{1F680}',
  'fire': '\u{1F525}', 'check': '\u{2705}', 'explosion': '\u{1F4A5}',
  'mind-blown': '\u{1F92F}', 'muscle': '\u{1F4AA}', 'bullseye': '\u{1F3AF}',
  'gem': '\u{1F48E}', 'heart': '\u{2764}\u{FE0F}', 'heart-eyes': '\u{1F60D}',
  'sad': '\u{1F622}', 'neutral': '\u{1F610}',
}

function sentimentColor(s: string): string {
  if (s.includes('very positive')) return 'var(--status-success)'
  if (s.includes('positive')) return 'var(--accent-secondary)'
  if (s.includes('negative')) return 'var(--status-error)'
  return 'var(--text-secondary)'
}

function formatTime(ts: string): string {
  try {
    const d = new Date(ts)
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch {
    return ts
  }
}

export function PointsView() {
  const [data, setData] = useState<ReactionSummary | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchData = useCallback(async () => {
    try {
      const d = await apiGet<ReactionSummary>('/api/reaction/summary')
      setData(d)
    } catch {
      // silent
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 15_000)
    return () => clearInterval(interval)
  }, [fetchData])

  if (loading) return <div className="pts-loading"><LoadingSpinner size={32} /></div>

  if (!data) return <div className="pts-view"><h2 className="pts-title">Points</h2><p>No data</p></div>

  return (
    <div className="pts-view">
      <h2 className="pts-title">Points & Sentiment</h2>

      {/* Hero score */}
      <div className="pts-hero">
        <div className="pts-score-ring">
          <span className="pts-score-value">{data.net_score}</span>
          <span className="pts-score-label">Net Score</span>
        </div>
        <div className="pts-hero-stats">
          <div className="pts-hero-stat">
            <span className="pts-hero-stat-value">{data.total_reactions}</span>
            <span className="pts-hero-stat-label">Reactions</span>
          </div>
          <div className="pts-hero-stat">
            <span className="pts-hero-stat-value" style={{ color: sentimentColor(data.loose_sentiment) }}>
              {data.loose_sentiment}
            </span>
            <span className="pts-hero-stat-label">Mood</span>
          </div>
        </div>
      </div>

      {/* Who gave what */}
      <div className="pts-grid">
        <div className="pts-card">
          <h3 className="pts-card-title">Score by Reactor</h3>
          <div className="pts-card-body">
            <div className="pts-reactor-bars">
              <div className="pts-reactor-row">
                <span className="pts-reactor-label">Human</span>
                <div className="pts-reactor-bar-bg">
                  <div
                    className="pts-reactor-bar-fill pts-bar-human"
                    style={{ width: data.net_score > 0 ? `${Math.min((data.human_score / data.net_score) * 100, 100)}%` : '0%' }}
                  />
                </div>
                <span className="pts-reactor-score">{data.human_score}</span>
              </div>
              <div className="pts-reactor-row">
                <span className="pts-reactor-label">CIV</span>
                <div className="pts-reactor-bar-bg">
                  <div
                    className="pts-reactor-bar-fill pts-bar-civ"
                    style={{ width: data.net_score > 0 ? `${Math.min((data.civ_score / data.net_score) * 100, 100)}%` : '0%' }}
                  />
                </div>
                <span className="pts-reactor-score">{data.civ_score}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="pts-card">
          <h3 className="pts-card-title">Top Emojis</h3>
          <div className="pts-card-body">
            <div className="pts-emoji-list">
              {data.top_emojis.map(e => (
                <div key={e.emoji} className="pts-emoji-row">
                  <span className="pts-emoji-icon">{EMOJI_MAP[e.emoji] ?? e.emoji}</span>
                  <span className="pts-emoji-name">{e.emoji}</span>
                  <span className="pts-emoji-count">{e.count}x</span>
                </div>
              ))}
              {data.top_emojis.length === 0 && (
                <span className="pts-empty">No reactions yet</span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Sentiment breakdown */}
      <div className="pts-card">
        <h3 className="pts-card-title">Sentiment Breakdown</h3>
        <div className="pts-card-body">
          <div className="pts-sentiment-grid">
            {Object.entries(data.sentiment_breakdown).map(([label, count]) => (
              <div key={label} className="pts-sentiment-chip">
                <span className="pts-sentiment-label">{label}</span>
                <span className="pts-sentiment-count">{count}</span>
              </div>
            ))}
            {Object.keys(data.sentiment_breakdown).length === 0 && (
              <span className="pts-empty">No sentiment data</span>
            )}
          </div>
        </div>
      </div>

      {/* Recent activity feed */}
      <div className="pts-card">
        <h3 className="pts-card-title">Recent Activity</h3>
        <div className="pts-card-body">
          <div className="pts-feed">
            {data.recent.slice().reverse().map((r, i) => (
              <div key={i} className="pts-feed-item">
                <span className="pts-feed-emoji">{r.emoji}</span>
                <div className="pts-feed-content">
                  <div className="pts-feed-meta">
                    <span className="pts-feed-reactor">{r.reactor === 'civ' ? 'CIV' : 'Human'}</span>
                    <span className="pts-feed-arrow">reacted to</span>
                    <span className="pts-feed-role">{r.msg_role}</span>
                    <span className="pts-feed-weight">
                      {r.weight > 0 ? `+${r.weight}` : r.weight}
                    </span>
                  </div>
                  {r.msg_preview && (
                    <div className="pts-feed-preview">{r.msg_preview}</div>
                  )}
                  <div className="pts-feed-time">{formatTime(r.timestamp)}</div>
                </div>
              </div>
            ))}
            {data.recent.length === 0 && (
              <span className="pts-empty">No reactions yet — start reacting in Chat!</span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
