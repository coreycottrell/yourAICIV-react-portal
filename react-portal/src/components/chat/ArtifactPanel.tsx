import { useState, useRef, useCallback, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './ArtifactPanel.css'

interface ArtifactPanelProps {
  content: string
  language: string
  onClose: () => void
}

type RenderMode = 'html' | 'markdown' | 'code'

function resolveMode(language: string): RenderMode {
  const lang = language.toLowerCase()
  if (lang === 'html') return 'html'
  if (lang === 'markdown' || lang === 'md') return 'markdown'
  return 'code'
}

function langLabel(language: string): string {
  const labels: Record<string, string> = {
    html: 'HTML',
    markdown: 'Markdown',
    md: 'Markdown',
    css: 'CSS',
    js: 'JavaScript',
    javascript: 'JavaScript',
    ts: 'TypeScript',
    typescript: 'TypeScript',
    tsx: 'TSX',
    jsx: 'JSX',
    python: 'Python',
    py: 'Python',
    json: 'JSON',
    yaml: 'YAML',
    yml: 'YAML',
    bash: 'Bash',
    sh: 'Shell',
    sql: 'SQL',
    rust: 'Rust',
    go: 'Go',
  }
  return labels[language.toLowerCase()] || language.toUpperCase()
}

export function ArtifactPanel({ content, language, onClose }: ArtifactPanelProps) {
  const mode = resolveMode(language)
  const [copied, setCopied] = useState(false)
  const [panelWidth, setPanelWidth] = useState<number | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const startX = useRef(0)
  const startWidth = useRef(0)

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // fallback
    }
  }, [content])

  // Resize logic
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    startX.current = e.clientX
    startWidth.current = panelRef.current?.offsetWidth || 0
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }, [])

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragging.current) return
      const delta = startX.current - e.clientX
      const newWidth = Math.max(350, Math.min(startWidth.current + delta, window.innerWidth * 0.75))
      setPanelWidth(newWidth)
    }

    const onMouseUp = () => {
      if (dragging.current) {
        dragging.current = false
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
    }

    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [])

  // Close on Escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const style = panelWidth ? { width: `${panelWidth}px`, minWidth: `${panelWidth}px` } : undefined

  return (
    <div className="artifact-overlay" ref={panelRef} style={style}>
      <div className="artifact-resize-handle" onMouseDown={onMouseDown} />

      <div className="artifact-header">
        <div className="artifact-title">
          <span className="artifact-lang-badge">{langLabel(language)}</span>
          <span className="artifact-label">
            {mode === 'html' ? 'Live Preview' : mode === 'markdown' ? 'Rendered' : 'Source'}
          </span>
        </div>
        <div className="artifact-header-actions">
          {copied && <span className="artifact-copy-toast">Copied!</span>}
          <button className="artifact-btn" onClick={handleCopy} title="Copy content">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
              <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
            </svg>
          </button>
          <button className="artifact-btn artifact-btn-close" onClick={onClose} title="Close panel (Esc)">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
      </div>

      <div className="artifact-body">
        {mode === 'html' && (
          <iframe
            className="artifact-iframe"
            srcDoc={content}
            sandbox="allow-scripts"
            title="HTML Preview"
          />
        )}
        {mode === 'markdown' && (
          <div className="artifact-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </div>
        )}
        {mode === 'code' && (
          <div className="artifact-code">
            <pre><code>{content}</code></pre>
          </div>
        )}
      </div>
    </div>
  )
}
