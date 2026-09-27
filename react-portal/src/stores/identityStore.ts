import { create } from 'zustand'
import { fetchCommands, fetchStatus } from '../api/identity'
import type { StatusResponse, CommandsResponse } from '../types/identity'

interface IdentityState {
  civName: string
  humanName: string
  status: StatusResponse | null
  commands: CommandsResponse | null
  loading: boolean
  fetchIdentity: () => Promise<void>
  fetchStatusInfo: () => Promise<void>
}

export const useIdentityStore = create<IdentityState>((set) => ({
  civName: '',
  humanName: '',
  status: null,
  commands: null,
  loading: false,

  fetchIdentity: async () => {
    set({ loading: true })
    try {
      const cmds = await fetchCommands()
      set({
        commands: cmds,
        civName: cmds.civ?.name || 'AiCIV',
        humanName: cmds.civ?.human_name || '',
        loading: false,
      })
    } catch {
      set({ loading: false })
    }
  },

  fetchStatusInfo: async () => {
    try {
      const s = await fetchStatus()
      set({ status: s })
    } catch {
      // silently fail status polling
    }
  },
}))
