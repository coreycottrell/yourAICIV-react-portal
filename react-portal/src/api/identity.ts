import { apiGet } from './client'
import type { StatusResponse, CommandsResponse } from '../types/identity'

export function fetchStatus(): Promise<StatusResponse> {
  return apiGet<StatusResponse>('/api/status')
}

export function fetchCommands(): Promise<CommandsResponse> {
  return apiGet<CommandsResponse>('/api/commands')
}
