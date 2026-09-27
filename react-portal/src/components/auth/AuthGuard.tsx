import { useEffect } from 'react'
import { useAuthStore } from '../../stores/authStore'
import { AuthModal } from './AuthModal'
import { FullPageSpinner } from '../common/LoadingSpinner'

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { authenticated, loading, checkAuth } = useAuthStore()

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  if (loading) return <FullPageSpinner />
  if (!authenticated) return <AuthModal />

  return <>{children}</>
}
