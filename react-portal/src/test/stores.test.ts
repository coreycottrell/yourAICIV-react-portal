import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useAuthStore } from '../stores/authStore'
import { useSettingsStore } from '../stores/settingsStore'
import { useCalendarStore } from '../stores/calendarStore'
import { useMailStore } from '../stores/mailStore'

// Mock fetch globally
const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => {
  mockFetch.mockReset()
  localStorage.clear()
})

describe('authStore', () => {
  it('starts unauthenticated with no token', () => {
    const state = useAuthStore.getState()
    expect(state.authenticated).toBe(false)
    expect(state.token).toBeNull()
  })

  it('login succeeds with valid token', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({ civ: 'nova', uptime: 100 }),
    })

    const result = await useAuthStore.getState().login('test-token')
    expect(result).toBe(true)
    expect(useAuthStore.getState().authenticated).toBe(true)
    expect(useAuthStore.getState().token).toBe('test-token')
    expect(localStorage.getItem('youraiciv-portal-token')).toBe('test-token')
  })

  it('login fails with invalid token', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      text: () => Promise.resolve('unauthorized'),
    })

    // Mock reload to prevent actual reload
    const reloadMock = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload: reloadMock },
      writable: true,
    })

    const result = await useAuthStore.getState().login('bad-token')
    expect(result).toBe(false)
    expect(useAuthStore.getState().authenticated).toBe(false)
    expect(useAuthStore.getState().error).toBe('Invalid token')
  })

  it('logout clears token', () => {
    localStorage.setItem('youraiciv-portal-token', 'token')
    useAuthStore.setState({ token: 'token', authenticated: true })

    useAuthStore.getState().logout()

    expect(useAuthStore.getState().authenticated).toBe(false)
    expect(useAuthStore.getState().token).toBeNull()
    expect(localStorage.getItem('youraiciv-portal-token')).toBeNull()
  })
})

describe('settingsStore', () => {
  it('defaults to dark theme', () => {
    const state = useSettingsStore.getState()
    expect(state.theme).toBe('dark')
  })

  it('persists theme to localStorage', () => {
    useSettingsStore.getState().setTheme('light')
    expect(localStorage.getItem('youraiciv-theme')).toBe('light')
    expect(useSettingsStore.getState().theme).toBe('light')
  })

  it('manages quickfire pills', () => {
    const pills = ['Hello', 'Status']
    useSettingsStore.getState().setQuickfirePills(pills)
    expect(useSettingsStore.getState().quickfirePills).toEqual(pills)
  })

  it('loads theme from storage', () => {
    localStorage.setItem('youraiciv-theme', 'light')
    useSettingsStore.getState().loadFromStorage()
    expect(useSettingsStore.getState().theme).toBe('light')
  })
})

describe('calendarStore', () => {
  it('starts with month view and empty tasks', () => {
    const state = useCalendarStore.getState()
    expect(state.viewMode).toBe('month')
    expect(state.tasks).toEqual([])
  })

  it('switches view modes', () => {
    useCalendarStore.getState().setViewMode('week')
    expect(useCalendarStore.getState().viewMode).toBe('week')

    useCalendarStore.getState().setViewMode('day')
    expect(useCalendarStore.getState().viewMode).toBe('day')
  })

  it('loads tasks from API', async () => {
    // The calendar is backed by AgentCal: /api/agentcal/events returns { events }
    const mockEvents = [
      { id: 'task-1', summary: 'Test task', start: '2026-03-20T10:00:00Z', end: '2026-03-20T10:05:00Z' },
    ]
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({ events: mockEvents }),
    })

    await useCalendarStore.getState().loadTasks()
    const tasks = useCalendarStore.getState().tasks
    expect(tasks).toHaveLength(1)
    expect(tasks[0].id).toBe('task-1')
    expect(tasks[0].message).toBe('Test task')
    expect(tasks[0].status).toBe('pending') // normalized default
    expect(useCalendarStore.getState().loading).toBe(false)
  })

  it('handles load failure gracefully', async () => {
    // Reset store state first (previous test loaded tasks)
    useCalendarStore.setState({ tasks: [] })
    mockFetch.mockRejectedValueOnce(new Error('network error'))
    await useCalendarStore.getState().loadTasks()
    expect(useCalendarStore.getState().loading).toBe(false)
    expect(useCalendarStore.getState().tasks).toEqual([])
  })
})

describe('mailStore', () => {
  it('starts with inbox folder', () => {
    const state = useMailStore.getState()
    expect(state.folder).toBe('inbox')
    expect(state.inbox).toEqual([])
    expect(state.unreadCount).toBe(0)
  })

  it('switches folders and clears selection', () => {
    useMailStore.setState({ selectedMessage: { id: 1 } as never })
    useMailStore.getState().setFolder('sent')
    expect(useMailStore.getState().folder).toBe('sent')
    expect(useMailStore.getState().selectedMessage).toBeNull()
  })

  it('loads inbox and counts unread', async () => {
    const mockMessages = [
      { id: 1, from_agent: 'A', to_agent: 'B', subject: 'Hi', body: 'Hello', timestamp: '2026-03-18T10:00:00Z', read: false, archived: false, thread_id: null },
      { id: 2, from_agent: 'C', to_agent: 'B', subject: 'Hey', body: 'World', timestamp: '2026-03-18T11:00:00Z', read: true, archived: false, thread_id: null },
    ]
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({ messages: mockMessages }),
    })

    await useMailStore.getState().loadInbox()
    expect(useMailStore.getState().inbox).toEqual(mockMessages)
    expect(useMailStore.getState().unreadCount).toBe(1)
  })

  it('marks message as read and decrements count', async () => {
    useMailStore.setState({
      inbox: [
        { id: 1, read: false } as never,
        { id: 2, read: true } as never,
      ],
      unreadCount: 1,
    })

    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => Promise.resolve({ ok: true }),
    })

    await useMailStore.getState().markRead(1)
    const msg = useMailStore.getState().inbox.find(m => m.id === 1)
    expect(msg?.read).toBe(true)
    expect(useMailStore.getState().unreadCount).toBe(0)
  })
})
