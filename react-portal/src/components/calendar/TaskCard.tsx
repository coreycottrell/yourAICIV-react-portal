import { cn } from '../../utils/cn'
import type { ScheduledTask } from '../../types/calendar'
import './TaskCard.css'

interface TaskCardProps {
  task: ScheduledTask
  compact?: boolean
  onClick: () => void
}

export function TaskCard({ task, compact, onClick }: TaskCardProps) {
  const status = task.status || 'pending'
  const statusClass = `task-${status.replace('_', '-')}`

  return (
    <button
      className={cn('task-card', statusClass, compact && 'task-card-compact')}
      onClick={(e) => { e.stopPropagation(); onClick() }}
    >
      <span className="task-card-dot" />
      <span className="task-card-text">{task.message}</span>
      {task.recur_type && <span className="task-card-recur">{task.recur_type === 'daily' ? 'D' : 'W'}</span>}
    </button>
  )
}
