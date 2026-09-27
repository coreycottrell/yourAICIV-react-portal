import { apiGet, apiPost } from './client'
import type { ChatMessage, ReactionRequest } from '../types/chat'

export function fetchChatHistory(last = 100): Promise<{ messages: ChatMessage[] }> {
  return apiGet<{ messages: ChatMessage[] }>(`/api/chat/history?last=${last}`)
}

export function sendChatMessage(message: string): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/api/chat/send', { message })
}

export function sendReaction(req: ReactionRequest): Promise<{ ok: boolean; sentiment: string }> {
  return apiPost<{ ok: boolean; sentiment: string }>('/api/reaction', req)
}
