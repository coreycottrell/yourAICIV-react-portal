import { useEffect, useState, useMemo, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useDocsStore } from '../../stores/docsStore'
import { EmptyState } from '../common/EmptyState'
import { LoadingSpinner } from '../common/LoadingSpinner'
import { Modal } from '../common/Modal'
import { cn } from '../../utils/cn'
import type { CreateDocRequest } from '../../types/docs'
import './DocsView.css'

const VISIBILITY_OPTIONS = ['public', 'private', 'civ-only']
const TAG_COLORS = [
  'var(--accent-primary)',
  'var(--accent-secondary, #8b5cf6)',
  'var(--accent-success, #10b981)',
  'var(--accent-warning, #f59e0b)',
  'var(--accent-error, #ef4444)',
  '#06b6d4',
  '#ec4899',
  '#6366f1',
]

function tagColor(tag: string): string {
  let hash = 0
  for (let i = 0; i < tag.length; i++) {
    hash = tag.charCodeAt(i) + ((hash << 5) - hash)
  }
  return TAG_COLORS[Math.abs(hash) % TAG_COLORS.length]
}

function formatDate(iso?: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  } catch {
    return iso
  }
}

export function DocsView() {
  const {
    docs, selectedDoc, loading, search, visibilityFilter,
    editing, creating,
    setSearch, setVisibilityFilter, setSelectedDoc, setEditing, setCreating,
    loadDocs, createDoc, updateDoc, deleteDoc,
  } = useDocsStore()

  // Sort state
  const [sortMode, setSortMode] = useState<'recent' | 'alpha'>('recent')

  // Edit state
  const [editTitle, setEditTitle] = useState('')
  const [editContent, setEditContent] = useState('')
  const [editVisibility, setEditVisibility] = useState('public')
  const [editTags, setEditTags] = useState('')

  // Create modal state
  const [newTitle, setNewTitle] = useState('')
  const [newContent, setNewContent] = useState('')
  const [newVisibility, setNewVisibility] = useState('public')
  const [newTags, setNewTags] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    loadDocs()
  }, [loadDocs])

  // Sync edit fields when entering edit mode
  useEffect(() => {
    if (editing && selectedDoc) {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- copy the doc into the edit form on entering edit mode
      setEditTitle(selectedDoc.title)
      setEditContent(selectedDoc.content)
      setEditVisibility(selectedDoc.visibility)
      setEditTags(selectedDoc.tags?.join(', ') || '')
    }
  }, [editing, selectedDoc])

  const filteredDocs = useMemo(() => {
    let result = docs
    if (search) {
      const q = search.toLowerCase()
      result = result.filter(
        (d) =>
          d.title.toLowerCase().includes(q) ||
          d.tags?.some((t) => t.toLowerCase().includes(q)) ||
          d.content?.toLowerCase().includes(q),
      )
    }
    const sorted = [...result]
    if (sortMode === 'alpha') {
      sorted.sort((a, b) => a.title.localeCompare(b.title))
    } else {
      sorted.sort((a, b) => {
        const ta = a.updated_at || a.created_at || ''
        const tb = b.updated_at || b.created_at || ''
        return tb.localeCompare(ta)
      })
    }
    return sorted
  }, [docs, search, sortMode])

  const handleSelect = useCallback(
    (doc: typeof docs[0]) => {
      setSelectedDoc(doc)
    },
    [setSelectedDoc],
  )

  const handleBack = useCallback(() => {
    setSelectedDoc(null)
  }, [setSelectedDoc])

  const handleSaveEdit = async () => {
    if (!selectedDoc) return
    setSaving(true)
    await updateDoc(selectedDoc.id, {
      title: editTitle,
      content: editContent,
      visibility: editVisibility,
      tags: editTags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
    })
    setSaving(false)
  }

  const handleCreate = async () => {
    if (!newTitle.trim()) return
    setSaving(true)
    const req: CreateDocRequest = {
      title: newTitle.trim(),
      content: newContent,
      visibility: newVisibility,
      tags: newTags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
    }
    const ok = await createDoc(req)
    if (ok) {
      setNewTitle('')
      setNewContent('')
      setNewVisibility('public')
      setNewTags('')
    }
    setSaving(false)
  }

  const handleDelete = async () => {
    if (!selectedDoc) return
    if (!window.confirm(`Delete "${selectedDoc.title}"?`)) return
    await deleteDoc(selectedDoc.id)
  }

  const showMobileDetail = !!selectedDoc

  return (
    <div className="docs-view">
      {/* ── Left sidebar: doc list ── */}
      <div className={cn('docs-list-pane', showMobileDetail && 'docs-list-pane--hidden-mobile')}>
        <div className="docs-list-header">
          <h2 className="docs-list-title">Knowledge Base</h2>
          <button className="docs-create-btn" onClick={() => setCreating(true)} type="button">
            + New
          </button>
        </div>

        <div className="docs-filters">
          <input
            className="docs-search"
            type="text"
            placeholder="Search docs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            className="docs-visibility-filter"
            value={visibilityFilter}
            onChange={(e) => {
              setVisibilityFilter(e.target.value)
              // Reload with new filter
              setTimeout(() => loadDocs(), 0)
            }}
          >
            <option value="">All visibility</option>
            {VISIBILITY_OPTIONS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
          <select
            className="docs-sort-filter"
            value={sortMode}
            onChange={(e) => setSortMode(e.target.value as 'recent' | 'alpha')}
          >
            <option value="recent">Recent first</option>
            <option value="alpha">A — Z</option>
          </select>
        </div>

        {loading && docs.length === 0 ? (
          <div className="docs-loading">
            <LoadingSpinner size={24} />
          </div>
        ) : filteredDocs.length === 0 ? (
          <div className="docs-empty-list">
            <EmptyState
              title={search ? 'No matches' : 'No documents yet'}
              description={search ? 'Try a different search term' : 'Create your first document'}
            />
          </div>
        ) : (
          <div className="docs-list">
            {filteredDocs.map((doc) => (
              <button
                key={doc.id}
                className={cn('docs-list-item', selectedDoc?.id === doc.id && 'docs-list-item--active')}
                onClick={() => handleSelect(doc)}
                type="button"
              >
                <div className="docs-list-item-title">{doc.title}</div>
                <div className="docs-list-item-meta">
                  <span className="docs-list-item-visibility">{doc.visibility}</span>
                  {doc.updated_at && (
                    <span className="docs-list-item-date">{formatDate(doc.updated_at)}</span>
                  )}
                </div>
                {doc.tags && doc.tags.length > 0 && (
                  <div className="docs-list-item-tags">
                    {doc.tags.slice(0, 3).map((tag) => (
                      <span
                        key={tag}
                        className="docs-tag-chip"
                        style={{ '--tag-color': tagColor(tag) } as React.CSSProperties}
                      >
                        {tag}
                      </span>
                    ))}
                    {doc.tags.length > 3 && (
                      <span className="docs-tag-more">+{doc.tags.length - 3}</span>
                    )}
                  </div>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ── Right pane: detail / editor ── */}
      <div className={cn('docs-detail-pane', showMobileDetail && 'docs-detail-pane--visible-mobile')}>
        {selectedDoc ? (
          <>
            <button className="docs-back-btn" onClick={handleBack} type="button">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path
                  d="M10 3L5 8L10 13"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Back
            </button>

            <div className="docs-detail-content">
              {editing ? (
                /* ── Edit mode ── */
                <div className="docs-editor">
                  <input
                    className="docs-editor-title"
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    placeholder="Document title"
                  />
                  <div className="docs-editor-row">
                    <select
                      className="docs-editor-visibility"
                      value={editVisibility}
                      onChange={(e) => setEditVisibility(e.target.value)}
                    >
                      {VISIBILITY_OPTIONS.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                    <input
                      className="docs-editor-tags"
                      type="text"
                      value={editTags}
                      onChange={(e) => setEditTags(e.target.value)}
                      placeholder="Tags (comma-separated)"
                    />
                  </div>
                  <textarea
                    className="docs-editor-body"
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    placeholder="Write markdown content..."
                    rows={20}
                  />
                  <div className="docs-editor-actions">
                    <button
                      className="docs-btn docs-btn--primary"
                      onClick={handleSaveEdit}
                      disabled={saving}
                      type="button"
                    >
                      {saving ? 'Saving...' : 'Save'}
                    </button>
                    <button
                      className="docs-btn docs-btn--secondary"
                      onClick={() => setEditing(false)}
                      type="button"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                /* ── Read mode ── */
                <div className="docs-reader">
                  <div className="docs-reader-header">
                    <h1 className="docs-reader-title">{selectedDoc.title}</h1>
                    <div className="docs-reader-actions">
                      <button
                        className="docs-btn docs-btn--secondary"
                        onClick={() => setEditing(true)}
                        type="button"
                      >
                        Edit
                      </button>
                      <button
                        className="docs-btn docs-btn--danger"
                        onClick={handleDelete}
                        type="button"
                      >
                        Delete
                      </button>
                    </div>
                  </div>

                  <div className="docs-reader-meta">
                    <span className="docs-reader-visibility">{selectedDoc.visibility}</span>
                    {selectedDoc.author && (
                      <span className="docs-reader-author">by {selectedDoc.author}</span>
                    )}
                    {selectedDoc.created_at && (
                      <span className="docs-reader-date">
                        Created {formatDate(selectedDoc.created_at)}
                      </span>
                    )}
                    {selectedDoc.updated_at && (
                      <span className="docs-reader-date">
                        Updated {formatDate(selectedDoc.updated_at)}
                      </span>
                    )}
                  </div>

                  {selectedDoc.tags && selectedDoc.tags.length > 0 && (
                    <div className="docs-reader-tags">
                      {selectedDoc.tags.map((tag) => (
                        <span
                          key={tag}
                          className="docs-tag-chip"
                          style={{ '--tag-color': tagColor(tag) } as React.CSSProperties}
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="docs-reader-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
                      {selectedDoc.content || ''}
                    </ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <EmptyState
            title="Select a document"
            description="Choose a document from the list to read it, or create a new one"
          />
        )}
      </div>

      {/* ── Create modal ── */}
      <Modal open={creating} onClose={() => setCreating(false)} title="New Document" width="640px">
        <div className="docs-create-form">
          <input
            className="docs-editor-title"
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="Document title"
          />
          <div className="docs-editor-row">
            <select
              className="docs-editor-visibility"
              value={newVisibility}
              onChange={(e) => setNewVisibility(e.target.value)}
            >
              {VISIBILITY_OPTIONS.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <input
              className="docs-editor-tags"
              type="text"
              value={newTags}
              onChange={(e) => setNewTags(e.target.value)}
              placeholder="Tags (comma-separated)"
            />
          </div>
          <textarea
            className="docs-editor-body"
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="Write markdown content..."
            rows={14}
          />
          <div className="docs-editor-actions">
            <button
              className="docs-btn docs-btn--primary"
              onClick={handleCreate}
              disabled={saving || !newTitle.trim()}
              type="button"
            >
              {saving ? 'Creating...' : 'Create'}
            </button>
            <button
              className="docs-btn docs-btn--secondary"
              onClick={() => setCreating(false)}
              type="button"
            >
              Cancel
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
