import { useEffect, useState } from 'react'
import { useTeamsStore, type TmuxPane } from '../../stores/teamsStore'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { EmptyState } from '../common/EmptyState'
import './TeamsView.css'

function PaneCard({ pane }: { pane: TmuxPane }) {
  const [msg, setMsg] = useState('')
  const [sending, setSending] = useState(false)
  const injectMessage = useTeamsStore(s => s.injectMessage)

  const handleSend = async () => {
    const trimmed = msg.trim()
    if (!trimmed || sending) return
    setSending(true)
    await injectMessage(pane.id, trimmed)
    setMsg('')
    setSending(false)
  }

  return (
    <div className="pane-card">
      <div className="pane-header">
        <span className="pane-title">{pane.title || pane.id}</span>
        <span className="pane-target">{pane.target}</span>
      </div>
      <pre className="pane-content">{pane.content || '(empty)'}</pre>
      <div className="pane-input">
        <input
          className="pane-msg-input"
          value={msg}
          onChange={e => setMsg(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSend() }}
          placeholder="Send message to this pane..."
          disabled={sending}
        />
        <button
          className="pane-send-btn"
          onClick={handleSend}
          disabled={!msg.trim() || sending}
        >
          {sending ? '...' : '\u{27A4}'}
        </button>
      </div>
    </div>
  )
}

export function TeamsView() {
  const { panes, loading, loadPanes } = useTeamsStore()

  useEffect(() => {
    loadPanes()
    const interval = setInterval(loadPanes, 3000)
    return () => clearInterval(interval)
  }, [loadPanes])

  if (loading && panes.length === 0) {
    return <div className="teams-loading"><LoadingSpinner size={32} /></div>
  }

  if (panes.length === 0) {
    return (
      <div className="teams-empty">
        <EmptyState title="No active panes" description="No tmux panes found in the current session" />
      </div>
    )
  }

  return (
    <div className="teams-view">
      <h2 className="teams-title">Teams</h2>
      <p className="teams-subtitle">{panes.length} active pane{panes.length !== 1 ? 's' : ''}</p>
      <div className="teams-grid">
        {panes.map(pane => (
          <PaneCard key={pane.id} pane={pane} />
        ))}
      </div>
    </div>
  )
}
