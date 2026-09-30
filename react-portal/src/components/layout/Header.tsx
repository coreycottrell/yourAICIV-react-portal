import { Link } from 'react-router-dom'
import { useIdentityStore } from '../../stores/identityStore'
import { StatusBadge } from '../common/StatusBadge'
import { BrandMark } from '../brand/BrandMark'
import { TrialBadge } from '../trial/TrialBadge'
import { ReconnectClaudeButton } from '../auth/ReconnectClaudeButton'
import './Header.css'

export function Header() {
  const { civName, status } = useIdentityStore()
  const online = !!status?.claude_running

  return (
    <header className="header">
      <div className="header-left">
        <Link to="/" className="header-brand-link" aria-label="yourAICIV home">
          <BrandMark size={30} />
        </Link>
        {civName && (
          <span className="header-ai-name" title="Your AI">
            <span className="header-divider" aria-hidden="true" />
            {civName}
          </span>
        )}
      </div>
      <div className="header-right">
        <TrialBadge />
        <ReconnectClaudeButton variant="header" />
        <StatusBadge
          status={online ? 'online' : 'offline'}
          label={online ? 'Working' : 'Offline'}
        />
      </div>
    </header>
  )
}
