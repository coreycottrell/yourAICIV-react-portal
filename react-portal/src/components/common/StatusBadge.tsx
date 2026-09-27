import { cn } from '../../utils/cn'
import './StatusBadge.css'

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'busy' | 'idle'
  label?: string
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  return (
    <span className={cn('status-badge', `status-${status}`)}>
      <span className="status-dot" />
      {label && <span className="status-label">{label}</span>}
    </span>
  )
}
