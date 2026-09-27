export interface Agent {
  id: string
  name: string
  description: string
  type?: string
  status: 'active' | 'working' | 'idle' | 'offline'
  capabilities?: string[]
  department: string
  is_lead: number
  last_active?: string
  created_at?: string
  current_task?: string
  last_completed?: string
}

export interface Department {
  name: string
  count: number
  lead: Agent | null
  members: Agent[]
}

export interface OrgChartResponse {
  departments: Department[]
  total: number
}

export interface StatsResponse {
  total: number
  active: number
  working: number
  idle: number
  offline: number
  departments: number
}
