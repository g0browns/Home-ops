// The navigation model (SPEC §5 — collapsible left nav).
//
// One list, in build order, including the modules that do not exist yet. They
// render muted and non-interactive rather than being hidden: the shell is what
// we are reviewing in Phase 2, and a nav with two items would not show whether
// the layout holds at full size. Each carries the phase it arrives in, so the
// sidebar doubles as an honest statement of what is and is not built.

export interface NavItem {
  readonly path: string
  readonly label: string
  /** Undefined once the module is built. */
  readonly arrivesInPhase?: number
}

export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/', label: 'Household' },
  { path: '/calendar', label: 'Calendar' },
  { path: '/tasks', label: 'Tasks' },
  { path: '/notes', label: 'Notes' },
  { path: '/kitchen', label: 'Kitchen' },
  { path: '/shopping', label: 'Shopping' },
  { path: '/contacts', label: 'Contacts' },
  { path: '/health', label: 'Health' },
  { path: '/settings', label: 'Settings' },
]

export function isBuilt(item: NavItem): boolean {
  return item.arrivesInPhase === undefined
}

/**
 * Which nav item a path belongs to, or null when none does.
 *
 * Longest match wins so a future `/tasks/42` resolves to Tasks rather than to
 * the root. Returning **null** for an unknown path matters: an earlier version
 * fell back to `/`, which meant a typo in the address bar quietly rendered the
 * Household page as though nothing were wrong.
 */
export function matchNavItem(pathname: string): NavItem | null {
  const matches = NAV_ITEMS.filter(
    (item) => pathname === item.path || pathname.startsWith(`${item.path}/`),
  )
  if (matches.length === 0) return null
  return matches.reduce((best, item) => (item.path.length > best.path.length ? item : best))
}

export const SIDEBAR_STORAGE_KEY = 'home-ops.sidebar-collapsed'
