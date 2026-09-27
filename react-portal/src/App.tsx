import { useEffect } from 'react'
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthGuard } from './components/auth/AuthGuard'
import { ClaudeAuthFlow } from './components/auth/ClaudeAuthFlow'
import { TrialGate } from './components/trial/TrialGate'
import { OperatorOnly } from './components/trial/OperatorOnly'
import { AppShell } from './components/layout/AppShell'
import { ChatView } from './components/chat/ChatView'
import { CalendarView } from './components/calendar/CalendarView'
import { MailView } from './components/agentmail/MailView'
import { SettingsView } from './components/settings/SettingsView'
import { TerminalView } from './components/terminal/TerminalView'
import { TeamsView } from './components/teams/TeamsView'
import { BookmarksView } from './components/bookmarks/BookmarksView'
import { StatusView } from './components/status/StatusView'
import { ContextView } from './components/context/ContextView'
import OrgChartView from './components/agents/OrgChartView'
import { DocsView } from './components/docs/DocsView'
import { SheetsView } from './components/sheets/SheetsView'
import { PointsView } from './components/points/PointsView'
import { BrowserView } from './components/browser/BrowserView'
import { useIdentityStore } from './stores/identityStore'
import { useSettingsStore } from './stores/settingsStore'

/** Runs identity + status fetches only after auth succeeds */
function AuthenticatedApp() {
  const fetchIdentity = useIdentityStore(s => s.fetchIdentity)
  const fetchStatusInfo = useIdentityStore(s => s.fetchStatusInfo)

  useEffect(() => {
    fetchIdentity()
    fetchStatusInfo()
    const interval = setInterval(fetchStatusInfo, 30_000)
    return () => clearInterval(interval)
  }, [fetchIdentity, fetchStatusInfo])

  return (
    <>
      <ClaudeAuthFlow />
      <Routes>
        <Route element={<AppShell />}>
          {/* Client-facing */}
          <Route path="/" element={<ChatView />} />
          <Route path="/calendar" element={<CalendarView />} />
          <Route path="/mail" element={<MailView />} />
          <Route path="/docs" element={<DocsView />} />
          <Route path="/sheets" element={<SheetsView />} />
          <Route path="/orgchart" element={<OrgChartView />} />
          <Route path="/bookmarks" element={<BookmarksView />} />
          <Route path="/status" element={<StatusView />} />
          <Route path="/settings" element={<SettingsView />} />
          {/* Operator tools (linked from Settings, not in the main nav) */}
          <Route path="/terminal" element={<OperatorOnly><TerminalView /></OperatorOnly>} />
          <Route path="/teams" element={<OperatorOnly><TeamsView /></OperatorOnly>} />
          <Route path="/context" element={<ContextView />} />
          <Route path="/browser" element={<OperatorOnly><BrowserView /></OperatorOnly>} />
          <Route path="/points" element={<PointsView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </>
  )
}

export default function App() {
  const loadFromStorage = useSettingsStore(s => s.loadFromStorage)

  useEffect(() => {
    loadFromStorage()
  }, [loadFromStorage])

  return (
    <HashRouter>
      <TrialGate>
        <AuthGuard>
          <AuthenticatedApp />
        </AuthGuard>
      </TrialGate>
    </HashRouter>
  )
}
