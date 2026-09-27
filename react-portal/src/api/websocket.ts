import { AUTH_TOKEN_KEY } from '../utils/constants'
import type { ChatMessage } from '../types/chat'
import { signalTrialExpired } from './client'

type MessageHandler = (msg: ChatMessage) => void

export class ChatWebSocket {
  private ws: WebSocket | null = null
  private handlers: Set<MessageHandler> = new Set()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelay = 1000
  private maxReconnectDelay = 30000
  private _connected = false

  get connected(): boolean {
    return this._connected
  }

  connect(): void {
    const token = localStorage.getItem(AUTH_TOKEN_KEY)
    if (!token) return

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/chat?token=${token}`

    try {
      this.ws = new WebSocket(url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this._connected = true
      this.reconnectDelay = 1000
    }

    this.ws.onmessage = (event) => {
      try {
        const msg: ChatMessage = JSON.parse(event.data)
        this.handlers.forEach(h => h(msg))
      } catch {
        // ignore malformed messages
      }
    }

    this.ws.onclose = (event) => {
      this._connected = false
      if (event.code === 4402) {
        // Trial expired: stop reconnecting, show the payment screen.
        signalTrialExpired()
        return
      }
      if (event.code !== 4401) {
        this.scheduleReconnect()
      }
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.ws?.close()
    this.ws = null
    this._connected = false
  }

  onMessage(handler: MessageHandler): () => void {
    this.handlers.add(handler)
    return () => this.handlers.delete(handler)
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay)
      this.connect()
    }, this.reconnectDelay)
  }
}

export const chatWs = new ChatWebSocket()
