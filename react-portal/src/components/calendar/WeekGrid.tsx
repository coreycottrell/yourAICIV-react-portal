import {
  startOfWeek, endOfWeek, eachDayOfInterval, eachHourOfInterval,
  format, isSameDay, isToday, getHours,
  startOfDay, endOfDay,
} from 'date-fns'
import { TaskCard } from './TaskCard'
import { cn } from '../../utils/cn'
import type { ScheduledTask } from '../../types/calendar'
import './WeekGrid.css'

interface WeekGridProps {
  date: Date
  tasks: ScheduledTask[]
  onTimeClick: (date: Date) => void
  onTaskClick: (task: ScheduledTask) => void
}

export function WeekGrid({ date, tasks, onTimeClick, onTaskClick }: WeekGridProps) {
  const weekStart = startOfWeek(date)
  const weekEnd = endOfWeek(date)
  const days = eachDayOfInterval({ start: weekStart, end: weekEnd })
  const hours = eachHourOfInterval({ start: startOfDay(date), end: endOfDay(date) }).slice(0, 24)

  function tasksForDayHour(day: Date, hour: number): ScheduledTask[] {
    return tasks.filter(t => {
      try {
        const d = new Date(t.fire_at)
        return isSameDay(d, day) && getHours(d) === hour
      } catch {
        return false
      }
    })
  }

  return (
    <div className="week-grid">
      <div className="week-header">
        <div className="week-time-gutter" />
        {days.map(day => (
          <div key={day.toISOString()} className={cn('week-header-cell', isToday(day) && 'week-header-today')}>
            <span className="week-header-dow">{format(day, 'EEE')}</span>
            <span className={cn('week-header-date', isToday(day) && 'week-date-today')}>{format(day, 'd')}</span>
          </div>
        ))}
      </div>
      <div className="week-body">
        {hours.map((hour) => {
          const h = getHours(hour)
          return (
            <div key={h} className="week-row">
              <div className="week-time-gutter">
                <span className="week-time-label">{format(hour, 'ha')}</span>
              </div>
              {days.map(day => {
                const cellTasks = tasksForDayHour(day, h)
                return (
                  <div
                    key={day.toISOString()}
                    className="week-cell"
                    onClick={() => {
                      const d = new Date(day)
                      d.setHours(h)
                      onTimeClick(d)
                    }}
                  >
                    {cellTasks.map(t => (
                      <TaskCard key={t.id} task={t} compact onClick={() => onTaskClick(t)} />
                    ))}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}
