import { apiGet, apiPost } from './client'
import type { UserSettings } from '../types/settings'

export function fetchSettings(): Promise<UserSettings> {
  return apiGet<UserSettings>('/api/settings')
}

export function saveSettings(settings: Partial<UserSettings>): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/api/settings', settings)
}

export function fetchBoopConfig(): Promise<{ enabled: boolean }> {
  return apiGet<{ enabled: boolean }>('/api/boop/config')
}

export function toggleBoop(enabled: boolean): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/api/boop/toggle', { enabled })
}
