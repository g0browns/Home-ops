// The application shell (SPEC §5 Phase 2, §6).
//
// Collapsible left navigation whose state persists, a header carrying identity
// and theme, and a main region. Landmarks are real elements — nav/header/main —
// and the first focusable thing on the page is a skip link, because a nav of
// eight items in front of the content is exactly the case skip links exist for.

import { useEffect, useRef, useState, type ReactNode } from 'react'

import { useSidebar, useTheme } from '../hooks/usePreferences'
import { NAV_ITEMS, isBuilt, matchNavItem } from '../lib/nav'
import { linkHandler, usePathname } from '../lib/router'
import { THEME_PREFERENCES, themeLabel, type ThemePreference } from '../lib/theme'
import type { CurrentUser } from '../api/client'
import { Icon, iconForNav } from './icons'
import { MemberMark } from './MemberMark'
import './AppShell.css'

/** Closes a popover on outside click and on Escape, and restores focus. */
function useDismissable(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    const onPointerDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) close()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close()
        ref.current?.querySelector<HTMLButtonElement>('.menu-trigger')?.focus()
      }
    }

    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, close])

  return ref
}

function ThemeMenu({
  preference,
  onChoose,
}: {
  readonly preference: ThemePreference
  readonly onChoose: (next: ThemePreference) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useDismissable(open, () => setOpen(false))
  const glyph = { system: 'monitor', light: 'sun', dark: 'moon' } as const

  return (
    <div className="menu-anchor" ref={ref}>
      <button
        type="button"
        className="icon-button menu-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name={glyph[preference]} />
        <span className="visually-hidden">Theme: {themeLabel(preference)}</span>
      </button>

      {open && (
        <div className="menu-panel" role="menu">
          <p className="menu-heading">Theme</p>
          {THEME_PREFERENCES.map((option) => (
            <button
              key={option}
              type="button"
              role="menuitemradio"
              aria-checked={option === preference}
              className="menu-item"
              onClick={() => {
                onChoose(option)
                setOpen(false)
              }}
            >
              <Icon name={glyph[option]} />
              <span>{themeLabel(option)}</span>
              <span className="icon-check">
                <Icon name="check" />
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function UserMenu({ user, onSignOut }: { readonly user: CurrentUser; readonly onSignOut: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useDismissable(open, () => setOpen(false))

  return (
    <div className="menu-anchor" ref={ref}>
      <button
        type="button"
        className="menu-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <MemberMark member={user} size="sm" />
        <span>{user.display_name}</span>
        <Icon name="chevron" />
      </button>

      {open && (
        <div className="menu-panel" role="menu">
          <p className="menu-heading">
            {user.username} · {user.role}
          </p>
          <div className="menu-separator" />
          <button type="button" role="menuitem" className="menu-item" onClick={onSignOut}>
            <Icon name="signout" />
            <span>Sign out</span>
          </button>
        </div>
      )}
    </div>
  )
}

export function AppShell({
  user,
  householdName,
  onSignOut,
  children,
}: {
  readonly user: CurrentUser
  readonly householdName: string
  readonly onSignOut: () => void
  readonly children: ReactNode
}) {
  const pathname = usePathname()
  const current = matchNavItem(pathname)?.path ?? null
  const { collapsed, toggle } = useSidebar(true)
  const { preference, choose } = useTheme(true)

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <div className="shell" data-collapsed={collapsed}>
        {/* Narrow screens turn the sidebar into an overlay drawer; without a
            backdrop there is nothing to tap to dismiss it. CSS hides this
            entirely on wide screens, where the nav is docked. */}
        {!collapsed && (
          <button
            type="button"
            className="nav-backdrop"
            onClick={toggle}
            aria-label="Close navigation"
          />
        )}

        <nav className="shell-nav" id="primary-nav" aria-label="Primary">
          <div className="shell-brand">
            <span className="brand-rule" aria-hidden="true" />
            <span className="brand-name">Home Ops</span>
          </div>

          <ul className="nav-list">
            {NAV_ITEMS.map((item) => {
              const built = isBuilt(item)
              const isCurrent = built && item.path === current
              return (
                <li key={item.path}>
                  <a
                    className="nav-link"
                    href={built ? item.path : undefined}
                    aria-current={isCurrent ? 'page' : undefined}
                    aria-disabled={built ? undefined : 'true'}
                    tabIndex={built ? undefined : -1}
                    title={collapsed ? item.label : undefined}
                    onClick={built ? linkHandler(item.path) : undefined}
                  >
                    <Icon name={iconForNav(item.label)} />
                    <span className="nav-label">{item.label}</span>
                    {!built && (
                      <span className="nav-phase">
                        <span className="visually-hidden">Not built yet, arrives in </span>
                        ph{item.arrivesInPhase}
                      </span>
                    )}
                  </a>
                </li>
              )
            })}
          </ul>
        </nav>

        <header className="shell-header">
          <button
            type="button"
            className="icon-button"
            aria-expanded={!collapsed}
            aria-controls="primary-nav"
            onClick={toggle}
          >
            <Icon name="sidebar" />
            <span className="visually-hidden">
              {collapsed ? 'Expand navigation' : 'Collapse navigation'}
            </span>
          </button>

          <span className="shell-household">{householdName}</span>

          <div className="shell-header-right">
            <ThemeMenu preference={preference} onChoose={choose} />
            <UserMenu user={user} onSignOut={onSignOut} />
          </div>
        </header>

        <main className="shell-main" id="main" tabIndex={-1}>
          {children}
        </main>
      </div>
    </>
  )
}
