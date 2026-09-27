import { apiGet, apiPost, apiPatch, apiDelete } from './client'
import type { ScheduledTask, CreateTaskRequest, UpdateTaskRequest } from '../types/calendar'

/** AgentCal event shape from the API */
interface AgentCalEvent {
  id: string
  summary: string
  description?: string
  start: string
  end: string
  status?: string
  recurrence?: string | null
  prompt_payload?: { type?: string; text?: string; command?: string; [k: string]: unknown }
  metadata?: { event_category?: string; priority?: string; [k: string]: unknown }
  created_at?: string
}

/** Convert AgentCal event to ScheduledTask for existing calendar components */
function eventToTask(evt: AgentCalEvent): ScheduledTask {
  // Extract recurrence info from RRULE
  let recurType: 'daily' | 'weekly' | null = null
  let recurDays: string[] | null = null
  let recurTime: string | null = null
  if (evt.recurrence) {
    if (evt.recurrence.includes('FREQ=DAILY')) recurType = 'daily'
    else if (evt.recurrence.includes('FREQ=WEEKLY')) recurType = 'weekly'
    const byDay = evt.recurrence.match(/BYDAY=([A-Z,]+)/)
    if (byDay) {
      const map: Record<string, string> = { MO: 'Mon', TU: 'Tue', WE: 'Wed', TH: 'Thu', FR: 'Fri', SA: 'Sat', SU: 'Sun' }
      recurDays = byDay[1].split(',').map(d => map[d] || d)
    }
    const timeMatch = evt.start.match(/T(\d{2}:\d{2})/)
    if (timeMatch) recurTime = timeMatch[1]
  }

  // Build message from summary + prompt_payload
  const payload = evt.prompt_payload
  let message = evt.summary
  if (payload?.text) message = payload.text
  else if (payload?.command) message = payload.command
  else if (evt.description) message = `${evt.summary}\n\n${evt.description}`

  return {
    id: evt.id,
    message,
    fire_at: evt.start,
    created_at: evt.created_at || '',
    recur_type: recurType,
    recur_time: recurTime,
    recur_days: recurDays,
    status: 'pending',
    subtasks: [],
    notes: [],
    order: 0,
    completion_pct: 0,
  }
}

/** Convert CreateTaskRequest to AgentCal event body */
function taskToEvent(req: CreateTaskRequest): Record<string, unknown> {
  const startStr = req.fire_at
  // Parse start to compute end (+5min default)
  let endStr: string
  try {
    const d = new Date(startStr)
    d.setMinutes(d.getMinutes() + 5)
    endStr = d.toISOString()
  } catch {
    endStr = startStr
  }

  // Build RRULE
  let recurrence: string | undefined
  if (req.recur_type === 'daily') {
    recurrence = 'RRULE:FREQ=DAILY'
  } else if (req.recur_type === 'weekly' && req.recur_days?.length) {
    const map: Record<string, string> = { Mon: 'MO', Tue: 'TU', Wed: 'WE', Thu: 'TH', Fri: 'FR', Sat: 'SA', Sun: 'SU' }
    const byday = req.recur_days.map(d => map[d] || d).join(',')
    recurrence = `RRULE:FREQ=WEEKLY;BYDAY=${byday}`
  }

  // Split message into summary + prompt_payload
  const lines = req.message.trim().split('\n')
  const summary = lines[0].slice(0, 512)

  return {
    summary,
    start: startStr,
    end: endStr,
    description: lines.length > 1 ? lines.slice(1).join('\n').trim() : undefined,
    prompt_payload: { type: 'prompt_injection', text: req.message },
    recurrence,
  }
}

// --- AgentCal-backed API functions ---

export async function fetchTasks(): Promise<{ tasks: ScheduledTask[] }> {
  // Fetch a wide window (60 days back, 60 days forward) to cover all views
  const now = new Date()
  const min = new Date(now)
  min.setDate(min.getDate() - 60)
  const max = new Date(now)
  max.setDate(max.getDate() + 60)
  const params = new URLSearchParams({
    time_min: min.toISOString(),
    time_max: max.toISOString(),
    limit: '250',
  })
  const data = await apiGet<{ events: AgentCalEvent[] }>(`/api/agentcal/events?${params}`)
  return { tasks: (data.events || []).map(eventToTask) }
}

export async function createTask(req: CreateTaskRequest): Promise<{ ok: boolean; task_id: string }> {
  const body = taskToEvent(req)
  const res = await apiPost<{ ok: boolean; event: AgentCalEvent }>('/api/agentcal/events', body)
  return { ok: res.ok, task_id: res.event?.id || '' }
}

export async function updateTask(id: string, req: CreateTaskRequest): Promise<{ ok: boolean; task: ScheduledTask }> {
  const body = taskToEvent(req)
  const res = await apiPatch<{ ok: boolean; event: AgentCalEvent }>(`/api/agentcal/events/${id}`, body)
  return { ok: res.ok, task: res.event ? eventToTask(res.event) : {} as ScheduledTask }
}

export async function patchTask(id: string, req: UpdateTaskRequest): Promise<{ ok: boolean; task: ScheduledTask }> {
  // Map UpdateTaskRequest fields to AgentCal patch
  const body: Record<string, unknown> = {}
  if (req.message !== undefined) {
    body.summary = req.message.split('\n')[0].slice(0, 512)
    body.prompt_payload = { type: 'prompt_injection', text: req.message }
  }
  if (req.fire_at !== undefined) body.start = req.fire_at
  if (req.status !== undefined) body.status = req.status
  const res = await apiPatch<{ ok: boolean; event: AgentCalEvent }>(`/api/agentcal/events/${id}`, body)
  return { ok: res.ok, task: res.event ? eventToTask(res.event) : {} as ScheduledTask }
}

export async function deleteTask(id: string): Promise<{ ok: boolean }> {
  return apiDelete<{ ok: boolean }>(`/api/agentcal/events/${id}`)
}
