import { cn } from '../../utils/cn'
import { formatRelativeTime, isoToEpochSeconds } from '../../utils/time'
import type { MailMessage } from '../../types/agentmail'
import './MailMessageList.css'

interface MailMessageListProps {
  messages: MailMessage[]
  selectedId: string | number | null
  onSelect: (msg: MailMessage) => void
}

export function MailMessageList({ messages, selectedId, onSelect }: MailMessageListProps) {
  if (messages.length === 0) {
    return <div className="mail-list-empty">No messages</div>
  }

  return (
    <div className="mail-list">
      {messages.map(msg => (
        <button
          key={msg.id}
          className={cn(
            'mail-list-item',
            !msg.read && 'mail-list-unread',
            selectedId === msg.id && 'mail-list-selected',
          )}
          onClick={() => onSelect(msg)}
        >
          <div className="mail-list-top">
            <span className="mail-list-from">{msg.from_agent}</span>
            <span className="mail-list-time">{formatRelativeTime(isoToEpochSeconds(msg.timestamp))}</span>
          </div>
          <div className="mail-list-subject">{msg.subject}</div>
          <div className="mail-list-preview">{msg.body.slice(0, 80)}</div>
        </button>
      ))}
    </div>
  )
}
