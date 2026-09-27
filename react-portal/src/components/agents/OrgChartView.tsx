import React, { useEffect, useMemo, useCallback, useState } from 'react'
import { useAgentsStore } from '../../stores/agentsStore'
import { useChatStore } from '../../stores/chatStore'
import { apiPost } from '../../api/client'
import { Modal } from '../common/Modal'
import { useNavigate } from 'react-router-dom'
import type { Agent, Department } from '../../types/agents'
import './OrgChartView.css'

/* ── Constants ──────────────────────────────────────────── */

const DEPARTMENT_COLORS: Record<string, string> = {
  'Leadership': '#f1420b',
  'Systems & Technology': '#2a93c1',
  'Product Development': '#8b5cf6',
  'Sales & Distribution': '#f59e0b',
  'Marketing & Advertising': '#f1420b',
  'Brand & Content': '#e85d04',
  'Commercial & Business Development': '#06b6d4',
  'Operations & Planning': '#22c55e',
  'Human Resources': '#ec4899',
  'Accounting & Finance': '#84cc16',
  'Capital & Investments': '#fbbf24',
  'Research': '#a78bfa',
  'Legal & Compliance': '#6366f1',
  'Infrastructure': '#f97316',
  'Digital Assets': '#60a5fa',
  'Community & Care': '#f472b6',
  'Board of Advisors': '#c084fc',
  'Karma': '#34d399',
  'Meta & Governance': '#818cf8',
  'AI Strategy': '#06b6d4',
  'IT Support': '#94a3b8',
  'Customer Support': '#fb923c',
  'Other': '#9ca3af',
}

const STATUS_COLORS: Record<string, string> = {
  active: '#22c55e',
  working: '#3b82f6',
  idle: '#f59e0b',
  offline: '#6b7280',
}

const STATUS_LABELS: Record<string, string> = {
  all: 'All',
  active: 'Active',
  working: 'Working',
  idle: 'Idle',
  offline: 'Offline',
}

const TECH_DEPTS = ['Systems & Technology', 'Product Development', 'Infrastructure']
const MARKETING_DEPTS = ['Marketing & Advertising', 'Brand & Content']
const PRIMARY_DEPT = 'Leadership'

const TECH_TEAMS: Record<string, { label: string; depts: string[] }> = {
  core: { label: 'Core Tech Team', depts: ['Systems & Technology'] },
  product: { label: 'Product Team', depts: ['Product Development'] },
  infra: { label: 'Infrastructure Team', depts: ['Infrastructure'] },
}

function getDeptColor(name: string): string {
  return DEPARTMENT_COLORS[name] || '#9ca3af'
}

