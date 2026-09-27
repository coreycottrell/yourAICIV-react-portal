import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useTrialStore } from '../../stores/trialStore'
import { EmptyState } from '../common/EmptyState'

/**
 * Operator tools that reach the AI's shell or sessions (Terminal, Sessions,
 * Browser). During a trial the server refuses them (HTTP 403 / WS 4403);
 * this only keeps the page from trying.
 */
export function OperatorOnly({ children }: { children: ReactNode }) {
  const trial = useTrialStore(s => s.status.trial)
  if (!trial) return <>{children}</>
  return (
    <EmptyState
      title="Not available during the free trial"
      description="Operator tools are turned on when you subscribe."
      action={<Link to="/settings" className="settings-link">Back to Settings</Link>}
    />
  )
}
