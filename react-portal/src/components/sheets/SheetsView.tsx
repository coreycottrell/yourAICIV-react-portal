import { useEffect, useState, useRef, useCallback } from 'react'
import { useSheetsStore } from '../../stores/sheetsStore'
import { exportSheet } from '../../api/sheets'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { EmptyState } from '../common/EmptyState'
import { Modal } from '../common/Modal'
import { cn } from '../../utils/cn'
import type { Column } from '../../types/sheets'
import './SheetsView.css'

// ── Inline Cell Editor ──────────────────────────────────────────────────
function CellEditor({
  value,
  onSave,
  onCancel,
}: {
  value: string
  onSave: (v: string) => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const [draft, setDraft] = useState(value)

  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSave(draft)
    } else if (e.key === 'Escape') {
      onCancel()
    } else if (e.key === 'Tab') {
      e.preventDefault()
      onSave(draft)
    }
  }

  return (
    <textarea
      ref={ref}
      className="sheets-cell-input"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => onSave(draft)}
      onKeyDown={handleKeyDown}
      rows={1}
    />
  )
}

// ── Create Workbook Modal ───────────────────────────────────────────────
function CreateWorkbookModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const createWorkbook = useSheetsStore((s) => s.createWorkbook)
  const selectWorkbook = useSheetsStore((s) => s.selectWorkbook)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    setSubmitting(true)
    const wb = await createWorkbook({ name: name.trim(), description: desc.trim() || undefined })
    setSubmitting(false)
    if (wb) {
      await selectWorkbook(wb.id)
      setName('')
      setDesc('')
      onClose()
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New Workbook">
      <form onSubmit={handleSubmit}>
        <div className="sheets-form-group">
          <label>Name</label>
          <input
            className="sheets-form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My Workbook"
            autoFocus
          />
        </div>
        <div className="sheets-form-group">
          <label>Description (optional)</label>
          <input
            className="sheets-form-input"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="What is this workbook for?"
          />
        </div>
        <div className="sheets-form-actions">
          <button type="button" className="sheets-btn sheets-btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="sheets-btn sheets-btn-primary"
            disabled={!name.trim() || submitting}
          >
            {submitting ? 'Creating...' : 'Create'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── Create Sheet Modal ──────────────────────────────────────────────────
function CreateSheetModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const createSheet = useSheetsStore((s) => s.createSheet)
  const selectSheet = useSheetsStore((s) => s.selectSheet)
  const [name, setName] = useState('')
  const [columns, setColumns] = useState<Column[]>([{ name: '', type: 'text' }])
  const [submitting, setSubmitting] = useState(false)

  const addColumn = () => setColumns([...columns, { name: '', type: 'text' }])
  const removeColumn = (i: number) => setColumns(columns.filter((_, idx) => idx !== i))
  const updateColumn = (i: number, field: 'name' | 'type', val: string) => {
    const next = [...columns]
    if (field === 'type') {
      next[i] = { ...next[i], type: val as Column['type'] }
    } else {
      next[i] = { ...next[i], name: val }
    }
    setColumns(next)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const validCols = columns.filter((c) => c.name.trim())
    if (!name.trim() || validCols.length === 0) return
    setSubmitting(true)
    const sheet = await createSheet({ name: name.trim(), columns: validCols })
    setSubmitting(false)
    if (sheet) {
      await selectSheet(sheet.id)
      setName('')
      setColumns([{ name: '', type: 'text' }])
      onClose()
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New Sheet" width="540px">
      <form onSubmit={handleSubmit}>
        <div className="sheets-form-group">
          <label>Sheet Name</label>
          <input
            className="sheets-form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Sheet 1"
            autoFocus
          />
        </div>
        <div className="sheets-form-group">
          <label>Columns</label>
          <div className="sheets-columns-list">
            {columns.map((col, i) => (
              <div key={i} className="sheets-column-row">
                <input
                  className="sheets-form-input"
                  value={col.name}
                  onChange={(e) => updateColumn(i, 'name', e.target.value)}
                  placeholder="Column name"
                />
                <select
                  value={col.type}
                  onChange={(e) => updateColumn(i, 'type', e.target.value)}
                >
                  <option value="text">Text</option>
                  <option value="number">Number</option>
                  <option value="boolean">Boolean</option>
                  <option value="date">Date</option>
                  <option value="json">JSON</option>
                </select>
                {columns.length > 1 && (
                  <button
                    type="button"
                    className="sheets-column-remove"
                    onClick={() => removeColumn(i)}
                    aria-label="Remove column"
                  >
                    &times;
                  </button>
                )}
              </div>
            ))}
          </div>
          <button type="button" className="sheets-add-btn" onClick={addColumn}>
            + Add column
          </button>
        </div>
        <div className="sheets-form-actions">
          <button type="button" className="sheets-btn sheets-btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="sheets-btn sheets-btn-primary"
            disabled={!name.trim() || columns.every((c) => !c.name.trim()) || submitting}
          >
            {submitting ? 'Creating...' : 'Create Sheet'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── Data Grid ───────────────────────────────────────────────────────────
function DataGrid() {
  const rows = useSheetsStore((s) => s.rows)
  const sheets = useSheetsStore((s) => s.sheets)
  const selectedSheetId = useSheetsStore((s) => s.selectedSheetId)
  const loadingRows = useSheetsStore((s) => s.loadingRows)
  const rowsTotal = useSheetsStore((s) => s.rowsTotal)
  const rowsOffset = useSheetsStore((s) => s.rowsOffset)
  const updateRow = useSheetsStore((s) => s.updateRow)
  const deleteRow = useSheetsStore((s) => s.deleteRow)
  const createRow = useSheetsStore((s) => s.createRow)
  const loadRows = useSheetsStore((s) => s.loadRows)

  const [editingCell, setEditingCell] = useState<{ rowId: string; col: string } | null>(null)
  const [addingRow, setAddingRow] = useState(false)
  const [newRowData, setNewRowData] = useState<Record<string, string>>({})

  const currentSheet = sheets.find((s) => s.id === selectedSheetId)
  const columns = currentSheet?.columns || []

  // Derive columns from row data if sheet has no explicit columns
  const effectiveColumns: Column[] =
    columns.length > 0
      ? columns
      : rows.length > 0
        ? Object.keys(rows[0].data).map((k) => ({ name: k, type: 'text' as const }))
        : []

  const handleCellSave = useCallback(
    async (rowId: string, colName: string, value: string) => {
      setEditingCell(null)
      const row = rows.find((r) => r.id === rowId)
      if (!row) return
      const currentVal = String(row.data[colName] ?? '')
      if (value === currentVal) return
      await updateRow(rowId, { ...row.data, [colName]: value })
    },
    [rows, updateRow],
  )

  const handleAddRow = async () => {
    if (Object.values(newRowData).every((v) => !v.trim())) return
    const data: Record<string, unknown> = {}
    for (const col of effectiveColumns) {
      data[col.name] = newRowData[col.name] || ''
    }
    await createRow(data)
    setNewRowData({})
    setAddingRow(false)
  }

  const handleDeleteRow = async (rowId: string) => {
    await deleteRow(rowId)
  }

  const pageSize = 100
  const hasNext = rowsOffset + pageSize < rowsTotal
  const hasPrev = rowsOffset > 0

  if (loadingRows && rows.length === 0) {
    return (
      <div className="sheets-loading">
        <LoadingSpinner size={32} />
      </div>
    )
  }

  if (effectiveColumns.length === 0 && rows.length === 0) {
    return (
      <div className="sheets-empty">
        <EmptyState
          title="Empty sheet"
          description="This sheet has no data yet. Add a row to get started."
          action={
            <button
              className="sheets-btn sheets-btn-primary"
              onClick={() => setAddingRow(true)}
            >
              + Add Row
            </button>
          }
        />
      </div>
    )
  }

  return (
    <>
      <div className="sheets-grid-wrapper">
        <table className="sheets-grid">
          <thead>
            <tr>
              <th className="col-row-num">#</th>
              {effectiveColumns.map((col) => (
                <th key={col.name}>{col.name}</th>
              ))}
              <th className="col-actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={row.id}>
                <td className="sheets-row-num">{rowsOffset + idx + 1}</td>
                {effectiveColumns.map((col) => {
                  const isEditing =
                    editingCell?.rowId === row.id && editingCell?.col === col.name
                  const rawVal = row.data[col.name]
                  const displayVal = rawVal == null ? '' : String(rawVal)
                  return (
                    <td key={col.name}>
                      {isEditing ? (
                        <div className="sheets-cell editing">
                          <CellEditor
                            value={displayVal}
                            onSave={(v) => handleCellSave(row.id, col.name, v)}
                            onCancel={() => setEditingCell(null)}
                          />
                        </div>
                      ) : (
                        <div
                          className="sheets-cell"
                          onClick={() => setEditingCell({ rowId: row.id, col: col.name })}
                        >
                          {displayVal || '\u00A0'}
                        </div>
                      )}
                    </td>
                  )
                })}
                <td className="sheets-row-actions">
                  <button
                    className="sheets-row-delete-btn"
                    onClick={() => handleDeleteRow(row.id)}
                    aria-label="Delete row"
                  >
                    &times;
                  </button>
                </td>
              </tr>
            ))}
            {/* New row being added */}
            {addingRow && (
              <tr className="sheets-row-adding">
                <td className="sheets-row-num">+</td>
                {effectiveColumns.map((col) => (
                  <td key={col.name}>
                    <div className="sheets-cell editing">
                      <input
                        className="sheets-cell-input"
                        value={newRowData[col.name] || ''}
                        onChange={(e) =>
                          setNewRowData((d) => ({ ...d, [col.name]: e.target.value }))
                        }
                        placeholder={col.name}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleAddRow()
                          if (e.key === 'Escape') {
                            setAddingRow(false)
                            setNewRowData({})
                          }
                        }}
                      />
                    </div>
                  </td>
                ))}
                <td className="sheets-row-actions">
                  <button
                    className="sheets-btn sheets-btn-primary"
                    style={{ fontSize: 11, padding: '4px 8px' }}
                    onClick={handleAddRow}
                  >
                    Save
                  </button>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Footer: Add row + pagination */}
      <div className="sheets-add-row">
        {!addingRow && (
          <button
            className="sheets-btn sheets-btn-secondary"
            onClick={() => setAddingRow(true)}
          >
            + Add Row
          </button>
        )}
        {addingRow && (
          <>
            <button className="sheets-btn sheets-btn-primary" onClick={handleAddRow}>
              Save Row
            </button>
            <button
              className="sheets-btn sheets-btn-secondary"
              onClick={() => {
                setAddingRow(false)
                setNewRowData({})
              }}
            >
              Cancel
            </button>
          </>
        )}
      </div>

      {rowsTotal > pageSize && (
        <div className="sheets-pagination">
          <span>
            Showing {rowsOffset + 1}–{Math.min(rowsOffset + pageSize, rowsTotal)} of{' '}
            {rowsTotal} rows
          </span>
          <div className="sheets-pagination-controls">
            <button
              className="sheets-page-btn"
              disabled={!hasPrev}
              onClick={() => loadRows(Math.max(0, rowsOffset - pageSize))}
            >
              Prev
            </button>
            <button
              className="sheets-page-btn"
              disabled={!hasNext}
              onClick={() => loadRows(rowsOffset + pageSize)}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </>
  )
}

// ── Main SheetsView ─────────────────────────────────────────────────────
export function SheetsView() {
  const workbooks = useSheetsStore((s) => s.workbooks)
  const selectedWorkbookId = useSheetsStore((s) => s.selectedWorkbookId)
  const selectedSheetId = useSheetsStore((s) => s.selectedSheetId)
  const sheets = useSheetsStore((s) => s.sheets)
  const loading = useSheetsStore((s) => s.loading)
  const rowsTotal = useSheetsStore((s) => s.rowsTotal)
  const loadWorkbooks = useSheetsStore((s) => s.loadWorkbooks)
  const selectWorkbook = useSheetsStore((s) => s.selectWorkbook)
  const selectSheet = useSheetsStore((s) => s.selectSheet)
  const deleteWorkbook = useSheetsStore((s) => s.deleteWorkbook)

  const [wbModalOpen, setWbModalOpen] = useState(false)
  const [sheetModalOpen, setSheetModalOpen] = useState(false)
  const [exportLoading, setExportLoading] = useState(false)

  useEffect(() => {
    loadWorkbooks()
  }, [loadWorkbooks])

  const currentSheet = sheets.find((s) => s.id === selectedSheetId)

  const handleExport = async (format: 'csv' | 'json') => {
    if (!selectedWorkbookId || !selectedSheetId) return
    setExportLoading(true)
    try {
      const data = await exportSheet(selectedWorkbookId, selectedSheetId, format)
      const blob = new Blob([data], {
        type: format === 'csv' ? 'text/csv' : 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${currentSheet?.name || 'export'}.${format}`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('Export failed:', e)
    }
    setExportLoading(false)
  }

  const handleDeleteWorkbook = async (wbId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!confirm('Delete this workbook and all its sheets? This cannot be undone.')) return
    await deleteWorkbook(wbId)
  }

  return (
    <div className="sheets-view">
      {/* Sidebar */}
      <div className="sheets-sidebar">
        <div className="sheets-sidebar-header">
          <h3>Workbooks</h3>
          <button className="sheets-add-btn" onClick={() => setWbModalOpen(true)}>
            +
          </button>
        </div>
        <div className="sheets-sidebar-list">
          {loading && workbooks.length === 0 && (
            <div className="sheets-loading" style={{ padding: '20px 0' }}>
              <LoadingSpinner size={20} />
            </div>
          )}
          {workbooks.map((wb) => (
            <button
              key={wb.id}
              className={cn('sheets-wb-item', selectedWorkbookId === wb.id && 'active')}
              onClick={() => selectWorkbook(wb.id)}
            >
              <span className="wb-icon">&#x1F4CA;</span>
              <span className="wb-name">{wb.name}</span>
              <span
                className="sheets-wb-delete"
                onClick={(e) => handleDeleteWorkbook(wb.id, e)}
                role="button"
                aria-label="Delete workbook"
              >
                &times;
              </span>
            </button>
          ))}
          {!loading && workbooks.length === 0 && (
            <div style={{ padding: '12px', color: 'var(--text-tertiary)', fontSize: 13 }}>
              No workbooks yet
            </div>
          )}
        </div>

        {/* Sheet tabs */}
        {selectedWorkbookId && sheets.length > 0 && (
          <div className="sheets-tabs">
            <div className="sheets-tabs-label">Sheets</div>
            {sheets.map((sh) => (
              <button
                key={sh.id}
                className={cn('sheets-tab', selectedSheetId === sh.id && 'active')}
                onClick={() => selectSheet(sh.id)}
              >
                {sh.name}
              </button>
            ))}
            <button className="sheets-add-btn" onClick={() => setSheetModalOpen(true)}>
              + Add Sheet
            </button>
          </div>
        )}
        {selectedWorkbookId && sheets.length === 0 && !loading && (
          <div className="sheets-tabs">
            <button className="sheets-add-btn" onClick={() => setSheetModalOpen(true)}>
              + Create first sheet
            </button>
          </div>
        )}
      </div>

      {/* Main area */}
      <div className="sheets-main">
        {selectedSheetId && currentSheet ? (
          <>
            <div className="sheets-toolbar">
              <div>
                <span className="sheets-toolbar-title">{currentSheet.name}</span>
                <span className="sheets-toolbar-meta">
                  {rowsTotal} row{rowsTotal !== 1 ? 's' : ''}
                  {currentSheet.columns?.length
                    ? ` / ${currentSheet.columns.length} col${currentSheet.columns.length !== 1 ? 's' : ''}`
                    : ''}
                </span>
              </div>
              <div className="sheets-toolbar-actions">
                <button
                  className="sheets-btn sheets-btn-secondary"
                  onClick={() => handleExport('csv')}
                  disabled={exportLoading}
                >
                  Export CSV
                </button>
                <button
                  className="sheets-btn sheets-btn-secondary"
                  onClick={() => handleExport('json')}
                  disabled={exportLoading}
                >
                  Export JSON
                </button>
              </div>
            </div>
            <DataGrid />
          </>
        ) : (
          <div className="sheets-empty">
            <EmptyState
              icon={selectedWorkbookId ? undefined : undefined}
              title={
                selectedWorkbookId
                  ? 'Select or create a sheet'
                  : 'Data Dashboard'
              }
              description={
                selectedWorkbookId
                  ? 'Pick a sheet from the sidebar or create a new one.'
                  : 'Select a workbook from the sidebar to view its data, or create a new one to get started.'
              }
              action={
                !selectedWorkbookId ? (
                  <button
                    className="sheets-btn sheets-btn-primary"
                    onClick={() => setWbModalOpen(true)}
                  >
                    + New Workbook
                  </button>
                ) : (
                  <button
                    className="sheets-btn sheets-btn-primary"
                    onClick={() => setSheetModalOpen(true)}
                  >
                    + New Sheet
                  </button>
                )
              }
            />
          </div>
        )}
      </div>

      {/* Modals */}
      <CreateWorkbookModal open={wbModalOpen} onClose={() => setWbModalOpen(false)} />
      <CreateSheetModal open={sheetModalOpen} onClose={() => setSheetModalOpen(false)} />
    </div>
  )
}
