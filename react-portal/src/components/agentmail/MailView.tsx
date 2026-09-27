import { useEffect, useState } from 'react'
import { useMailStore } from '../../stores/mailStore'
import { MailSidebar } from './MailSidebar'
import { MailMessageList } from './MailMessageList'
import { MessageDetail } from './MessageDetail'
import { ComposeModal } from './ComposeModal'
import { EmptyState } from '../common/EmptyState'
import { LoadingSpinner } from '../common/LoadingSpinner'
import './MailView.css'

export function MailView() {
  const {
    inbox, sent, folder, selectedMessage, composing, loading,
    loadInbox, loadSent, setSelectedMessage, setComposing, markRead, archive,
  } = useMailStore()

  const [replyTo, setReplyTo] = useState<{ to_agent: string; subject: string; thread_id?: string | null } | undefined>()

  useEffect(() => {
    loadInbox()
    loadSent()
  }, [loadInbox, loadSent])

  const messages = folder === 'inbox' ? inbox : sent

  const handleSelect = (msg: typeof messages[0]) => {
    setSelectedMessage(msg)
    if (!msg.read && folder === 'inbox') {
      markRead(msg.id)
    }
  }

  const handleBack = () => {
    setSelectedMessage(null)
  }

  const handleReply = () => {
    if (!selectedMessage) return
    setReplyTo({
      to_agent: selectedMessage.from_agent,
      subject: selectedMessage.subject,
      thread_id: selectedMessage.thread_id,
    })
    setComposing(true)
  }


  // On mobile: show either list OR detail, not both
  const showMobileDetail = !!selectedMessage

  return (
    <div className="mail-view">
      <MailSidebar />
      <div className="mail-content">
        {loading && messages.length === 0 ? (
          <div className="mail-loading"><LoadingSpinner size={32} /></div>
        ) : (
          <div className="mail-split">
            <div className={`mail-list-pane ${showMobileDetail ? 'mail-list-pane--hidden-mobile' : ''}`}>
              <MailMessageList
                messages={messages}
                selectedId={selectedMessage?.id ?? null}
                onSelect={handleSelect}
              />
            </div>
            <div className={`mail-detail-pane ${showMobileDetail ? 'mail-detail-pane--visible-mobile' : ''}`}>
              {selectedMessage ? (
                <>
                  <button className="mail-back-btn" onClick={handleBack} type="button">
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                      <path d="M10 3L5 8L10 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                    Back
                  </button>
                  <MessageDetail
                    message={selectedMessage}
                    onArchive={() => archive(selectedMessage.id)}
                    onReply={handleReply}
                  />
                </>
              ) : (
                <EmptyState
                  title="Select a message"
                  description="Choose a message from the list to read it"
                />
              )}
            </div>
          </div>
        )}
      </div>

      <ComposeModal
        open={composing}
        onClose={() => { setComposing(false); setReplyTo(undefined) }}
        replyTo={replyTo}
      />
    </div>
  )
}
