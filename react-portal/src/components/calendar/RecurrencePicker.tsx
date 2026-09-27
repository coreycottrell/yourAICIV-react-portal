import { RECUR_DAYS } from '../../utils/constants'
import { cn } from '../../utils/cn'
import './RecurrencePicker.css'

interface RecurrencePickerProps {
  recurType: 'daily' | 'weekly' | null
  recurTime: string
  recurDays: string[]
  onChange: (vals: { recurType: 'daily' | 'weekly' | null; recurTime: string; recurDays: string[] }) => void
}

export function RecurrencePicker({ recurType, recurTime, recurDays, onChange }: RecurrencePickerProps) {
  return (
    <div className="recurrence-picker">
      <div className="recurrence-type">
        <label className="recurrence-label">Repeat</label>
        <div className="recurrence-btns">
          {(['none', 'daily', 'weekly'] as const).map(t => (
            <button
              key={t}
              type="button"
              className={cn('recurrence-btn', (t === 'none' ? recurType === null : recurType === t) && 'recurrence-btn-active')}
              onClick={() => onChange({ recurType: t === 'none' ? null : t, recurTime, recurDays })}
            >
              {t === 'none' ? 'None' : t === 'daily' ? 'Daily' : 'Weekly'}
            </button>
          ))}
        </div>
      </div>

      {recurType && (
        <div className="recurrence-time">
          <label className="recurrence-label">Time</label>
          <input
            type="time"
            value={recurTime}
            onChange={e => onChange({ recurType, recurTime: e.target.value, recurDays })}
          />
        </div>
      )}

      {recurType === 'weekly' && (
        <div className="recurrence-days">
          <label className="recurrence-label">Days</label>
          <div className="recurrence-day-btns">
            {RECUR_DAYS.map(day => (
              <button
                key={day}
                type="button"
                className={cn('recurrence-day-btn', recurDays.includes(day) && 'recurrence-day-active')}
                onClick={() => {
                  const next = recurDays.includes(day)
                    ? recurDays.filter(d => d !== day)
                    : [...recurDays, day]
                  onChange({ recurType, recurTime, recurDays: next })
                }}
              >
                {day.slice(0, 2)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
