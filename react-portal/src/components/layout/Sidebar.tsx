import { NavLink } from 'react-router-dom'
import { useMailStore } from '../../stores/mailStore'
import { useBookmarkStore } from '../../stores/bookmarkStore'
import { cn } from '../../utils/cn'
import { Icon } from '../common/Icon'
import { NAV_GROUPS, SETTINGS_ITEM, type NavItem } from './nav'
import { SUPPORT_URL, SUPPORT_LABEL } from '../../utils/brand'
import './Sidebar.css'

function SidebarLink({ item, badge }: { item: NavItem; badge?: number }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === '/'}
      className={({ isActive }) => cn('sidebar-link', isActive && 'sidebar-link-active')}
    >
      <Icon name={item.icon} className="sidebar-icon" />
      <span className="sidebar-label">{item.label}</span>
      {badge ? <span className="sidebar-badge">{badge}</span> : null}
    </NavLink>
  )
}

export function Sidebar() {
  const unreadCount = useMailStore(s => s.unreadCount)
  const bookmarkCount = useBookmarkStore(s => s.bookmarks.length)

  const badgeFor = (to: string) =>
    to === '/mail' ? unreadCount : to === '/bookmarks' ? bookmarkCount : 0

  return (
    <aside className="sidebar" aria-label="Main navigation">
      <nav className="sidebar-nav">
        {NAV_GROUPS.map(group => (
          <div className="sidebar-group" key={group.label}>
            <div className="sidebar-group-label">{group.label}</div>
            {group.items.map(item => (
              <SidebarLink key={item.to} item={item} badge={badgeFor(item.to)} />
            ))}
          </div>
        ))}
      </nav>
      <div className="sidebar-footer">
        <SidebarLink item={SETTINGS_ITEM} />
        {SUPPORT_URL && (
          <a href={SUPPORT_URL} target="_blank" rel="noopener noreferrer" className="sidebar-link sidebar-help">
            <Icon name="help" className="sidebar-icon" />
            <span className="sidebar-label">{SUPPORT_LABEL}</span>
          </a>
        )}
      </div>
    </aside>
  )
}
