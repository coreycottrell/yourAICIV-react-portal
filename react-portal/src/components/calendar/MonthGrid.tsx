import {
  startOfMonth, endOfMonth, startOfWeek, endOfWeek,
  eachDayOfInterval, format, isSameMonth, isSameDay, isToday,
} from 'date-fns'
import { TaskCard } from './TaskCard'
import { cn } from '../../utils/cn'
import type { ScheduledTask } from '../../types/calendar'
import './MonthGrid.css'

interface MonthGridProps {
  date: Date
  tasks: ScheduledTask[]
  onDayClick: (date: Date) => void
  onTaskClick: (task: ScheduledTask) => void
}

export function MonthGrid({ date, tasks, onDayClick, onTaskClick }: MonthGridProps) {
  const monthStart = startOfMonth(date)
  const monthEnd = endOfMonth(date)
  const calStart = startOfWeek(monthStart)
  const calEnd = endOfWeek(monthEnd)
  const days = eachDayOfInterval({ start: calStart, end: calEnd })

  function tasksForDay(day: Date): ScheduledTask[] {
    return tasks.filter(t => {
      try {
        const fireDate = new Date(t.fire_at)
        return isSameDay(fireDate, day)
      } catch {
        return false
      }
    })
  }

  return (
    <div className="month-grid">
      <div className="month-header-row">
        {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(d => (
          <div key={d} className="month-header-cell">{d}</div>
        ))}
      </div>
      <div className="month-body">
        {days.map(day => {
          const dayTasks = tasksForDay(day)
          return (
            <div
              key={day.toISOString()}
              className={cn(
                'month-cell',
                !isSameMonth(day, date) && 'month-cell-muted',
                isToday(day) && 'month-cell-today',
              )}
              onClick={() => onDayClick(day)}
            >
              <span className={cn('month-day-num', isToday(day) && 'month-day-today')}>
                {format(day, 'd')}
              </span>
              <div className="month-cell-tasks">
                {dayTasks.slice(0, 3).map(t => (
                  <TaskCard
                    key={t.id}
                    task={t}
                    compact
                    onClick={() => onTaskClick(t)}
                  />
                ))}
                {dayTasks.length > 3 && (
                  <span className="month-cell-more">+{dayTasks.length - 3} more</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
