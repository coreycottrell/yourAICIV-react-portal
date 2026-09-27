import { create } from 'zustand'
import { fetchChatHistory, sendChatMessage, sendReaction } from '../api/chat'
import { chatWs } from '../api/websocket'
import type { ChatMessage } from '../types/chat'

let wsCleanup: (() => void) | null = null

interface ChatState {
  messages: ChatMessage[]
  loading: boolean
  sending: boolean
  wsConnected: boolean
  error: string | null
  loadHistory: () => Promise<void>
  send: (text: string) => Promise<void>
  react: (msgId: string, emoji: string, msgText: string, msgRole: 'user' | 'assistant') => Promise<void>
  connectWs: () => void
  disconnectWs: () => void
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  loading: false,
  sending: false,
  wsConnected: false,
  error: null,

  loadHistory: async () => {
    set({ loading: true, error: null })
    try {
      const data = await fetchChatHistory(200)
      const msgs = data.messages || []
      set({ messages: msgs, loading: false })
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : 'Failed to load chat' })
    }
  },

  send: async (text: string) => {
    set({ sending: true })
    try {
      await sendChatMessage(text)
      const userMsg: ChatMessage = {
        id: `local-${Date.now()}`,
        text,
        role: 'user',
        timestamp: Date.now() / 1000,
      }
      set(s => ({ messages: [...s.messages, userMsg], sending: false }))
    } catch (e) {
      console.error('[chat] send failed:', e)
      set({ sending: false })
    }
  },

  react: async (msgId: string, emoji: string, msgText: string, msgRole: 'user' | 'assistant') => {
    try {
      await sendReaction({
        msg_id: msgId,
        emoji,
        action: 'add',
        msg_preview: msgText.slice(0, 200),
        msg_role: msgRole,
      })
    } catch (e) {
      console.error('[chat] reaction failed:', e)
    }
  },

  connectWs: () => {
    if (wsCleanup) {
      wsCleanup()
      wsCleanup = null
    }

    chatWs.connect()
    set({ wsConnected: true })

    wsCleanup = chatWs.onMessage((msg) => {
      set((s) => {
        // Check if message already exists by ID
        const idx = s.messages.findIndex(m => m.id === msg.id)
        if (idx >= 0) {
          const updated = [...s.messages]
          updated[idx] = msg
          return { messages: updated }
        }

        // Check if this is a server echo of an optimistic local message:
        // same role + same text content → replace the local one
        if (msg.role === 'user') {
          const localIdx = s.messages.findIndex(
            m => m.id.startsWith('local-') && m.role === 'user' && m.text === msg.text
          )
          if (localIdx >= 0) {
            const updated = [...s.messages]
            updated[localIdx] = msg
            return { messages: updated }
          }
        }

        return { messages: [...s.messages, msg] }
      })
    })
  },

  disconnectWs: () => {
    if (wsCleanup) {
      wsCleanup()
      wsCleanup = null
    }
    chatWs.disconnect()
    set({ wsConnected: false })
  },
}))
