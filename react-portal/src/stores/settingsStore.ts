import { create } from 'zustand'
import { THEME_KEY, SETTINGS_KEY, DEFAULT_QUICKFIRE_PILLS } from '../utils/constants'
import type { Theme } from '../types/settings'

/** Saved choice, else follow the OS. */
function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* ignore */ }
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark'
}

interface SettingsState {
  theme: Theme
  quickfirePills: string[]
  boopEnabled: boolean
  setTheme: (theme: Theme) => void
  setQuickfirePills: (pills: string[]) => void
  setBoopEnabled: (v: boolean) => void
  loadFromStorage: () => void
}

export const useSettingsStore = create<SettingsState>((set) => ({
  theme: initialTheme(),
  quickfirePills: (() => {
    try {
      const s = localStorage.getItem(SETTINGS_KEY)
      return s ? JSON.parse(s).quickfirePills || DEFAULT_QUICKFIRE_PILLS : DEFAULT_QUICKFIRE_PILLS
    } catch {
      return DEFAULT_QUICKFIRE_PILLS
    }
  })(),
  boopEnabled: false,

  setTheme: (theme) => {
    localStorage.setItem(THEME_KEY, theme)
    document.documentElement.setAttribute('data-theme', theme)
    set({ theme })
  },

  setQuickfirePills: (pills) => {
    try {
      const existing = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...existing, quickfirePills: pills }))
    } catch { /* ignore */ }
    set({ quickfirePills: pills })
  },

  setBoopEnabled: (v) => set({ boopEnabled: v }),

  loadFromStorage: () => {
    const theme = initialTheme()
    document.documentElement.setAttribute('data-theme', theme)
    set({ theme })
  },
}))
