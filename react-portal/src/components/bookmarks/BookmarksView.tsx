import { useBookmarkStore } from '../../stores/bookmarkStore'
import { formatRelativeTime } from '../../utils/time'
import { EmptyState } from '../common/EmptyState'
import './BookmarksView.css'

export function BookmarksView() {
  const { bookmarks, remove } = useBookmarkStore()

  if (bookmarks.length === 0) {
    return (
      <div className="bookmarks-empty">
        <EmptyState
          title="No bookmarks yet"
          description="Hover over a chat message and click the bookmark icon to save it"
        />
      </div>
    )
  }

  return (
    <div className="bookmarks-view">
      <h2 className="bookmarks-title">Bookmarks</h2>
      <p className="bookmarks-subtitle">{bookmarks.length} saved message{bookmarks.length !== 1 ? 's' : ''}</p>
      <div className="bookmarks-list">
        {bookmarks.map(b => (
          <div key={b.msgId} className="bookmark-card">
            <div className="bookmark-header">
              <span className={`bookmark-role bookmark-role-${b.role}`}>
                {b.role === 'user' ? 'You' : 'CIV'}
              </span>
              <span className="bookmark-time">{formatRelativeTime(b.timestamp)}</span>
            </div>
            <p className="bookmark-text">{b.text}</p>
            <button className="bookmark-remove" onClick={() => remove(b.msgId)} title="Remove bookmark">
              &times;
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
