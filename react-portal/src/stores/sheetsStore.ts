import { create } from 'zustand'
import * as api from '../api/sheets'
import type {
  Workbook,
  Sheet,
  Row,
  CreateWorkbookRequest,
  CreateSheetRequest,
} from '../types/sheets'

interface SheetsState {
  workbooks: Workbook[]
  selectedWorkbookId: string | null
  selectedSheetId: string | null
  sheets: Sheet[]
  rows: Row[]
  rowsTotal: number
  rowsOffset: number
  loading: boolean
  loadingRows: boolean
  error: string | null

  // Actions
  loadWorkbooks: () => Promise<void>
  selectWorkbook: (wbId: string) => Promise<void>
  selectSheet: (shId: string) => Promise<void>
  createWorkbook: (req: CreateWorkbookRequest) => Promise<Workbook | null>
  deleteWorkbook: (wbId: string) => Promise<boolean>
  createSheet: (req: CreateSheetRequest) => Promise<Sheet | null>
  loadRows: (offset?: number) => Promise<void>
  createRow: (data: Record<string, unknown>) => Promise<Row | null>
  updateRow: (rowId: string, data: Record<string, unknown>) => Promise<Row | null>
  deleteRow: (rowId: string) => Promise<boolean>
}

export const useSheetsStore = create<SheetsState>((set, get) => ({
  workbooks: [],
  selectedWorkbookId: null,
  selectedSheetId: null,
  sheets: [],
  rows: [],
  rowsTotal: 0,
  rowsOffset: 0,
  loading: false,
  loadingRows: false,
  error: null,

  loadWorkbooks: async () => {
    set({ loading: true, error: null })
    try {
      const workbooks = await api.listWorkbooks()
      set({ workbooks, loading: false })
    } catch (e) {
      set({ loading: false, error: String(e) })
    }
  },

  selectWorkbook: async (wbId: string) => {
    set({ selectedWorkbookId: wbId, selectedSheetId: null, sheets: [], rows: [], rowsTotal: 0, rowsOffset: 0, loading: true, error: null })
    try {
      const sheets = await api.listSheets(wbId)
      set({ sheets, loading: false })
      // Auto-select first sheet
      if (sheets.length > 0) {
        await get().selectSheet(sheets[0].id)
      }
    } catch (e) {
      set({ loading: false, error: String(e) })
    }
  },

  selectSheet: async (shId: string) => {
    set({ selectedSheetId: shId, rows: [], rowsTotal: 0, rowsOffset: 0 })
    await get().loadRows(0)
  },

  createWorkbook: async (req) => {
    try {
      const wb = await api.createWorkbook(req)
      set((s) => ({ workbooks: [...s.workbooks, wb] }))
      return wb
    } catch (e) {
      set({ error: String(e) })
      return null
    }
  },

  deleteWorkbook: async (wbId) => {
    try {
      await api.deleteWorkbook(wbId)
      const s = get()
      set({
        workbooks: s.workbooks.filter((w) => w.id !== wbId),
        ...(s.selectedWorkbookId === wbId
          ? { selectedWorkbookId: null, selectedSheetId: null, sheets: [], rows: [], rowsTotal: 0 }
          : {}),
      })
      return true
    } catch (e) {
      set({ error: String(e) })
      return false
    }
  },

  createSheet: async (req) => {
    const wbId = get().selectedWorkbookId
    if (!wbId) return null
    try {
      const sheet = await api.createSheet(wbId, req)
      set((s) => ({ sheets: [...s.sheets, sheet] }))
      return sheet
    } catch (e) {
      set({ error: String(e) })
      return null
    }
  },

  loadRows: async (offset = 0) => {
    const { selectedWorkbookId, selectedSheetId } = get()
    if (!selectedWorkbookId || !selectedSheetId) return
    set({ loadingRows: true, error: null })
    try {
      const resp = await api.listRows(selectedWorkbookId, selectedSheetId, 100, offset)
      set({
        rows: resp.rows || [],
        rowsTotal: resp.total || 0,
        rowsOffset: offset,
        loadingRows: false,
      })
    } catch (e) {
      set({ loadingRows: false, error: String(e) })
    }
  },

  createRow: async (data) => {
    const { selectedWorkbookId, selectedSheetId } = get()
    if (!selectedWorkbookId || !selectedSheetId) return null
    try {
      const row = await api.createRow(selectedWorkbookId, selectedSheetId, data)
      set((s) => ({ rows: [...s.rows, row], rowsTotal: s.rowsTotal + 1 }))
      return row
    } catch (e) {
      set({ error: String(e) })
      return null
    }
  },

  updateRow: async (rowId, data) => {
    const { selectedWorkbookId, selectedSheetId } = get()
    if (!selectedWorkbookId || !selectedSheetId) return null
    try {
      const row = await api.updateRow(selectedWorkbookId, selectedSheetId, rowId, data)
      set((s) => ({
        rows: s.rows.map((r) => (r.id === rowId ? { ...r, data: { ...r.data, ...data } } : r)),
      }))
      return row
    } catch (e) {
      set({ error: String(e) })
      return null
    }
  },

  deleteRow: async (rowId) => {
    const { selectedWorkbookId, selectedSheetId } = get()
    if (!selectedWorkbookId || !selectedSheetId) return false
    try {
      await api.deleteRow(selectedWorkbookId, selectedSheetId, rowId)
      set((s) => ({
        rows: s.rows.filter((r) => r.id !== rowId),
        rowsTotal: s.rowsTotal - 1,
      }))
      return true
    } catch (e) {
      set({ error: String(e) })
      return false
    }
  },
}))
