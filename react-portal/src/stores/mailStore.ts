import { create } from 'zustand'
import * as api from '../api/agentmail'
import type { MailMessage, SendMailRequest } from '../types/agentmail'

type MailFolder = 'inbox' | 'sent'

const READ_IDS_KEY = 'aiciv-mail-read-ids'

function getReadIds(): Set<string> {
  try {
    const data = localStorage.getItem(READ_IDS_KEY)
    return new Set(data ? JSON.parse(data) : [])
  } catch {
    return new Set()
  }
}

function saveReadId(id: string | number) {
  const ids = getReadIds()
  ids.add(String(id))
  // Keep only last 500
  const arr = [...ids].slice(-500)
  localStorage.setItem(READ_IDS_KEY, JSON.stringify(arr))
}

function applyReadState(msgs: MailMessage[]): MailMessage[] {
  const readIds = getReadIds()
  return msgs.map(m => ({
    ...m,
    read: m.read || readIds.has(String(m.id)),
  }))
}

interface MailState {
  inbox: MailMessage[]
  sent: MailMessage[]
  selectedMessage: MailMessage | null
  thread: MailMessage[]
  folder: MailFolder
  composing: boolean
  loading: boolean
  unreadCount: number
  setFolder: (folder: MailFolder) => void
  setSelectedMessage: (msg: MailMessage | null) => void
  setComposing: (v: boolean) => void
  loadInbox: () => Promise<void>
  loadSent: () => Promise<void>
  loadThread: (threadId: string) => Promise<void>
  sendMail: (req: SendMailRequest) => Promise<boolean>
  markRead: (id: string | number) => Promise<void>
  archive: (id: string | number) => Promise<void>
}

export const useMailStore = create<MailState>((set, get) => ({
  inbox: [],
  sent: [],
  selectedMessage: null,
  thread: [],
  folder: 'inbox',
  composing: false,
  loading: false,
  unreadCount: 0,

  setFolder: (folder) => set({ folder, selectedMessage: null }),
  setSelectedMessage: (msg) => set({ selectedMessage: msg }),
  setComposing: (v) => set({ composing: v }),

  loadInbox: async () => {
    set({ loading: true })
    try {
      const data = await api.fetchInbox()
      const msgs = applyReadState(data.messages || [])
      set({
        inbox: msgs,
        unreadCount: msgs.filter(m => !m.read).length,
        loading: false,
      })
    } catch {
      set({ loading: false })
    }
  },

  loadSent: async () => {
    set({ loading: true })
    try {
      const data = await api.fetchSent()
      set({ sent: data.messages || [], loading: false })
    } catch {
      set({ loading: false })
    }
  },

  loadThread: async (threadId: string) => {
    try {
      const data = await api.fetchThread(threadId)
      set({ thread: data.messages || [] })
    } catch {
      // silently fail
    }
  },

  sendMail: async (req: SendMailRequest) => {
    try {
      const res = await api.sendMail(req)
      if (res.ok) {
        set({ composing: false })
        await get().loadSent()
        return true
      }
      return false
    } catch {
      return false
    }
  },

  markRead: async (id: string | number) => {
    saveReadId(id)
    set(s => ({
      inbox: s.inbox.map(m => String(m.id) === String(id) ? { ...m, read: true } : m),
      unreadCount: Math.max(0, s.unreadCount - 1),
    }))
  },

  archive: async (id: string | number) => {
    try {
      await api.updateMail(id as number, { archived: true })
    } catch {
      // no-op — server endpoint is a compatibility stub
    }
    set(s => ({
      inbox: s.inbox.filter(m => String(m.id) !== String(id)),
      selectedMessage: String(s.selectedMessage?.id) === String(id) ? null : s.selectedMessage,
    }))
  },
}))
