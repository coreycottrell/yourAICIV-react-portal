import { apiGet, apiPost } from './client'

export interface EvolutionStatusResponse {
  status: 'pending' | 'in_progress' | 'complete'
}

export interface EvolutionFireResponse {
  status: 'fired' | 'already_evolved' | 'already_fired'
  message?: string
  error?: string
}

export function fetchEvolutionStatus(): Promise<EvolutionStatusResponse> {
  return apiGet<EvolutionStatusResponse>('/api/evolution/status')
}

export function fireFirstBoot(): Promise<EvolutionFireResponse> {
  return apiPost<EvolutionFireResponse>('/api/evolution/first-boot')
}
