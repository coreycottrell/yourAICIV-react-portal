export type Theme = 'dark' | 'light'

export interface BoopConfig {
  enabled: boolean
  interval_minutes: number
}

export interface UserSettings {
  theme: Theme
  quickfire_pills: string[]
  boop_config: BoopConfig
}
