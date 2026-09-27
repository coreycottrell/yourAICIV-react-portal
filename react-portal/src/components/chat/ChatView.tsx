import { useEffect, useState, useMemo, useCallback } from 'react'
import { useChatStore } from '../../stores/chatStore'
import { uploadFile } from '../../api/client'
import { MessageList } from './MessageList'
import { ChatInput } from './ChatInput'
import { SearchPanel } from './SearchPanel'
import { ArtifactPanel } from './ArtifactPanel'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { WelcomePanel } from './WelcomePanel'
import { Icon } from '../common/Icon'
import './ChatView.css'

export function ChatView() {
  const messages = useChatStore(s => s.messages)
  const loading = useChatStore(s => s.loading)
  const sending = useChatStore(s => s.sending)
  const loadHistory = useChatStore(s => s.loadHistory)
  const send = useChatStore(s => s.send)
  const react = useChatStore(s => s.react)
  const connectWs = useChatStore(s => s.connectWs)
  const disconnectWs = useChatStore(s => s.disconnectWs)
  const [showSearch, setShowSearch] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [artifactContent, setArtifactContent] = useState('')
  const [artifactLanguage, setArtifactLanguage] = useState('')
  const [artifactOpen, setArtifactOpen] = useState(false)

  const handlePreviewArtifact = useCallback((content: string, language: string) => {
    setArtifactContent(content)
    setArtifactLanguage(language)
    setArtifactOpen(true)
  }, [])

  const handleCloseArtifact = useCallback(() => {
    setArtifactOpen(false)
  }, [])

  useEffect(() => {
    loadHistory()
    connectWs()
    return () => disconnectWs()
  }, [loadHistory, connectWs, disconnectWs])

  const handleUpload = async (file: File) => {
    try {
      const res = await uploadFile(file)
      if (res.ok) {
        send(`[Uploaded file: ${res.filename}](${res.url})`)
      }
    } catch {
      // silently fail
    }
  }

  const { matchCount, highlightIds } = useMemo(() => {
    if (!searchQuery.trim()) return { matchCount: 0, highlightIds: new Set<string>() }
    const q = searchQuery.toLowerCase()
    const ids = new Set<string>()
    for (const msg of messages) {
      if (msg.text.toLowerCase().includes(q)) {
        ids.add(msg.id)
      }
    }
    return { matchCount: ids.size, highlightIds: ids }
  }, [messages, searchQuery])

  return (
    <div className={`chat-view ${artifactOpen ? 'chat-with-artifact' : ''}`}>
      <div className="chat-main-area">
        <div className="chat-header-bar">
          <button
            className={`chat-search-toggle ${showSearch ? 'chat-search-toggle-active' : ''}`}
            onClick={() => {
              setShowSearch(v => !v)
              if (showSearch) setSearchQuery('')
            }}
            title="Search messages"
            aria-label="Search messages"
          >
            <Icon name="search" size={18} />
          </button>
        </div>
        {showSearch && (
          <SearchPanel
            onSearch={setSearchQuery}
            matchCount={matchCount}
            onClose={() => { setShowSearch(false); setSearchQuery('') }}
          />
        )}
        {loading && messages.length === 0 ? (
          <div className="chat-loading">
            <LoadingSpinner size={32} />
          </div>
        ) : messages.length === 0 ? (
          <div className="chat-empty">
            <WelcomePanel onPick={send} />
          </div>
        ) : (
          <MessageList messages={messages} onReact={react} highlightIds={highlightIds} onPreviewArtifact={handlePreviewArtifact} />
        )}
        <ChatInput onSend={send} onUpload={handleUpload} sending={sending} />
      </div>
      {artifactOpen && (
        <ArtifactPanel
          content={artifactContent}
          language={artifactLanguage}
          onClose={handleCloseArtifact}
        />
      )}
    </div>
  )
}
