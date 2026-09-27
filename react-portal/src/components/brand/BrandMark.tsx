import { BRAND_PREFIX, BRAND_SUFFIX, BRAND_NAME } from '../../utils/brand'
import './BrandMark.css'

/** The yourAICIV "y." glyph on an Ember tile. Same drawing as public/favicon.svg. */
export function BrandGlyph({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" className="brand-glyph">
      <defs>
        <linearGradient id="yaiciv-ember" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#F59E4B" />
          <stop offset="1" stopColor="#C2410C" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill="url(#yaiciv-ember)" />
      <path d="M19 18 L32 35 L45 18" fill="none" stroke="#fff" strokeWidth="7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M32 35 V47" fill="none" stroke="#fff" strokeWidth="7" strokeLinecap="round" />
      <circle cx="46" cy="46" r="4.5" fill="#fff" />
    </svg>
  )
}

/** Glyph + "yourAICIV" wordmark. */
export function BrandMark({ size = 28, showWordmark = true }: { size?: number; showWordmark?: boolean }) {
  return (
    <span className="brand-mark" aria-label={BRAND_NAME}>
      <BrandGlyph size={size} />
      {showWordmark && (
        <span className="brand-wordmark" aria-hidden="true">
          <span className="brand-wordmark-prefix">{BRAND_PREFIX}</span>
          <span className="brand-wordmark-suffix">{BRAND_SUFFIX}</span>
        </span>
      )}
    </span>
  )
}
