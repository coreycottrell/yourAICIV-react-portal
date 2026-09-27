import { create } from 'zustand'
import type { ChatMessage } from '../types/chat'

const STORAGE_KEY = 'aiciv-bookmarks'

export interface Bookmark {
  msgId: string
  text: string
  role: 'user' | 'assistant'
  timestamp: number
  savedAt: number
}

interface BookmarkState {
  bookmarks: Bookmark[]
  add: (msg: ChatMessage) => void
  remove: (msgId: string) => void
  isBookmarked: (msgId: string) => boolean
}

function loadBookmarks(): Bookmark[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveBookmarks(bookmarks: Bookmark[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(bookmarks))
}

export const useBookmarkStore = create<BookmarkState>((set, get) => ({
  bookmarks: loadBookmarks(),

  add: (msg: ChatMessage) => {
    const existing = get().bookmarks
    if (existing.some(b => b.msgId === msg.id)) return
    const bookmark: Bookmark = {
      msgId: msg.id,
      text: msg.text.slice(0, 300),
      role: msg.role,
      timestamp: msg.timestamp,
      savedAt: Date.now(),
    }
    const updated = [bookmark, ...existing]
    saveBookmarks(updated)
    set({ bookmarks: updated })
  },

  remove: (msgId: string) => {
    const updated = get().bookmarks.filter(b => b.msgId !== msgId)
    saveBookmarks(updated)
    set({ bookmarks: updated })
  },

  isBookmarked: (msgId: string) => {
    return get().bookmarks.some(b => b.msgId === msgId)
  },
}))
