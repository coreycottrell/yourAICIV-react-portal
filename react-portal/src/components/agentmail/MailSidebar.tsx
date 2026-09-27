import { useMailStore } from '../../stores/mailStore'
import { cn } from '../../utils/cn'
import './MailSidebar.css'

export function MailSidebar() {
  const { folder, setFolder, unreadCount, setComposing } = useMailStore()

  return (
    <div className="mail-sidebar">
      <button className="mail-compose-btn" onClick={() => setComposing(true)}>
        Compose
      </button>
      <nav className="mail-folders">
        <button
          className={cn('mail-folder-btn', folder === 'inbox' && 'mail-folder-active')}
          onClick={() => setFolder('inbox')}
        >
          <span>Inbox</span>
          {unreadCount > 0 && <span className="mail-folder-badge">{unreadCount}</span>}
        </button>
        <button
          className={cn('mail-folder-btn', folder === 'sent' && 'mail-folder-active')}
          onClick={() => setFolder('sent')}
        >
          <span>Sent</span>
        </button>
      </nav>
    </div>
  )
}
