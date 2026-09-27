import { apiGet, apiPost, apiPatch, apiDelete, apiFetch } from './client'
import type {
  Workbook,
  Sheet,
  Row,
  RowsResponse,
  CreateWorkbookRequest,
  CreateSheetRequest,
} from '../types/sheets'

// --- Workbooks ---

export async function listWorkbooks(): Promise<Workbook[]> {
  const data = await apiGet<{ workbooks: Workbook[] }>('/api/sheets/workbooks')
  return data.workbooks || []
}

export async function createWorkbook(req: CreateWorkbookRequest): Promise<Workbook> {
  const data = await apiPost<{ workbook: Workbook }>('/api/sheets/workbooks', req)
  return data.workbook
}

export async function getWorkbook(wbId: string): Promise<Workbook> {
  const data = await apiGet<{ workbook: Workbook }>(`/api/sheets/workbooks/${wbId}`)
  return data.workbook
}

export async function deleteWorkbook(wbId: string): Promise<void> {
  await apiDelete(`/api/sheets/workbooks/${wbId}`)
}

// --- Sheets ---

export async function listSheets(wbId: string): Promise<Sheet[]> {
  const data = await apiGet<{ sheets: Sheet[] }>(`/api/sheets/workbooks/${wbId}/sheets`)
  return data.sheets || []
}

export async function createSheet(wbId: string, req: CreateSheetRequest): Promise<Sheet> {
  const data = await apiPost<{ sheet: Sheet }>(`/api/sheets/workbooks/${wbId}/sheets`, req)
  return data.sheet
}

// --- Rows ---

export async function listRows(
  wbId: string,
  shId: string,
  limit = 100,
  offset = 0,
): Promise<RowsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return apiGet<RowsResponse>(`/api/sheets/workbooks/${wbId}/sheets/${shId}/rows?${params}`)
}

export async function createRow(
  wbId: string,
  shId: string,
  data: Record<string, unknown>,
): Promise<Row> {
  const res = await apiPost<{ row: Row }>(
    `/api/sheets/workbooks/${wbId}/sheets/${shId}/rows`,
    { data },
  )
  return res.row
}

export async function updateRow(
  wbId: string,
  shId: string,
  rowId: string,
  data: Record<string, unknown>,
): Promise<Row> {
  const res = await apiPatch<{ row: Row }>(
    `/api/sheets/workbooks/${wbId}/sheets/${shId}/rows/${rowId}`,
    { data },
  )
  return res.row
}

export async function deleteRow(
  wbId: string,
  shId: string,
  rowId: string,
): Promise<void> {
  await apiDelete(`/api/sheets/workbooks/${wbId}/sheets/${shId}/rows/${rowId}`)
}

export async function bulkInsertRows(
  wbId: string,
  shId: string,
  rows: Record<string, unknown>[],
): Promise<{ inserted: number }> {
  return apiPost(`/api/sheets/workbooks/${wbId}/sheets/${shId}/bulk`, { rows })
}

export async function exportSheet(
  wbId: string,
  shId: string,
  format: 'csv' | 'json' = 'csv',
): Promise<string> {
  const params = new URLSearchParams({ format })
  return apiFetch<string>(
    `/api/sheets/workbooks/${wbId}/sheets/${shId}/export?${params}`,
  )
}
