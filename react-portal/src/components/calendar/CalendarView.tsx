import { useEffect, useState } from 'react'
import { addMonths, subMonths, addWeeks, subWeeks, addDays, subDays, format } from 'date-fns'
import { useCalendarStore } from '../../stores/calendarStore'
import { MonthGrid } from './MonthGrid'
import { WeekGrid } from './WeekGrid'
import { DayView } from './DayView'
import { TaskModal } from './TaskModal'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { cn } from '../../utils/cn'
import type { ScheduledTask, CalendarViewMode } from '../../types/calendar'
import './CalendarView.css'

export function CalendarView() {
  const { tasks, loading, viewMode, selectedDate, setViewMode, setSelectedDate, loadTasks } = useCalendarStore()
  const [modalOpen, setModalOpen] = useState(false)
  const [editTask, setEditTask] = useState<ScheduledTask | null>(null)
  const [newTaskDate, setNewTaskDate] = useState<Date | undefined>()

  useEffect(() => {
    loadTasks()
    const interval = setInterval(() => loadTasks(), 60_000)
    return () => clearInterval(interval)
  }, [loadTasks])

  const navigate = (dir: 1 | -1) => {
    const fn = dir === 1
      ? viewMode === 'month' ? addMonths : viewMode === 'week' ? addWeeks : addDays
      : viewMode === 'month' ? subMonths : viewMode === 'week' ? subWeeks : subDays
    setSelectedDate(fn(selectedDate, 1))
  }

  const headerLabel =
    viewMode === 'month' ? format(selectedDate, 'MMMM yyyy') :
    viewMode === 'week' ? `Week of ${format(selectedDate, 'MMM d, yyyy')}` :
    format(selectedDate, 'EEEE, MMM d, yyyy')

  const handleDayClick = (date: Date) => {
    if (viewMode === 'month') {
      setSelectedDate(date)
      setViewMode('day')
    }
  }

  const handleTimeClick = (date: Date) => {
    setNewTaskDate(date)
    setEditTask(null)
    setModalOpen(true)
  }

  const handleTaskClick = (task: ScheduledTask) => {
    setEditTask(task)
    setNewTaskDate(undefined)
    setModalOpen(true)
  }

  const handleNew = () => {
    setEditTask(null)
    setNewTaskDate(selectedDate)
    setModalOpen(true)
  }

  return (
    <div className="calendar-view">
      <div className="calendar-toolbar">
        <div className="calendar-toolbar-left">
          <button className="cal-nav-btn" onClick={() => navigate(-1)}>&lt;</button>
          <h2 className="cal-title">{headerLabel}</h2>
          <button className="cal-nav-btn" onClick={() => navigate(1)}>&gt;</button>
          <button className="cal-today-btn" onClick={() => setSelectedDate(new Date())}>Today</button>
        </div>
        <div className="calendar-toolbar-right">
          <div className="cal-view-toggle">
            {(['day', 'week', 'month'] as CalendarViewMode[]).map(m => (
              <button
                key={m}
                className={cn('cal-view-btn', viewMode === m && 'cal-view-btn-active')}
                onClick={() => setViewMode(m)}
              >
                {m.charAt(0).toUpperCase() + m.slice(1)}
              </button>
            ))}
          </div>
          <button className="cal-new-btn" onClick={handleNew}>+ New</button>
        </div>
      </div>

      <div className="calendar-content">
        {loading && tasks.length === 0 ? (
          <div className="calendar-loading"><LoadingSpinner size={32} /></div>
        ) : (
          <>
            {viewMode === 'month' && (
              <MonthGrid date={selectedDate} tasks={tasks} onDayClick={handleDayClick} onTaskClick={handleTaskClick} />
            )}
            {viewMode === 'week' && (
              <WeekGrid date={selectedDate} tasks={tasks} onTimeClick={handleTimeClick} onTaskClick={handleTaskClick} />
            )}
            {viewMode === 'day' && (
              <DayView date={selectedDate} tasks={tasks} onTimeClick={handleTimeClick} onTaskClick={handleTaskClick} />
            )}
          </>
        )}
      </div>

      <TaskModal
        open={modalOpen}
        onClose={() => { setModalOpen(false); setEditTask(null) }}
        task={editTask}
        defaultDate={newTaskDate}
      />
    </div>
  )
}
