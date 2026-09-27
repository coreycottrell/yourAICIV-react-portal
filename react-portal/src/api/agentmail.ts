import { apiGet, apiPost, apiPatch } from './client'
import type { MailMessage, SendMailRequest, UpdateMailRequest } from '../types/agentmail'

export function fetchInbox(): Promise<{ messages: MailMessage[] }> {
  return apiGet<{ messages: MailMessage[] }>('/api/agentmail/inbox')
}

export function fetchSent(): Promise<{ messages: MailMessage[] }> {
  return apiGet<{ messages: MailMessage[] }>('/api/agentmail/sent')
}

export function fetchThread(threadId: string): Promise<{ messages: MailMessage[] }> {
  return apiGet<{ messages: MailMessage[] }>(`/api/agentmail/thread/${threadId}`)
}

export function sendMail(req: SendMailRequest): Promise<{ ok: boolean; id: number }> {
  return apiPost<{ ok: boolean; id: number }>('/api/agentmail/send', req)
}

export function updateMail(id: number, req: UpdateMailRequest): Promise<{ ok: boolean }> {
  return apiPatch<{ ok: boolean }>(`/api/agentmail/${id}`, req)
}
