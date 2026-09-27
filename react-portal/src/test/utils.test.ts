import { describe, it, expect } from 'vitest'
import { cn } from '../utils/cn'
import { formatRelativeTime, formatUptime, isoToEpochSeconds, localTimeToUTC } from '../utils/time'

describe('cn()', () => {
  it('joins truthy class names', () => {
    expect(cn('a', 'b', 'c')).toBe('a b c')
  })

  it('filters falsy values', () => {
    expect(cn('a', false, null, undefined, 'b')).toBe('a b')
  })

  it('returns empty string for no classes', () => {
    expect(cn(false, null, undefined)).toBe('')
  })
})

describe('formatRelativeTime()', () => {
  it('returns "just now" for recent timestamps', () => {
    const now = Date.now() / 1000
    expect(formatRelativeTime(now)).toBe('just now')
    expect(formatRelativeTime(now - 30)).toBe('just now')
  })

  it('returns minutes ago', () => {
    const fiveMinAgo = Date.now() / 1000 - 300
    expect(formatRelativeTime(fiveMinAgo)).toBe('5m ago')
  })

  it('returns hours ago', () => {
    const twoHoursAgo = Date.now() / 1000 - 7200
    expect(formatRelativeTime(twoHoursAgo)).toBe('2h ago')
  })

  it('returns days ago', () => {
    const threeDaysAgo = Date.now() / 1000 - 259200
    expect(formatRelativeTime(threeDaysAgo)).toBe('3d ago')
  })

  it('handles future timestamps', () => {
    const future = Date.now() / 1000 + 60
    expect(formatRelativeTime(future)).toBe('soon')
  })

  it('handles 0 timestamp', () => {
    expect(formatRelativeTime(0)).toBe('')
  })

  it('handles NaN', () => {
    expect(formatRelativeTime(NaN)).toBe('')
  })
})

describe('formatUptime()', () => {
  it('formats minutes', () => {
    expect(formatUptime(300)).toBe('5m')
  })

  it('formats hours and minutes', () => {
    expect(formatUptime(3660)).toBe('1h 1m')
  })

  it('formats days and hours', () => {
    expect(formatUptime(90000)).toBe('1d 1h')
  })
})

describe('isoToEpochSeconds()', () => {
  it('converts valid ISO string', () => {
    const result = isoToEpochSeconds('2026-03-18T12:00:00Z')
    expect(result).toBeGreaterThan(0)
    // Verify round-trip: epoch seconds back to Date matches original
    const roundTrip = new Date(result * 1000).toISOString()
    expect(roundTrip).toBe('2026-03-18T12:00:00.000Z')
  })

  it('returns 0 for invalid string', () => {
    expect(isoToEpochSeconds('not-a-date')).toBe(0)
  })

  it('returns 0 for empty string', () => {
    expect(isoToEpochSeconds('')).toBe(0)
  })
})

describe('localTimeToUTC()', () => {
  it('converts a Date to ISO string', () => {
    const d = new Date('2026-03-18T12:00:00Z')
    const result = localTimeToUTC(d)
    expect(result).toMatch(/2026-03-18T12:00:00/)
    expect(result).toContain('Z')
  })
})
