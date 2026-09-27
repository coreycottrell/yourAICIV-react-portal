/**
 * yourAICIV brand constants.
 *
 * Optional build-time seams (set in react-portal/.env.local or the build env):
 *   VITE_SUPPORT_URL   where the "Get help" link points (hidden when unset)
 *   VITE_SUPPORT_LABEL text for that link (default "Get help")
 */
export const BRAND_NAME = 'yourAICIV'
export const BRAND_PREFIX = 'your'
export const BRAND_SUFFIX = 'AICIV'
export const BRAND_TAGLINE = 'Your own AI, working for your business.'

export const SUPPORT_URL: string = (import.meta.env.VITE_SUPPORT_URL as string | undefined) ?? ''
export const SUPPORT_LABEL: string = (import.meta.env.VITE_SUPPORT_LABEL as string | undefined) || 'Get help'
