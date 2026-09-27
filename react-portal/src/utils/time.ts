import { format, parseISO } from 'date-fns'

export function localTimeToUTC(localDate: Date): string {
  return localDate.toISOString()
}

export function utcTimeToLocal(utcString: string): Date {
  return parseISO(utcString)
}

export function formatTime(date: Date): string {
  return format(date, 'h:mm a')
}

export function formatDate(date: Date): string {
  return format(date, 'MMM d, yyyy')
}

export function formatDateTime(date: Date): string {
  return format(date, 'MMM d, yyyy h:mm a')
}

export function formatRelativeTime(timestamp: number): string {
  if (!timestamp || !isFinite(timestamp)) return ''
  const now = Date.now() / 1000
  const diff = now - timestamp
  if (diff < 0) return 'soon'
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return formatDate(new Date(timestamp * 1000))
}

/** Convert an ISO string timestamp to epoch seconds, safely */
export function isoToEpochSeconds(iso: string): number {
  const ms = new Date(iso).getTime()
  return isNaN(ms) ? 0 : ms / 1000
}

export function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
