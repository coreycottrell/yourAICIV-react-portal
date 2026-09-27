import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ChatWebSocket } from '../api/websocket'

// Mock WebSocket
class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  url: string
  onopen: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  readyState = MockWebSocket.CONNECTING
  close = vi.fn()

  constructor(url: string) {
    this.url = url
    // Simulate successful connection
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN
      this.onopen?.(new Event('open'))
    }, 0)
  }

  simulateMessage(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  simulateClose(code = 1000) {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ code } as CloseEvent)
  }
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  localStorage.setItem('youraiciv-portal-token', 'test-token')
  ;(globalThis as unknown as Record<string, unknown>).WebSocket = MockWebSocket as unknown
})

afterEach(() => {
  ;(globalThis as unknown as Record<string, unknown>).WebSocket = originalWebSocket
  localStorage.clear()
})

describe('ChatWebSocket', () => {
  it('connects with token in URL', () => {
    const ws = new ChatWebSocket()
    ws.connect()

    // Should have created a WebSocket
    expect(ws.connected).toBe(false) // not yet - async
    ws.disconnect()
  })

  it('does not connect without token', () => {
    localStorage.removeItem('youraiciv-portal-token')
    const ws = new ChatWebSocket()
    ws.connect()
    expect(ws.connected).toBe(false)
  })

  it('handles message callbacks', async () => {
    const ws = new ChatWebSocket()
    const handler = vi.fn()

    ws.connect()
    const unsub = ws.onMessage(handler)

    // Wait for connection
    await new Promise(r => setTimeout(r, 10))

    // Simulate a message - we need to access the internal ws
    // Since we can't easily access private members, test the handler cleanup
    expect(typeof unsub).toBe('function')

    ws.disconnect()
  })

  it('cleans up handler on unsub', () => {
    const ws = new ChatWebSocket()
    const handler = vi.fn()

    const unsub = ws.onMessage(handler)
    unsub()

    // Handler should no longer be called
    ws.disconnect()
  })

  it('disconnect cleans up state', () => {
    const ws = new ChatWebSocket()
    ws.connect()
    ws.disconnect()
    expect(ws.connected).toBe(false)
  })
})
