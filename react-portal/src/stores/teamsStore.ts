import { create } from 'zustand'
import { apiGet, apiPost } from '../api/client'

export interface TmuxPane {
  id: string
  title: string
  target: string
  content: string
}

interface TeamsState {
  panes: TmuxPane[]
  loading: boolean
  loadPanes: () => Promise<void>
  injectMessage: (paneId: string, message: string) => Promise<boolean>
}

export const useTeamsStore = create<TeamsState>((set) => ({
  panes: [],
  loading: false,

  loadPanes: async () => {
    set({ loading: true })
    try {
      const data = await apiGet<{ panes: TmuxPane[] }>('/api/panes')
      set({ panes: data.panes || [], loading: false })
    } catch {
      set({ loading: false })
    }
  },

  injectMessage: async (paneId: string, message: string) => {
    try {
      await apiPost('/api/inject/pane', { pane_id: paneId, message })
      return true
    } catch {
      return false
    }
  },
}))
