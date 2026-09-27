import { useState, type FormEvent } from 'react'
import { useAuthStore } from '../../stores/authStore'
import { BrandMark } from '../brand/BrandMark'
import { BRAND_TAGLINE } from '../../utils/brand'
import './AuthModal.css'

export function AuthModal() {
  const [token, setToken] = useState('')
  const [loading, setLoading] = useState(false)
  const { login, error } = useAuthStore()

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!token.trim()) return
    setLoading(true)
    await login(token.trim())
    setLoading(false)
  }

  return (
    <div className="auth-overlay">
      <div className="auth-card">
        <div className="auth-header">
          <BrandMark size={40} />
          <h1 className="auth-title">Sign in to your AI</h1>
          <p className="auth-subtitle">{BRAND_TAGLINE}</p>
        </div>
        <form onSubmit={handleSubmit} className="auth-form">
          <label className="auth-label" htmlFor="auth-token">Access code</label>
          <input
            id="auth-token"
            type="password"
            className="auth-input"
            placeholder="Paste your access code"
            value={token}
            onChange={e => setToken(e.target.value)}
            autoFocus
            autoComplete="current-password"
            disabled={loading}
          />
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button
            type="submit"
            className="auth-submit"
            disabled={loading || !token.trim()}
          >
            {loading ? 'Signing in...' : 'Sign in'}
          </button>
          <p className="auth-hint">
            Tip: the sign-in link in your welcome message logs you in automatically.
          </p>
        </form>
      </div>
    </div>
  )
}
