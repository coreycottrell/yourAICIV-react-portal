import { apiGet } from './client'
import type { OrgChartResponse, StatsResponse } from '../types/agents'

export function fetchOrgChart(): Promise<OrgChartResponse> {
  return apiGet<OrgChartResponse>('/api/agents/orgchart')
}

export function fetchStats(): Promise<StatsResponse> {
  return apiGet<StatsResponse>('/api/agents/stats')
}
