import { useState, useCallback } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useMailStore } from '../../stores/mailStore'
import { cn } from '../../utils/cn'
import { Icon } from '../common/Icon'
import { MOBILE_PRIMARY, MOBILE_MORE } from './nav'
import './MobileNav.css'

export function MobileNav() {
  const unreadCount = useMailStore(s => s.unreadCount)
  const [moreOpen, setMoreOpen] = useState(false)
  const navigate = useNavigate()

  const handleMoreItem = useCallback((to: string) => {
    navigate(to)
    setMoreOpen(false)
  }, [navigate])

  return (
    <>
      {moreOpen && (
        <div className="mobile-more-overlay" onClick={() => setMoreOpen(false)} />
      )}

      {moreOpen && (
        <div className="mobile-more-sheet" role="menu" aria-label="More">
          <div className="mobile-more-handle" />
          <div className="mobile-more-grid">
            {MOBILE_MORE.map(item => (
              <button
                key={item.to}
                className="mobile-more-item"
                onClick={() => handleMoreItem(item.to)}
                type="button"
                role="menuitem"
              >
                <Icon name={item.icon} size={22} className="mobile-more-icon" />
                <span className="mobile-more-label">{item.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <nav className="mobile-nav" aria-label="Main navigation">
        {MOBILE_PRIMARY.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            className={({ isActive }) => cn('mobile-nav-item', isActive && 'mobile-nav-active')}
            onClick={() => setMoreOpen(false)}
          >
            <span className="mobile-nav-icon">
              <Icon name={item.icon} size={22} />
              {item.to === '/mail' && unreadCount > 0 && (
                <span className="mobile-nav-badge">{unreadCount}</span>
              )}
            </span>
            <span className="mobile-nav-label">{item.short ?? item.label}</span>
          </NavLink>
        ))}
        <button
          className={cn('mobile-nav-item', 'mobile-nav-more-btn', moreOpen && 'mobile-nav-active')}
          onClick={() => setMoreOpen(o => !o)}
          type="button"
          aria-expanded={moreOpen}
        >
          <span className="mobile-nav-icon">
            <Icon name={moreOpen ? 'close' : 'more'} size={22} />
          </span>
          <span className="mobile-nav-label">More</span>
        </button>
      </nav>
    </>
  )
}
