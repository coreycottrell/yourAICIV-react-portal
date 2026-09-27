export const AUTH_TOKEN_KEY = 'youraiciv-portal-token'
export const THEME_KEY = 'youraiciv-theme'
export const SETTINGS_KEY = 'youraiciv-settings'
export const WELCOME_DISMISSED_KEY = 'youraiciv-welcome-dismissed'

/** Keys used by the generic AiCIV portal this app was derived from. */
const LEGACY_KEYS: Record<string, string> = {
  'aiciv-portal-token': AUTH_TOKEN_KEY,
  'aiciv-theme': THEME_KEY,
  'aiciv-settings': SETTINGS_KEY,
}

/** Carry a login/theme over from the generic portal so upgrades don't log people out. */
export function migrateLegacyStorage(): void {
  try {
    for (const [oldKey, newKey] of Object.entries(LEGACY_KEYS)) {
      const v = localStorage.getItem(oldKey)
      if (v !== null && localStorage.getItem(newKey) === null) {
        localStorage.setItem(newKey, v)
      }
    }
  } catch {
    // storage unavailable (private mode): nothing to migrate
  }
}

/** Starter prompts shown under the chat box. Editable in Settings. */
export const DEFAULT_QUICKFIRE_PILLS = [
  'What did you get done today?',
  "What's on my schedule?",
  'Check my inbox',
  'Ideas to grow my business this week',
]

export const DAYS_OF_WEEK = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const
export const RECUR_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const
