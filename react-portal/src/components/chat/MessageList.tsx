import { useEffect, useRef } from 'react'
import { MessageBubble } from './MessageBubble'
import type { ChatMessage } from '../../types/chat'
import './MessageList.css'

interface MessageListProps {
  messages: ChatMessage[]
  onReact: (msgId: string, emoji: string, text: string, role: 'user' | 'assistant') => void
  highlightIds?: Set<string>
  onPreviewArtifact?: (content: string, language: string) => void
}

export function MessageList({ messages, onReact, highlightIds, onPreviewArtifact }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const autoScrollRef = useRef(true)

  useEffect(() => {
    if (autoScrollRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  const handleScroll = () => {
    const el = containerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100
    autoScrollRef.current = atBottom
  }

  return (
    <div className="msg-list" ref={containerRef} onScroll={handleScroll} role="log" aria-live="polite" aria-label="Chat messages">
      <div className="msg-list-inner">
        {messages.map(msg => (
          <MessageBubble
            key={msg.id}
            message={msg}
            onReact={(emoji) => onReact(msg.id, emoji, msg.text, msg.role)}
            highlight={highlightIds?.has(msg.id)}
            onPreviewArtifact={onPreviewArtifact}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
