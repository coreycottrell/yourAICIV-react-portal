import { create } from 'zustand'
import * as api from '../api/calendar'
import type { ScheduledTask, CreateTaskRequest, UpdateTaskRequest, CalendarViewMode } from '../types/calendar'

/** Normalize task from API — fill in missing optional fields */
function normalizeTask(t: Partial<ScheduledTask> & { id: string; message: string; fire_at: string }): ScheduledTask {
  return {
    status: 'pending',
    created_at: '',
    recur_type: null,
    recur_time: null,
    recur_days: null,
    subtasks: [],
    notes: [],
    order: 0,
    completion_pct: 0,
    ...t,
  }
}

interface CalendarState {
  tasks: ScheduledTask[]
  loading: boolean
  viewMode: CalendarViewMode
  selectedDate: Date
  setViewMode: (mode: CalendarViewMode) => void
  setSelectedDate: (date: Date) => void
  loadTasks: () => Promise<void>
  createTask: (req: CreateTaskRequest) => Promise<string | null>
  updateTask: (id: string, req: CreateTaskRequest) => Promise<boolean>
  patchTask: (id: string, req: UpdateTaskRequest) => Promise<boolean>
  deleteTask: (id: string) => Promise<boolean>
}

export const useCalendarStore = create<CalendarState>((set, get) => ({
  tasks: [],
  loading: false,
  viewMode: 'month',
  selectedDate: new Date(),

  setViewMode: (mode) => set({ viewMode: mode }),
  setSelectedDate: (date) => set({ selectedDate: date }),

  loadTasks: async () => {
    set({ loading: true })
    try {
      const data = await api.fetchTasks()
      set({ tasks: (data.tasks || []).map(normalizeTask), loading: false })
    } catch {
      set({ loading: false })
    }
  },

  createTask: async (req) => {
    try {
      const res = await api.createTask(req)
      if (res.ok) {
        await get().loadTasks()
        return res.task_id
      }
      return null
    } catch {
      return null
    }
  },

  updateTask: async (id, req) => {
    try {
      const res = await api.updateTask(id, req)
      if (res.ok) {
        await get().loadTasks()
        return true
      }
      return false
    } catch {
      return false
    }
  },

  patchTask: async (id, req) => {
    try {
      const res = await api.patchTask(id, req)
      if (res.ok) {
        await get().loadTasks()
        return true
      }
      return false
    } catch {
      return false
    }
  },

  deleteTask: async (id) => {
    try {
      const res = await api.deleteTask(id)
      if (res.ok) {
        set(s => ({ tasks: s.tasks.filter(t => t.id !== id) }))
        return true
      }
      return false
    } catch {
      return false
    }
  },
}))
