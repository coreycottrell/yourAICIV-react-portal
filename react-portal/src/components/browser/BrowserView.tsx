import { useEffect, useRef, useState, useCallback } from 'react'
import { AUTH_TOKEN_KEY } from '../../utils/constants'
import './BrowserView.css'

// Browser viewport dimensions (must match server.py VIEWPORT)
const BROWSER_W = 1280
const BROWSER_H = 800

interface LogEntry {
  action: string
  [key: string]: unknown
}

export function BrowserView() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const [currentUrl, setCurrentUrl] = useState('about:blank')
  const [urlInput, setUrlInput] = useState('')
  const [title, setTitle] = useState('')
  const [humanControl, setHumanControl] = useState(false)
  const [log, setLog] = useState<LogEntry[]>([])
  const [status, setStatus] = useState<'connecting' | 'live' | 'error'>('connecting')

  // Draw a frame onto the canvas
  const drawFrame = useCallback((b64: string) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const img = new Image()
    img.onload = () => ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
    img.src = `data:image/jpeg;base64,${b64}`
  }, [])

  // Connect WebSocket
  useEffect(() => {
    const token = localStorage.getItem(AUTH_TOKEN_KEY) ?? ''
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/browser?token=${token}`

    const connect = () => {
      setStatus('connecting')
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        setStatus('live')
      }

      ws.onclose = () => {
        setConnected(false)
        setStatus('error')
        // Reconnect after 3s
        setTimeout(connect, 3000)
      }

      ws.onerror = () => {
        setStatus('error')
      }

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'frame') {
            drawFrame(msg.data)
            if (msg.url && msg.url !== currentUrl) setCurrentUrl(msg.url)
            if (msg.title) setTitle(msg.title)
          } else if (msg.type === 'log') {
            setLog(prev => [msg.entry, ...prev].slice(0, 50))
          }
        } catch {
          // ignore
        }
      }
    }

    connect()
    return () => {
      wsRef.current?.close()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Send command over WebSocket
  const sendWs = useCallback((cmd: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(cmd))
    }
  }, [])

  // Navigate to URL via REST
  const navigate = useCallback(async () => {
    if (!urlInput.trim()) return
    sendWs({ action: 'navigate', url: urlInput.trim() })
  }, [urlInput, sendWs])

  // Canvas click → forward to browser when in human control mode
  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!humanControl || !canvasRef.current) return
    const rect = canvasRef.current.getBoundingClientRect()
    const x = ((e.clientX - rect.left) / rect.width) * BROWSER_W
    const y = ((e.clientY - rect.top) / rect.height) * BROWSER_H
    sendWs({ action: 'click', x, y })
  }, [humanControl, sendWs])

  // Canvas scroll → forward to browser when in human control mode
  const handleWheel = useCallback((e: React.WheelEvent<HTMLCanvasElement>) => {
    if (!humanControl) return
    e.preventDefault()
    sendWs({ action: 'scroll', deltaY: e.deltaY })
  }, [humanControl, sendWs])

  const statusColor = status === 'live' ? 'var(--success)' : status === 'error' ? 'var(--error)' : 'var(--warning)'
  const statusLabel = status === 'live' ? 'Live' : status === 'error' ? 'Reconnecting...' : 'Connecting...'

  return (
    <div className="browser-view">
      {/* Toolbar */}
      <div className="browser-toolbar">
        <div className="browser-status-dot" style={{ background: statusColor }} title={statusLabel} />

        <form
          className="browser-url-form"
          onSubmit={e => { e.preventDefault(); navigate() }}
        >
          <input
            className="browser-url-input"
            type="text"
            value={urlInput}
            onChange={e => setUrlInput(e.target.value)}
            placeholder="https://..."
            spellCheck={false}
          />
          <button className="browser-go-btn" type="submit">Go</button>
        </form>

        <div className="browser-current-url" title={currentUrl}>
          {title || currentUrl}
        </div>

        <button
          className={`browser-control-btn ${humanControl ? 'active' : ''}`}
          onClick={() => setHumanControl(v => !v)}
          title={humanControl ? 'Click to return to Agent control' : 'Click to take manual control'}
        >
          {humanControl ? '🕹️ You' : '🤖 Agent'}
        </button>
      </div>

      {/* Main area: viewport + log */}
      <div className="browser-body">
        <div className="browser-canvas-wrap">
          {!connected && (
            <div className="browser-overlay">
              <div className="browser-overlay-msg">
                {status === 'connecting' ? 'Connecting to browser...' : 'Browser offline — reconnecting...'}
              </div>
            </div>
          )}
          <canvas
            ref={canvasRef}
            className={`browser-canvas ${humanControl ? 'human-mode' : ''}`}
            width={BROWSER_W}
            height={BROWSER_H}
            onClick={handleCanvasClick}
            onWheel={handleWheel}
          />
        </div>

        <div className="browser-log">
          <div className="browser-log-header">Action Log</div>
          {log.length === 0 ? (
            <div className="browser-log-empty">No actions yet</div>
          ) : (
            log.map((entry, i) => (
              <div key={i} className="browser-log-entry">
                <span className="browser-log-action">{entry.action}</span>
                {entry.url != null && <span className="browser-log-detail">{String(entry.url).slice(0, 40)}</span>}
                {entry.x != null && <span className="browser-log-detail">({Math.round(entry.x as number)}, {Math.round(entry.y as number)})</span>}
                {entry.text != null && <span className="browser-log-detail">"{String(entry.text).slice(0, 20)}"</span>}
              </div>
            ))
          )}
        </div>
      </div>

      {/* Human control hint */}
      {humanControl && (
        <div className="browser-control-hint">
          🕹️ You have control — clicks and scrolls go to the browser. Press <kbd>Esc</kbd> or click Agent to hand back.
        </div>
      )}
    </div>
  )
}