function getInitials(name: string): string {
  return name
    .split(/[\s-]+/)
    .map((w) => w[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase()
}

/* ── OrgNode component (the makeNode equivalent) ────────── */

type NodeTier = 'primary' | 'l2' | 'dept' | 'agent'

interface OrgNodeProps {
  agent: Agent | null
  tier: NodeTier
  badge?: string
  badgeColor?: string
  deptColor?: string
  onClick?: () => void
  expandHint?: 'down' | 'up' | null
  agentCount?: number
}

const OrgNode = React.memo(function OrgNode({
  agent,
  tier,
  badge,
  badgeColor,
  deptColor = '#9ca3af',
  onClick,
  expandHint,
  agentCount,
}: OrgNodeProps) {
  if (!agent) return null

  const statusColor = STATUS_COLORS[agent.status] || STATUS_COLORS.offline
  const isPrimary = tier === 'primary'

  return (
    <div
      className={`oct-node oct-node--${tier}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}
    >
      <div
        className={`oct-avatar oct-avatar--${tier}`}
        style={
          isPrimary
            ? { background: 'linear-gradient(135deg, #3b82f6, #f97316)' }
            : { background: `${deptColor}22`, borderColor: `${deptColor}44` }
        }
      >
        <span className="oct-avatar-initials" style={isPrimary ? { color: '#fff' } : { color: deptColor }}>
          {getInitials(agent.name)}
        </span>
        <span
          className="oct-status-dot"
          style={{ backgroundColor: statusColor, boxShadow: `0 0 6px ${statusColor}88` }}
          title={agent.status}
        />
      </div>
      <div className="oct-node-name">{agent.name}</div>
      {badge && (
        <span className="oct-node-badge" style={{ backgroundColor: badgeColor || deptColor }}>
          {badge}
        </span>
      )}
      {agent.current_task && (
        <div className="oct-node-task" title={agent.current_task}>
          {agent.current_task}
        </div>
      )}
      {!agent.current_task && agent.description && (
        <div className="oct-node-desc" title={agent.description}>
          {agent.description}
        </div>
      )}
      {expandHint && (
        <div className="oct-expand-hint">
          {expandHint === 'down' ? '\u25BC' : '\u25B2'} {agentCount != null ? `${agentCount} agents` : ''}
        </div>
      )}
    </div>
  )
})

/* ── Agent list (expanded under a dept node) ─────────────── */

const AgentList = React.memo(function AgentList({
  members,
  deptColor,
}: {
  members: Agent[]
  deptColor: string
}) {
  return (
    <div className="oct-dept-agents">
      {members.map((m) => (
        <OrgNode key={m.id} agent={m} tier="agent" deptColor={deptColor} />
      ))}
    </div>
  )
})

/* ── Department node with expand/collapse ────────────────── */

const DeptNode = React.memo(function DeptNode({
  dept,
  expanded,
  onToggle,
}: {
  dept: Department
  expanded: boolean
  onToggle: () => void
}) {
  const color = getDeptColor(dept.name)
  const lead = dept.lead
  const hasMembers = dept.members.length > 0

  return (
    <div className="oct-dept-col">
      <OrgNode
        agent={lead || { id: dept.name, name: dept.name, description: '', status: 'offline', department: dept.name, is_lead: 0 } as Agent}
        tier="dept"
        deptColor={color}
        onClick={hasMembers ? onToggle : undefined}
        expandHint={hasMembers ? (expanded ? 'up' : 'down') : null}
        agentCount={dept.members.length}
      />
      {expanded && hasMembers && <AgentList members={dept.members} deptColor={color} />}
    </div>
  )
})

/* ── Main OrgChartView ───────────────────────────────────── */

/* ── Hire Modal ──────────────────────────────────────────── */

function HireModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [department, setDepartment] = useState('Development')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    setSaving(true)
    setResult(null)
    const agentId = name.trim().toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '')
    try {
      await apiPost('/api/agents/create', {
        id: agentId,
        name: name.trim(),
        description: description.trim(),
        department,
        model: 'opus',
        tools: 'Read, Write, Edit, Bash, Grep, Glob',
        prompt: `You are ${name.trim()}, a specialist in the ${department} department.\n\n${description.trim()}`,
        is_lead: false,
        capabilities: ['General'],
      })
      setResult(`${name.trim()} added to ${department}`)
      setName('')
      setDescription('')
      onCreated()
      setTimeout(() => { setResult(null); onClose() }, 1500)
    } catch {
      setResult('Failed to create agent')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Hire New Agent" width="440px">
      <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Data Pipeline Lead"
            autoFocus
            style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px' }}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>What should they do?</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Describe their role in one or two sentences..."
            rows={3}
            style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px', resize: 'vertical' }}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>Department</label>
          <select
            value={department}
            onChange={(e) => setDepartment(e.target.value)}
            style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px' }}
          >
            {Object.keys(DEPARTMENT_COLORS).map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </div>
        {result && (
          <div style={{ padding: '8px 12px', borderRadius: 'var(--radius-md)', background: result.includes('Failed') ? 'var(--status-error)22' : 'var(--status-success)22', color: result.includes('Failed') ? 'var(--status-error)' : 'var(--status-success)', fontSize: '13px', fontWeight: 500 }}>
            {result}
          </div>
        )}
        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose} style={{ padding: '8px 16px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-subtle)', background: 'transparent', color: 'var(--text-secondary)', fontSize: '13px', cursor: 'pointer' }}>
            Cancel
          </button>
          <button type="submit" disabled={saving || !name.trim()} style={{ padding: '8px 16px', borderRadius: 'var(--radius-md)', border: 'none', background: 'var(--accent-primary)', color: '#fff', fontSize: '13px', fontWeight: 600, cursor: 'pointer', opacity: saving || !name.trim() ? 0.5 : 1 }}>
            {saving ? 'Creating...' : 'Create Agent'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

export default function OrgChartView() {
  const loading = useAgentsStore((s) => s.loading)
  const stats = useAgentsStore((s) => s.stats)
  const searchQuery = useAgentsStore((s) => s.searchQuery)
  const statusFilter = useAgentsStore((s) => s.statusFilter)
  const expandedDepts = useAgentsStore((s) => s.expandedDepts)
  const loadOrgChart = useAgentsStore((s) => s.loadOrgChart)
  const loadStats = useAgentsStore((s) => s.loadStats)
  const setSearch = useAgentsStore((s) => s.setSearch)
  const setStatusFilter = useAgentsStore((s) => s.setStatusFilter)
  const toggleDept = useAgentsStore((s) => s.toggleDept)
  const filteredDepartments = useAgentsStore((s) => s.filteredDepartments)

  const [hireOpen, setHireOpen] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    loadOrgChart()
    loadStats()
  }, [loadOrgChart, loadStats])

  // searchQuery/statusFilter are read inside the store selector; listing them recomputes on filter change
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const departments = useMemo(() => filteredDepartments(), [filteredDepartments, searchQuery, statusFilter])

  const handleSearch = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => setSearch(e.target.value),
    [setSearch]
  )

  // Partition departments into hierarchy buckets
  const deptMap = useMemo(() => {
    const map = new Map<string, Department>()
    departments.forEach((d) => map.set(d.name, d))
    return map
  }, [departments])

  const primaryDept = deptMap.get(PRIMARY_DEPT) || null

  // Primary agent = lead of the Leadership department
  const primaryAgent = primaryDept?.lead || null

  const techDepts = useMemo(
    () => TECH_DEPTS.map((name) => deptMap.get(name)).filter(Boolean) as Department[],
    [deptMap]
  )

  const marketingDepts = useMemo(
    () => MARKETING_DEPTS.map((name) => deptMap.get(name)).filter(Boolean) as Department[],
    [deptMap]
  )

  const otherDepts = useMemo(() => {
    const reserved = new Set([PRIMARY_DEPT, ...TECH_DEPTS, ...MARKETING_DEPTS])
    return departments.filter((d) => !reserved.has(d.name))
  }, [departments])

  // Build a synthetic CTO and CMO agent from leads (if present)
  const ctoAgent: Agent | null = useMemo(() => {
    // Find the first tech dept lead as CTO representative
    for (const d of techDepts) {
      if (d.lead) return d.lead
    }
    return { id: 'cto', name: 'CTO', description: 'Chief Technology Officer', status: 'active' as const, department: 'Systems & Technology', is_lead: 1 }
  }, [techDepts])

  const cmoAgent: Agent | null = useMemo(() => {
    for (const d of marketingDepts) {
      if (d.lead) return d.lead
    }
    return { id: 'cmo', name: 'CMO', description: 'Chief Marketing Officer', status: 'active' as const, department: 'Marketing & Advertising', is_lead: 1 }
  }, [marketingDepts])

  const filters: Array<'all' | 'active' | 'working' | 'idle' | 'offline'> = [
    'all', 'active', 'working', 'idle', 'offline',
  ]

  const hasHierarchy = primaryDept || techDepts.length > 0 || marketingDepts.length > 0

  return (
    <div className="oct-container">
      {/* ── Header ─────────────────────────────────────── */}
      <div className="oct-header">
        <div className="oct-header-top">
          <h1 className="oct-title">Organization</h1>
          {stats && (
            <div className="oct-stats-row">
              <span className="oct-stat">
                <span className="oct-stat-value">{stats.total}</span>
                <span className="oct-stat-label">Agents</span>
              </span>
              <span className="oct-stat-divider" />
              <span className="oct-stat">
                <span className="oct-stat-value">{stats.departments}</span>
                <span className="oct-stat-label">Depts</span>
              </span>
              <span className="oct-stat-divider" />
              <span className="oct-stat">
                <span className="oct-stat-dot" style={{ backgroundColor: STATUS_COLORS.active }} />
                <span className="oct-stat-value">{stats.active}</span>
              </span>
              <span className="oct-stat">
                <span className="oct-stat-dot" style={{ backgroundColor: STATUS_COLORS.working }} />
                <span className="oct-stat-value">{stats.working}</span>
              </span>
              <span className="oct-stat">
                <span className="oct-stat-dot" style={{ backgroundColor: STATUS_COLORS.idle }} />
                <span className="oct-stat-value">{stats.idle}</span>
              </span>
              <span className="oct-stat">
                <span className="oct-stat-dot" style={{ backgroundColor: STATUS_COLORS.offline }} />
                <span className="oct-stat-value">{stats.offline}</span>
              </span>
            </div>
          )}
        </div>

        {/* Legend */}
        <div className="oct-legend">
          {Object.entries(STATUS_COLORS).map(([key, color]) => (
            <span key={key} className="oct-legend-item">
              <span className="oct-legend-dot" style={{ backgroundColor: color }} />
              <span className="oct-legend-label">{STATUS_LABELS[key]}</span>
            </span>
          ))}
        </div>

        {/* Search + Filters */}
        <div className="oct-controls">
          <div className="oct-search-wrap">
            <svg className="oct-search-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.5" />
              <path d="M11 11L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            <input
              className="oct-search"
              type="text"
              placeholder="Search agents, departments, tasks..."
              value={searchQuery}
              onChange={handleSearch}
            />
            {searchQuery && (
              <button className="oct-search-clear" onClick={() => setSearch('')} type="button">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M3 3L11 11M11 3L3 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
          <div className="oct-filters">
            {filters.map((f) => (
              <button
                key={f}
                className={`oct-filter-chip${statusFilter === f ? ' oct-filter-chip--active' : ''}`}
                onClick={() => setStatusFilter(f)}
                type="button"
                style={
                  statusFilter === f && f !== 'all'
                    ? { borderColor: STATUS_COLORS[f], color: STATUS_COLORS[f], backgroundColor: `${STATUS_COLORS[f]}18` }
                    : undefined
                }
              >
                {f !== 'all' && (
                  <span className="oct-filter-dot" style={{ backgroundColor: STATUS_COLORS[f] }} />
                )}
                {STATUS_LABELS[f]}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ── Quickfire Skills ──────────────────────────── */}
      <div className="oct-quickfire">
        <button
          className="oct-quickfire-btn"
          onClick={() => { useChatStore.getState().send('/org-health'); navigate('/') }}
          title="Check all team readiness"
          type="button"
        >
          <span className="oct-quickfire-icon">{'\u2764\uFE0F'}</span>
          <span className="oct-quickfire-label">Health Check</span>
        </button>
        <button
          className="oct-quickfire-btn"
          onClick={() => { useChatStore.getState().send('/org-restructure'); navigate('/') }}
          title="Analyze and optimize teams"
          type="button"
        >
          <span className="oct-quickfire-icon">{'\uD83D\uDD04'}</span>
          <span className="oct-quickfire-label">Restructure</span>
        </button>
        <button
          className="oct-quickfire-btn"
          onClick={() => setHireOpen(true)}
          title="Create a new team member"
          type="button"
        >
          <span className="oct-quickfire-icon">{'\u2795'}</span>
          <span className="oct-quickfire-label">Hire Agent</span>
        </button>
      </div>

      {/* Hire Modal */}
      <HireModal open={hireOpen} onClose={() => setHireOpen(false)} onCreated={() => { loadOrgChart(); loadStats() }} />

      {/* ── Content ───────────────────────────────────── */}
      {loading ? (
        <div className="oct-loading">
          <div className="oct-spinner" />
          <span>Loading organization...</span>
        </div>
      ) : departments.length === 0 ? (
        <div className="oct-empty">
          <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
            <circle cx="24" cy="24" r="20" stroke="var(--text-tertiary)" strokeWidth="1.5" />
            <path d="M17 20C17 20 20 17 24 17C28 17 31 20 31 20" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" />
            <circle cx="18" cy="26" r="1.5" fill="var(--text-tertiary)" />
            <circle cx="30" cy="26" r="1.5" fill="var(--text-tertiary)" />
            <path d="M19 33C19 33 21 31 24 31C27 31 29 33 29 33" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <span>No agents match your filters</span>
        </div>
      ) : hasHierarchy ? (
        <div className="oct-tree">

          {/* ── Level 1: Primary ────────────────────────── */}
          {primaryAgent && (
            <div className="oct-row oct-row--center">
              <OrgNode
                agent={primaryAgent}
                tier="primary"
                badge="Primary"
                badgeColor="#f1420b"
                deptColor="#f1420b"
                onClick={primaryDept && primaryDept.members.length > 0 ? () => toggleDept(PRIMARY_DEPT) : undefined}
                expandHint={primaryDept && primaryDept.members.length > 0 ? (expandedDepts.has(PRIMARY_DEPT) ? 'up' : 'down') : null}
                agentCount={primaryDept?.members.length}
              />
              {expandedDepts.has(PRIMARY_DEPT) && primaryDept && primaryDept.members.length > 0 && (
                <AgentList members={primaryDept.members} deptColor="#f1420b" />
              )}
            </div>
          )}

          {/* ── Stem down from Primary to L2 ────────────── */}
          {primaryAgent && (techDepts.length > 0 || marketingDepts.length > 0) && (
            <div className="oct-stem-down" />
          )}

          {/* ── Level 2: CTO + CMO ──────────────────────── */}
          {(techDepts.length > 0 || marketingDepts.length > 0) && (
            <div className="oct-row oct-row--l2">
              {techDepts.length > 0 && (
                <div className="oct-col oct-col--center">
                  <OrgNode
                    agent={ctoAgent}
                    tier="l2"
                    badge="CTO"
                    badgeColor="#2a93c1"
                    deptColor="#2a93c1"
                  />
                </div>
              )}
              <div className="oct-hbar" />
              {marketingDepts.length > 0 && (
                <div className="oct-col oct-col--center">
                  <OrgNode
                    agent={cmoAgent}
                    tier="l2"
                    badge="CMO"
                    badgeColor="#e85d04"
                    deptColor="#e85d04"
                  />
                </div>
              )}
            </div>
          )}

          {/* ── Level 3: CTO subtree + CMO subtree side by side ── */}
          <div className="oct-row oct-row--l3">
            {/* CTO subtree */}
            {techDepts.length > 0 && (
              <div className="oct-subtree">
                <div className="oct-stem-down oct-stem-down--short" />
                <div className="oct-tech-groups">
                  {Object.entries(TECH_TEAMS).map(([key, team]) => {
                    const teamDepts = team.depts
                      .map((name) => deptMap.get(name))
                      .filter(Boolean) as Department[]
                    if (teamDepts.length === 0) return null
                    return (
                      <div key={key} className="oct-tech-team-group">
                        <div className="oct-tech-team-label">{team.label}</div>
                        {teamDepts.map((dept) => (
                          <DeptNode
                            key={dept.name}
                            dept={dept}
                            expanded={expandedDepts.has(dept.name)}
                            onToggle={() => toggleDept(dept.name)}
                          />
                        ))}
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* CMO subtree */}
            {marketingDepts.length > 0 && (
              <div className="oct-subtree">
                <div className="oct-stem-down oct-stem-down--short" />
                <div className="oct-marketing-groups">
                  {marketingDepts.map((dept) => (
                    <DeptNode
                      key={dept.name}
                      dept={dept}
                      expanded={expandedDepts.has(dept.name)}
                      onToggle={() => toggleDept(dept.name)}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* ── Level 4: All other departments ──────────── */}
          {otherDepts.length > 0 && (
            <>
              <div className="oct-stem-down" />
              <div className="oct-l4-label">Other Departments</div>
              <div className="oct-row oct-row--l4">
                {otherDepts.map((dept) => (
                  <DeptNode
                    key={dept.name}
                    dept={dept}
                    expanded={expandedDepts.has(dept.name)}
                    onToggle={() => toggleDept(dept.name)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      ) : (
        /* Fallback: flat grid when no hierarchy departments found */
        <div className="oct-row oct-row--l4">
          {departments.map((dept) => (
            <DeptNode
              key={dept.name}
              dept={dept}
              expanded={expandedDepts.has(dept.name)}
              onToggle={() => toggleDept(dept.name)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
