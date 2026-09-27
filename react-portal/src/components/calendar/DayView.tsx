import {
  eachHourOfInterval, startOfDay, endOfDay, format,
  isSameDay, getHours, isToday,
} from 'date-fns'
import { TaskCard } from './TaskCard'
import { cn } from '../../utils/cn'
import type { ScheduledTask } from '../../types/calendar'
import './DayView.css'

interface DayViewProps {
  date: Date
  tasks: ScheduledTask[]
  onTimeClick: (date: Date) => void
  onTaskClick: (task: ScheduledTask) => void
}

export function DayView({ date, tasks, onTimeClick, onTaskClick }: DayViewProps) {
  const hours = eachHourOfInterval({ start: startOfDay(date), end: endOfDay(date) }).slice(0, 24)

  function tasksForHour(hour: number): ScheduledTask[] {
    return tasks.filter(t => {
      try {
        const d = new Date(t.fire_at)
        return isSameDay(d, date) && getHours(d) === hour
      } catch {
        return false
      }
    })
  }

  return (
    <div className="day-view">
      <div className="day-view-header">
        <span className={cn('day-view-date', isToday(date) && 'day-view-today')}>
          {format(date, 'EEEE, MMMM d')}
        </span>
      </div>
      <div className="day-view-body">
        {hours.map(hour => {
          const h = getHours(hour)
          const hourTasks = tasksForHour(h)
          return (
            <div
              key={h}
              className="day-view-row"
              onClick={() => {
                const d = new Date(date)
                d.setHours(h)
                onTimeClick(d)
              }}
            >
              <div className="day-view-time">{format(hour, 'h a')}</div>
              <div className="day-view-content">
                {hourTasks.map(t => (
                  <TaskCard key={t.id} task={t} onClick={() => onTaskClick(t)} />
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
