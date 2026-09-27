import { create } from 'zustand'
import { fetchOrgChart, fetchStats } from '../api/agents'
import type { Department, StatsResponse } from '../types/agents'

type StatusFilter = 'all' | 'active' | 'working' | 'idle' | 'offline'

interface AgentsState {
  departments: Department[]
  stats: StatsResponse | null
  loading: boolean
  searchQuery: string
  statusFilter: StatusFilter
  expandedDepts: Set<string>

  loadOrgChart: () => Promise<void>
  loadStats: () => Promise<void>
  setSearch: (query: string) => void
  setStatusFilter: (filter: StatusFilter) => void
  toggleDept: (deptName: string) => void

  filteredDepartments: () => Department[]
}

export const useAgentsStore = create<AgentsState>((set, get) => ({
  departments: [],
  stats: null,
  loading: false,
  searchQuery: '',
  statusFilter: 'all',
  expandedDepts: new Set<string>(),

  loadOrgChart: async () => {
    set({ loading: true })
    try {
      const data = await fetchOrgChart()
      set({ departments: data.departments || [], loading: false })
    } catch {
      set({ loading: false })
    }
  },

  loadStats: async () => {
    try {
      const data = await fetchStats()
      set({ stats: data })
    } catch {
      // silent
    }
  },

  setSearch: (query: string) => set({ searchQuery: query }),

  setStatusFilter: (filter: StatusFilter) => set({ statusFilter: filter }),

  toggleDept: (deptName: string) => {
    const current = get().expandedDepts
    const next = new Set(current)
    if (next.has(deptName)) {
      next.delete(deptName)
    } else {
      next.add(deptName)
    }
    set({ expandedDepts: next })
  },

  filteredDepartments: () => {
    const { departments, searchQuery, statusFilter } = get()
    const q = searchQuery.toLowerCase().trim()

    return departments
      .map((dept) => {
        let members = dept.members || []
        let lead = dept.lead

        // Apply status filter
        if (statusFilter !== 'all') {
          members = members.filter((m) => m.status === statusFilter)
          if (lead && lead.status !== statusFilter) lead = null
        }

        // Apply search filter
        if (q) {
          members = members.filter(
            (m) =>
              m.name.toLowerCase().includes(q) ||
              m.id.toLowerCase().includes(q) ||
              (m.current_task && m.current_task.toLowerCase().includes(q)) ||
              (m.description && m.description.toLowerCase().includes(q))
          )
          const leadMatches =
            lead &&
            (lead.name.toLowerCase().includes(q) ||
              lead.id.toLowerCase().includes(q) ||
              (lead.current_task && lead.current_task.toLowerCase().includes(q)) ||
              (lead.description && lead.description.toLowerCase().includes(q)))
          if (!leadMatches) lead = null
        }

        const deptNameMatches = dept.name.toLowerCase().includes(q)

        // If department name matches search, show all (status-filtered) members
        if (q && deptNameMatches) {
          let allMembers = dept.members || []
          let allLead = dept.lead
          if (statusFilter !== 'all') {
            allMembers = allMembers.filter((m) => m.status === statusFilter)
            if (allLead && allLead.status !== statusFilter) allLead = null
          }
          return { ...dept, lead: allLead, members: allMembers, count: allMembers.length + (allLead ? 1 : 0) }
        }

        const totalVisible = members.length + (lead ? 1 : 0)
        if (totalVisible === 0 && (q || statusFilter !== 'all')) return null

        return { ...dept, lead, members, count: totalVisible }
      })
      .filter(Boolean) as Department[]
  },
}))
