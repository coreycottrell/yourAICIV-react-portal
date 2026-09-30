import { Link } from 'react-router-dom'
import { useSettingsStore } from '../../stores/settingsStore'
import { useAuthStore } from '../../stores/authStore'
import { useIdentityStore } from '../../stores/identityStore'
import { toggleBoop } from '../../api/settings'
import { cn } from '../../utils/cn'
import { SUPPORT_URL, SUPPORT_LABEL, BRAND_NAME } from '../../utils/brand'
import { OPERATOR_TOOLS } from '../layout/nav'
import { useTrialStore } from '../../stores/trialStore'
import type { Theme } from '../../types/settings'
import { ReconnectClaudeButton } from '../auth/ReconnectClaudeButton'
import './SettingsView.css'

export function SettingsView() {
  const { theme, setTheme, quickfirePills, setQuickfirePills, boopEnabled, setBoopEnabled } = useSettingsStore()
  const { logout } = useAuthStore()
  const inTrial = useTrialStore(s => s.status.trial)
  const operatorTools = OPERATOR_TOOLS.filter(t => !(inTrial && t.shell))
  const { civName, humanName, status } = useIdentityStore()

  const handleBoopToggle = async () => {
    const next = !boopEnabled
    try {
      await toggleBoop(next)
      setBoopEnabled(next)
    } catch {
      // leave the switch where it was
    }
  }

  const handleRemovePill = (pill: string) => {
    setQuickfirePills(quickfirePills.filter(p => p !== pill))
  }

  const handleAddPill = () => {
    const val = prompt('New quick prompt:')
    if (val?.trim() && !quickfirePills.includes(val.trim())) {
      setQuickfirePills([...quickfirePills, val.trim()])
    }
  }

  return (
    <div className="settings-view">
      <h2 className="settings-title">Settings</h2>

      <section className="settings-section">
        <h3>Account</h3>
        <div className="settings-info">
          <div className="settings-row">
            <span className="settings-label">Your AI</span>
            <span className="settings-value">{civName || '—'}</span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Your name</span>
            <span className="settings-value">{humanName || '—'}</span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Claude sign-in</span>
            <span className="settings-value"><ReconnectClaudeButton variant="settings" /></span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Version</span>
            <span className="settings-value">{BRAND_NAME} {status?.version || ''}</span>
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h3>Appearance</h3>
        <div className="theme-toggle" role="group" aria-label="Theme">
          {(['light', 'dark'] as Theme[]).map(t => (
            <button
              key={t}
              className={cn('theme-btn', theme === t && 'theme-btn-active')}
              onClick={() => setTheme(t)}
              aria-pressed={theme === t}
            >
              {t === 'dark' ? 'Dark' : 'Light'}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-section">
        <h3>Scheduled check-ins</h3>
        <div className="settings-row">
          <span className="settings-label">
            Let your AI check in on its own schedule to keep work moving while you&rsquo;re away
          </span>
          <button
            className={cn('boop-toggle', boopEnabled && 'boop-toggle-on')}
            onClick={handleBoopToggle}
            role="switch"
            aria-checked={boopEnabled}
            aria-label="Scheduled check-ins"
          >
            <span className="boop-toggle-thumb" />
          </button>
        </div>
      </section>

      <section className="settings-section">
        <h3>Quick prompts</h3>
        <p className="settings-help">Shortcuts shown under the chat box.</p>
        <div className="pill-list">
          {quickfirePills.map((pill, i) => (
            <span key={`${i}-${pill}`} className="pill-item">
              {pill}
              <button className="pill-remove" onClick={() => handleRemovePill(pill)} aria-label={`Remove ${pill}`}>&times;</button>
            </span>
          ))}
          <button className="pill-add" onClick={handleAddPill}>+ Add</button>
        </div>
      </section>

      {SUPPORT_URL && (
        <section className="settings-section">
          <h3>Help</h3>
          <div className="settings-links">
            <a href={SUPPORT_URL} target="_blank" rel="noopener noreferrer" className="settings-link">
              {SUPPORT_LABEL}
            </a>
          </div>
        </section>
      )}

      <section className="settings-section">
        <details className="settings-operator">
          <summary>Operator tools</summary>
          <p className="settings-help">
            Technical views for the team that runs your AI. You don&rsquo;t need these day to day.
            {inTrial && ' Terminal, Sessions and Browser are turned on when you subscribe.'}
          </p>
          <ul className="settings-operator-list">
            {operatorTools.map(t => (
              <li key={t.to}>
                <Link to={t.to} className="settings-link">{t.label}</Link>
                <span className="settings-help">{t.desc}</span>
              </li>
            ))}
          </ul>
        </details>
      </section>

      <section className="settings-section">
        <button className="settings-logout" onClick={logout}>
          Sign out
        </button>
      </section>
    </div>
  )
}
