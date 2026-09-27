import type { IconName } from '../common/Icon'

export interface NavItem {
  to: string
  icon: IconName
  label: string
  /** shorter label for the phone tab bar */
  short?: string
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

/**
 * Client-facing navigation. Operator-only views (terminal, sessions, browser,
 * context, feedback) are routed but not listed here; they are linked from
 * Settings > Operator tools.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Work',
    items: [
      { to: '/', icon: 'chat', label: 'Chat' },
      { to: '/calendar', icon: 'calendar', label: 'Calendar' },
      { to: '/mail', icon: 'mail', label: 'Email' },
      { to: '/docs', icon: 'doc', label: 'Docs' },
      { to: '/sheets', icon: 'sheet', label: 'Sheets' },
    ],
  },
  {
    label: 'Your AI',
    items: [
      { to: '/orgchart', icon: 'team', label: 'AI Team' },
      { to: '/bookmarks', icon: 'bookmark', label: 'Saved' },
      { to: '/status', icon: 'pulse', label: 'Status' },
    ],
  },
]

export const SETTINGS_ITEM: NavItem = { to: '/settings', icon: 'settings', label: 'Settings' }

/** Phone tab bar: four tabs + More. */
export const MOBILE_PRIMARY: NavItem[] = [
  { to: '/', icon: 'chat', label: 'Chat' },
  { to: '/calendar', icon: 'calendar', label: 'Calendar' },
  { to: '/mail', icon: 'mail', label: 'Email' },
  { to: '/docs', icon: 'doc', label: 'Docs' },
]

export const MOBILE_MORE: NavItem[] = [
  { to: '/orgchart', icon: 'team', label: 'AI Team' },
  { to: '/sheets', icon: 'sheet', label: 'Sheets' },
  { to: '/bookmarks', icon: 'bookmark', label: 'Saved' },
  { to: '/status', icon: 'pulse', label: 'Status' },
  SETTINGS_ITEM,
]

/** `shell: true` = reaches the AI's shell or sessions; off (server-side) during a trial. */
export const OPERATOR_TOOLS: { to: string; label: string; desc: string; shell?: boolean }[] = [
  { to: '/terminal', label: 'Terminal', desc: 'Live view of the AI session (tmux)', shell: true },
  { to: '/teams', label: 'Sessions', desc: 'Panes the AI is running', shell: true },
  { to: '/context', label: 'Context window', desc: 'How full the AI’s working memory is' },
  { to: '/browser', label: 'Browser', desc: 'Watch the AI’s browser (needs the browser service)', shell: true },
  { to: '/points', label: 'Feedback', desc: 'Reactions you gave to messages' },
]
