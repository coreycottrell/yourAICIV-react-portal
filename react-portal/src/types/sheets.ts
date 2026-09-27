export interface Column {
  name: string
  type: 'text' | 'number' | 'boolean' | 'date' | 'json'
}

export interface Sheet {
  id: string
  name: string
  columns: Column[]
  created_at: string
  row_count?: number
}

export interface Row {
  id: string
  data: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

export interface Workbook {
  id: string
  name: string
  description?: string
  sheets: Sheet[]
  created_at: string
  updated_at?: string
}

export interface CreateWorkbookRequest {
  name: string
  description?: string
}

export interface CreateSheetRequest {
  name: string
  columns: Column[]
}

export interface RowsResponse {
  rows: Row[]
  total: number
  limit: number
  offset: number
}
