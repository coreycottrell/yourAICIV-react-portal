import { create } from 'zustand'
import * as api from '../api/docs'
import type { Doc, CreateDocRequest, UpdateDocRequest } from '../types/docs'

interface DocsState {
  docs: Doc[]
  selectedDoc: Doc | null
  loading: boolean
  search: string
  visibilityFilter: string
  editing: boolean
  creating: boolean

  setSearch: (q: string) => void
  setVisibilityFilter: (v: string) => void
  setSelectedDoc: (doc: Doc | null) => void
  setEditing: (v: boolean) => void
  setCreating: (v: boolean) => void

  loadDocs: () => Promise<void>
  createDoc: (req: CreateDocRequest) => Promise<boolean>
  updateDoc: (docId: string, req: UpdateDocRequest) => Promise<boolean>
  deleteDoc: (docId: string) => Promise<boolean>
}

export const useDocsStore = create<DocsState>((set, get) => ({
  docs: [],
  selectedDoc: null,
  loading: false,
  search: '',
  visibilityFilter: '',
  editing: false,
  creating: false,

  setSearch: (q) => set({ search: q }),
  setVisibilityFilter: (v) => set({ visibilityFilter: v }),
  setSelectedDoc: (doc) => set({ selectedDoc: doc, editing: false }),
  setEditing: (v) => set({ editing: v }),
  setCreating: (v) => set({ creating: v }),

  loadDocs: async () => {
    set({ loading: true })
    try {
      const filter = get().visibilityFilter
      const data = await api.fetchDocs(filter ? { visibility: filter } : undefined)
      const docs = Array.isArray(data) ? data : []
      set({ docs, loading: false })
    } catch {
      set({ loading: false })
    }
  },

  createDoc: async (req: CreateDocRequest) => {
    try {
      const doc = await api.createDoc(req)
      if (doc?.id) {
        set({ creating: false, selectedDoc: doc })
        await get().loadDocs()
        return true
      }
      return false
    } catch {
      return false
    }
  },

  updateDoc: async (docId: string, req: UpdateDocRequest) => {
    try {
      const doc = await api.updateDoc(docId, req)
      if (doc?.id) {
        set({ selectedDoc: doc, editing: false })
        await get().loadDocs()
        return true
      }
      return false
    } catch {
      return false
    }
  },

  deleteDoc: async (docId: string) => {
    try {
      await api.deleteDoc(docId)
      set({ selectedDoc: null })
      await get().loadDocs()
      return true
    } catch {
      return false
    }
  },
}))
