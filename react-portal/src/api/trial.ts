/**
 * yourAICIV trial contract, portal side.
 * GET /api/trial is unauthenticated on purpose so the payment screen can
 * render even without a stored token. Shape is fixed by the shared contract.
 */
export interface TrialStatus {
  trial: boolean
  day: number
  days_left: number
  duration_days?: number
  expires_at: string
  expired: boolean
  payment_url: string
  /** the server could not trust its trial record and failed closed */
  config_error?: boolean
}

export const NOT_A_TRIAL: TrialStatus = {
  trial: false,
  day: 0,
  days_left: 0,
  duration_days: 0,
  expires_at: '',
  expired: false,
  payment_url: '',
}

export async function fetchTrial(): Promise<TrialStatus> {
  const res = await fetch('/api/trial', { cache: 'no-store' })
  if (!res.ok) throw new Error(`trial status ${res.status}`)
  const data = (await res.json()) as Partial<TrialStatus>
  return { ...NOT_A_TRIAL, ...data }
}

/** Only ever send people to an https payment link. */
export function safePaymentUrl(url: string | undefined): string | null {
  if (!url) return null
  try {
    const u = new URL(url)
    return u.protocol === 'https:' ? u.toString() : null
  } catch {
    return null
  }
}
