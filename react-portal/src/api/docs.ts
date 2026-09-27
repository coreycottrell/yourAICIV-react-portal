import { apiGet, apiPost, apiPut, apiDelete } from './client'
import type { Doc, CreateDocRequest, UpdateDocRequest } from '../types/docs'

export function fetchDocs(params?: { author?: string; visibility?: string; tag?: string }): Promise<Doc[]> {
  const query = new URLSearchParams()
  if (params?.author) query.set('author', params.author)
  if (params?.visibility) query.set('visibility', params.visibility)
  if (params?.tag) query.set('tag', params.tag)
  const qs = query.toString()
  return apiGet<Doc[]>(`/api/docs${qs ? `?${qs}` : ''}`)
}

export function getDoc(docId: string): Promise<Doc> {
  return apiGet<Doc>(`/api/docs/${docId}`)
}

export function createDoc(req: CreateDocRequest): Promise<Doc> {
  return apiPost<Doc>('/api/docs', req)
}

export function updateDoc(docId: string, req: UpdateDocRequest): Promise<Doc> {
  return apiPut<Doc>(`/api/docs/${docId}`, req)
}

export function deleteDoc(docId: string): Promise<{ ok: boolean }> {
  return apiDelete<{ ok: boolean }>(`/api/docs/${docId}`)
}
