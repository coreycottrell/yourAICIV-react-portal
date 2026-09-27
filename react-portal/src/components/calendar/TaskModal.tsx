import { useState, useEffect, type FormEvent } from 'react'
import { Modal } from '../common/Modal'
import { RecurrencePicker } from './RecurrencePicker'
import { useCalendarStore } from '../../stores/calendarStore'
import { localTimeToUTC } from '../../utils/time'
import { format } from 'date-fns'
import type { ScheduledTask } from '../../types/calendar'
import './TaskModal.css'

interface TaskModalProps {
  open: boolean
  onClose: () => void
  task?: ScheduledTask | null
  defaultDate?: Date
}

export function TaskModal({ open, onClose, task, defaultDate }: TaskModalProps) {
  const { createTask, updateTask, deleteTask } = useCalendarStore()
  const [message, setMessage] = useState('')
  const [fireDate, setFireDate] = useState('')
  const [fireTime, setFireTime] = useState('')
  const [recurType, setRecurType] = useState<'daily' | 'weekly' | null>(null)
  const [recurTime, setRecurTime] = useState('09:00')
  const [recurDays, setRecurDays] = useState<string[]>([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (task) {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load the edited task into the form
      setMessage(task.message)
      const d = new Date(task.fire_at)
      setFireDate(format(d, 'yyyy-MM-dd'))
      setFireTime(format(d, 'HH:mm'))
      setRecurType(task.recur_type)
      setRecurTime(task.recur_time || '09:00')
      setRecurDays(task.recur_days || [])
    } else {
      const d = defaultDate || new Date()
      setMessage('')
      setFireDate(format(d, 'yyyy-MM-dd'))
      setFireTime(format(d, 'HH:mm'))
      setRecurType(null)
      setRecurTime('09:00')
      setRecurDays([])
    }
  }, [task, defaultDate, open])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!message.trim() || !fireDate) return
    setSaving(true)

    const fireAt = localTimeToUTC(new Date(`${fireDate}T${fireTime || '00:00'}`))
    const req = {
      message: message.trim(),
      fire_at: fireAt,
      recur_type: recurType,
      recur_time: recurType ? recurTime : undefined,
      recur_days: recurType === 'weekly' ? recurDays : undefined,
    }

    if (task) {
      await updateTask(task.id, req)
    } else {
      await createTask(req)
    }

    setSaving(false)
    onClose()
  }

  const handleDelete = async () => {
    if (!task) return
    setSaving(true)
    await deleteTask(task.id)
    setSaving(false)
    onClose()
  }

  return (
    <Modal open={open} onClose={onClose} title={task ? 'Edit Task' : 'New Task'} width="480px">
      <form className="task-modal-form" onSubmit={handleSubmit}>
        <div className="task-modal-field">
          <label>Message</label>
          <textarea
            value={message}
            onChange={e => setMessage(e.target.value)}
            placeholder="What should happen?"
            rows={3}
            autoFocus
          />
        </div>
        <div className="task-modal-row">
          <div className="task-modal-field">
            <label>Date</label>
            <input type="date" value={fireDate} onChange={e => setFireDate(e.target.value)} required />
          </div>
          <div className="task-modal-field">
            <label>Time</label>
            <input type="time" value={fireTime} onChange={e => setFireTime(e.target.value)} />
          </div>
        </div>

        <RecurrencePicker
          recurType={recurType}
          recurTime={recurTime}
          recurDays={recurDays}
          onChange={({ recurType: rt, recurTime: rtm, recurDays: rd }) => {
            setRecurType(rt)
            setRecurTime(rtm)
            setRecurDays(rd)
          }}
        />

        <div className="task-modal-actions">
          {task && (
            <button type="button" className="task-modal-delete" onClick={handleDelete} disabled={saving}>
              Delete
            </button>
          )}
          <div className="task-modal-right">
            <button type="button" className="task-modal-cancel" onClick={onClose}>Cancel</button>
            <button type="submit" className="task-modal-save" disabled={saving || !message.trim()}>
              {saving ? 'Saving...' : task ? 'Update' : 'Create'}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  )
}
