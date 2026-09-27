import { create } from 'zustand'
import { fetchTrial, NOT_A_TRIAL, type TrialStatus } from '../api/trial'

interface TrialState {
  status: TrialStatus
  /** true once the first /api/trial answer (or failure) is in */
  ready: boolean
  refresh: () => Promise<void>
  /** Called when any request comes back 402: lock now, then confirm with the server. */
  markExpired: () => void
}

export const useTrialStore = create<TrialState>((set, get) => ({
  status: NOT_A_TRIAL,
  ready: false,

  refresh: async () => {
    try {
      const status = await fetchTrial()
      set({ status, ready: true })
    } catch {
      // Endpoint missing/unreachable: never block on our own failure.
      // (A real expiry is still enforced by the server with 402s.)
      set({ ready: true })
    }
  },

  markExpired: () => {
    const { status } = get()
    set({ status: { ...status, trial: true, expired: true }, ready: true })
    void get().refresh()
  },
}))
