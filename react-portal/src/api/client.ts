import { AUTH_TOKEN_KEY } from '../utils/constants'

/** Fired on window when the server says the trial is over (HTTP 402 / WS 4402). */
export const TRIAL_EXPIRED_EVENT = 'youraiciv:trial-expired'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function signalTrialExpired(): void {
  window.dispatchEvent(new Event(TRIAL_EXPIRED_EVENT))
}

function getToken(): string | null {
  return localStorage.getItem(AUTH_TOKEN_KEY)
}

function getBaseUrl(): string {
  // In production, API is on the same origin
  // In dev, Vite proxy handles it
  return ''
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> || {}),
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  if (options.body && typeof options.body === 'string') {
    headers['Content-Type'] = 'application/json'
  }

  const res = await fetch(`${getBaseUrl()}${path}`, {
    ...options,
    headers,
  })

  // Only 401 means "wrong access code". A 403 is a refusal of one action
  // (e.g. an operator tool during the trial) and must not sign the client out.
  if (res.status === 401) {
    localStorage.removeItem(AUTH_TOKEN_KEY)
    window.location.reload()
    throw new ApiError(res.status, 'Unauthorized')
  }

  // Trial over: keep the token (it works again the moment they subscribe),
  // just tell the app to show the payment screen.
  if (res.status === 402) {
    signalTrialExpired()
    throw new ApiError(402, 'Trial expired')
  }

  if (!res.ok) {
    const text = await res.text()
    throw new ApiError(res.status, `API error ${res.status}: ${text}`)
  }

  const contentType = res.headers.get('content-type')
  if (contentType?.includes('application/json')) {
    return res.json()
  }

  return res.text() as unknown as T
}

export function apiGet<T>(path: string): Promise<T> {
  return apiFetch<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function apiPut<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function apiDelete<T>(path: string): Promise<T> {
  return apiFetch<T>(path, { method: 'DELETE' })
}

export function uploadFile(file: File): Promise<{ ok: boolean; filename: string; url: string }> {
  const token = getToken()
  const formData = new FormData()
  formData.append('file', file)

  return fetch('/api/chat/upload', {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  }).then(r => {
    if (r.status === 402) {
      signalTrialExpired()
      throw new ApiError(402, 'Trial expired')
    }
    return r.json()
  })
}
