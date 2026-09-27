import { describe, it, expect, vi, beforeEach } from 'vitest'
import { apiFetch, apiGet, apiPost, apiPut, apiPatch, apiDelete } from '../api/client'

const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => {
  mockFetch.mockReset()
  localStorage.clear()
})

describe('apiFetch', () => {
  it('sends Authorization header when token exists', async () => {
    localStorage.setItem('youraiciv-portal-token', 'my-token')
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({ data: 'test' }),
    })

    await apiFetch('/api/test')

    expect(mockFetch).toHaveBeenCalledWith('/api/test', expect.objectContaining({
      headers: expect.objectContaining({
        Authorization: 'Bearer my-token',
      }),
    }))
  })

  it('does not send Authorization header without token', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({}),
    })

    await apiFetch('/api/test')

    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.headers.Authorization).toBeUndefined()
  })

  it('sets Content-Type for JSON body', async () => {
    localStorage.setItem('youraiciv-portal-token', 'tok')
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({}),
    })

    await apiFetch('/api/test', {
      method: 'POST',
      body: JSON.stringify({ key: 'value' }),
    })

    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.headers['Content-Type']).toBe('application/json')
  })

  it('throws on non-ok responses', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      headers: new Headers(),
      text: () => Promise.resolve('Internal Server Error'),
    })

    await expect(apiFetch('/api/fail')).rejects.toThrow('API error 500')
  })

  it('handles 401 by clearing token', async () => {
    localStorage.setItem('youraiciv-portal-token', 'old-token')
    const reloadMock = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload: reloadMock },
      writable: true,
    })

    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      text: () => Promise.resolve('unauthorized'),
    })

    await expect(apiFetch('/api/test')).rejects.toThrow('Unauthorized')
    expect(localStorage.getItem('youraiciv-portal-token')).toBeNull()
  })

  it('returns text for non-JSON responses', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: () => Promise.resolve('plain text response'),
    })

    const result = await apiFetch<string>('/api/test')
    expect(result).toBe('plain text response')
  })
})

describe('convenience methods', () => {
  const jsonResponse = (data: unknown) => ({
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve(data),
  })

  it('apiGet sends GET request', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ items: [] }))
    const result = await apiGet<{ items: [] }>('/api/items')
    expect(result).toEqual({ items: [] })
    expect(mockFetch.mock.calls[0][1].method).toBeUndefined() // GET is default
  })

  it('apiPost sends POST with body', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }))
    await apiPost('/api/create', { name: 'test' })
    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ name: 'test' })
  })

  it('apiPut sends PUT with body', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }))
    await apiPut('/api/update/1', { name: 'updated' })
    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.method).toBe('PUT')
  })

  it('apiPatch sends PATCH with body', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }))
    await apiPatch('/api/patch/1', { status: 'done' })
    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.method).toBe('PATCH')
  })

  it('apiDelete sends DELETE request', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }))
    await apiDelete('/api/delete/1')
    const [, opts] = mockFetch.mock.calls[0]
    expect(opts.method).toBe('DELETE')
  })
})
