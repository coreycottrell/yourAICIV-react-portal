export interface ScheduledTask {
  id: string
  message: string
  fire_at: string
  created_at: string
  recur_type: 'daily' | 'weekly' | null
  recur_time: string | null
  recur_days: string[] | null
  status: 'pending' | 'in_progress' | 'completed'
  subtasks: Subtask[]
  notes: TaskNote[]
  order: number
  completion_pct: number
}

export interface Subtask {
  id: string
  text: string
  done: boolean
}

export interface TaskNote {
  text: string
  ts: number
}

export interface CreateTaskRequest {
  message: string
  fire_at: string
  recur_type?: 'daily' | 'weekly' | null
  recur_time?: string
  recur_days?: string[]
}

export interface UpdateTaskRequest {
  message?: string
  fire_at?: string
  recur_type?: 'daily' | 'weekly' | null
  recur_time?: string
  recur_days?: string[]
  status?: 'pending' | 'in_progress' | 'completed'
  subtasks?: Subtask[]
  note?: string
  notes?: TaskNote[]
  order?: number
  completion_pct?: number
}

export type CalendarViewMode = 'day' | 'week' | 'month'
