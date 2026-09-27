import { BrandMark } from '../brand/BrandMark'
import { Icon } from '../common/Icon'
import { safePaymentUrl, type TrialStatus } from '../../api/trial'
import './TrialExpiredScreen.css'

/**
 * Full-screen block shown when the trial is over. Its only action is the
 * payment button. Nothing else in the portal is rendered behind it.
 */
export function TrialExpiredScreen({ status }: { status: TrialStatus }) {
  const payUrl = safePaymentUrl(status.payment_url)
  const days = status.duration_days || 7

  return (
    <div className="trial-expired" role="dialog" aria-modal="true" aria-labelledby="trial-expired-title">
      <div className="trial-expired-card">
        <BrandMark size={36} />
        <div className="trial-expired-icon" aria-hidden="true">
          <Icon name="lock" size={26} />
        </div>
        <h1 id="trial-expired-title" className="trial-expired-title">
          Your {days}-day trial has ended
        </h1>
        <p className="trial-expired-body">
          Everything your AI built for you is saved: the work, the files, and what it
          learned about your business. Subscribe and it all comes back right where you left off.
        </p>
        {payUrl ? (
          <a className="trial-expired-cta" href={payUrl} rel="noopener noreferrer">
            Subscribe and keep my AI
            <Icon name="arrow" size={18} />
          </a>
        ) : (
          <p className="trial-expired-body">
            Contact the team that set up your AI to subscribe.
          </p>
        )}
        {status.config_error && (
          <p className="trial-expired-fine">
            Already subscribed? Contact the team that set up your AI so they can switch your account over.
          </p>
        )}
        <p className="trial-expired-fine">
          After you subscribe, this page unlocks as soon as your account is switched over.
        </p>
      </div>
    </div>
  )
}
