import { useTrialStore } from '../../stores/trialStore'
import { safePaymentUrl } from '../../api/trial'
import './TrialBadge.css'

/** "Day N of 7" countdown shown in the header while a trial is active. */
export function TrialBadge() {
  const status = useTrialStore(s => s.status)
  if (!status.trial || status.expired) return null

  const total = status.duration_days || 7
  const payUrl = safePaymentUrl(status.payment_url)
  const left = status.days_left
  const hint = `${left} ${left === 1 ? 'day' : 'days'} left in your free trial`
  const lastDays = left <= 2

  const body = (
    <>
      <span className="trial-badge-dot" aria-hidden="true" />
      <span className="trial-badge-text">
        Day {status.day} of {total}
      </span>
      {payUrl && <span className="trial-badge-cta">Subscribe</span>}
    </>
  )

  return payUrl ? (
    <a
      className={`trial-badge${lastDays ? ' trial-badge-final' : ''}`}
      href={payUrl}
      target="_blank"
      rel="noopener noreferrer"
      title={`${hint}. Subscribe any time to keep everything.`}
      aria-label={`Trial: day ${status.day} of ${total}. ${hint}. Subscribe.`}
    >
      {body}
    </a>
  ) : (
    <span className={`trial-badge${lastDays ? ' trial-badge-final' : ''}`} title={hint}>
      {body}
    </span>
  )
}
