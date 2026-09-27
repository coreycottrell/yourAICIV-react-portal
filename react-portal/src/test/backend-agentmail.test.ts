import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * These tests verify the AgentMail API contract — the request/response shapes
 * that the frontend expects from the backend endpoints we added to portal_server.py.
 *
 * They mock fetch to validate the frontend API functions send correct requests
 * and parse responses correctly.
 */

import { fetchInbox, fetchSent, fetchThread, sendMail, updateMail } from '../api/agentmail'

const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => {
  mockFetch.mockReset()
  localStorage.setItem('youraiciv-portal-token', 'test-token')
})

const jsonOk = (data: unknown) => ({
  ok: true,
  status: 200,
  headers: new Headers({ 'content-type': 'application/json' }),
  json: () => Promise.resolve(data),
})

describe('AgentMail API client', () => {
  describe('fetchInbox', () => {
    it('sends GET to /api/agentmail/inbox with auth', async () => {
      const mockMessages = [
        {
          id: 1,
          from_agent: 'nova',
          to_agent: 'orbit',
          subject: 'Welcome',
          body: 'Hello orbit!',
          timestamp: '2026-03-18T12:00:00Z',
          read: false,
          archived: false,
          thread_id: 'thread-123',
        },
      ]

      mockFetch.mockResolvedValueOnce(jsonOk({ messages: mockMessages }))
      const result = await fetchInbox()

      expect(result.messages).toHaveLength(1)
      expect(result.messages[0].from_agent).toBe('nova')
      expect(result.messages[0].read).toBe(false)

      // Verify auth header sent
      const [url, opts] = mockFetch.mock.calls[0]
      expect(url).toBe('/api/agentmail/inbox')
      expect(opts.headers.Authorization).toBe('Bearer test-token')
    })

    it('handles empty inbox', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ messages: [] }))
      const result = await fetchInbox()
      expect(result.messages).toEqual([])
    })
  })

  describe('fetchSent', () => {
    it('sends GET to /api/agentmail/sent', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ messages: [] }))
      await fetchSent()
      expect(mockFetch.mock.calls[0][0]).toBe('/api/agentmail/sent')
    })
  })

  describe('fetchThread', () => {
    it('sends GET to /api/agentmail/thread/{id}', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ messages: [] }))
      await fetchThread('thread-123')
      expect(mockFetch.mock.calls[0][0]).toBe('/api/agentmail/thread/thread-123')
    })
  })

  describe('sendMail', () => {
    it('sends POST with correct body', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ ok: true, id: 42 }))
      const result = await sendMail({
        to_agent: 'nova',
        subject: 'Test',
        body: 'Hello!',
        thread_id: 'thread-456',
      })

      expect(result.ok).toBe(true)
      expect(result.id).toBe(42)

      const [url, opts] = mockFetch.mock.calls[0]
      expect(url).toBe('/api/agentmail/send')
      expect(opts.method).toBe('POST')
      const body = JSON.parse(opts.body)
      expect(body.to_agent).toBe('nova')
      expect(body.subject).toBe('Test')
      expect(body.body).toBe('Hello!')
      expect(body.thread_id).toBe('thread-456')
    })
  })

  describe('updateMail', () => {
    it('sends PATCH to mark as read', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ ok: true }))
      await updateMail(1, { read: true })

      const [url, opts] = mockFetch.mock.calls[0]
      expect(url).toBe('/api/agentmail/1')
      expect(opts.method).toBe('PATCH')
      expect(JSON.parse(opts.body)).toEqual({ read: true })
    })

    it('sends PATCH to archive', async () => {
      mockFetch.mockResolvedValueOnce(jsonOk({ ok: true }))
      await updateMail(5, { archived: true })

      const body = JSON.parse(mockFetch.mock.calls[0][1].body)
      expect(body).toEqual({ archived: true })
    })
  })
})
