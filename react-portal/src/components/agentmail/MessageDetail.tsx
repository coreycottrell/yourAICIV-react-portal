import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { formatDateTime } from '../../utils/time'
import type { MailMessage } from '../../types/agentmail'
import './MessageDetail.css'

interface MessageDetailProps {
  message: MailMessage
  onArchive: () => void
  onReply: () => void
}

export function MessageDetail({ message, onArchive, onReply }: MessageDetailProps) {
  return (
    <div className="msg-detail">
      <div className="msg-detail-header">
        <h2 className="msg-detail-subject">{message.subject}</h2>
        <div className="msg-detail-meta">
          <span className="msg-detail-from">From: <strong>{message.from_agent}</strong></span>
          <span className="msg-detail-to">To: <strong>{message.to_agent}</strong></span>
          <span className="msg-detail-date">{formatDateTime(new Date(message.timestamp))}</span>
        </div>
        <div className="msg-detail-actions">
          <button className="msg-detail-btn" onClick={onReply}>Reply</button>
          <button className="msg-detail-btn msg-detail-archive" onClick={onArchive}>Archive</button>
        </div>
      </div>
      <div className="msg-detail-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
          {message.body}
        </ReactMarkdown>
      </div>
    </div>
  )
}
