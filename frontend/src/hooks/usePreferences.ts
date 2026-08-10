// Theme and sidebar state (SPEC §6 — "collapsible left sidebar, persisting its
// state" and "light and dark mode, both first-class").
//
// Both live in two places on purpose:
//
//   localStorage  read synchronously on first paint, so the app never flashes
//                 the wrong theme or a briefly-expanded sidebar while the API
//                 round-trip is in flight.
//   the server    the source of truth, so the preference follows you between
//                 the Cloudflare, tailnet and LAN paths — which are three
//                 separate origins with three separate cookie jars (SPEC §2.1)
//                 and therefore three separate localStorages.
//
// Writes are optimistic: the UI changes immediately and the server call trails
// it. A failed write leaves the local value in place rather than snapping the
// interface back, because losing a theme toggle is not worth a visible error.

import { useCallback, useEffect, useState } from 'react'

import { getMySettings, updateMySetting } from '../api/client'
import { SIDEBAR_STORAGE_KEY } from '../lib/nav'
import {
  applyThemePreference,
  isThemePreference,
  readStoredPreference,
  storePreference,
  systemPrefersDark,
  type ResolvedTheme,
  type ThemePreference,
} from '../lib/theme'
import { resolveTheme } from '../lib/theme'

export function useTheme(signedIn: boolean) {
  const [preference, setPreference] = useState<ThemePreference>(() => readStoredPreference())
  const [systemDark, setSystemDark] = useState(() => systemPrefersDark())

  // Apply before paint so there is no flash of the previous theme.
  useEffect(() => {
    applyThemePreference(preference)
  }, [preference])

  // Track the OS changing while the page is open — only meaningful while the
  // preference is "system", but harmless to keep current either way.
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const query = matchMedia('(prefers-color-scheme: dark)')
    const sync = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  // Adopt the server's value once signed in, so the preference follows the user
  // between access paths.
  useEffect(() => {
    if (!signedIn) return
    let cancelled = false
    void getMySettings().then((result) => {
      if (cancelled || !result.ok) return
      const stored = result.data.values['theme']
      if (isThemePreference(stored)) {
        setPreference(stored)
        storePreference(stored)
      }
    })
    return () => {
      cancelled = true
    }
  }, [signedIn])

  const choose = useCallback(
    (next: ThemePreference) => {
      setPreference(next)
      storePreference(next)
      void updateMySetting('theme', next)
    },
    [],
  )

  const resolved: ResolvedTheme = resolveTheme(preference, systemDark)
  return { preference, resolved, choose }
}

function readStoredCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

export function useSidebar(signedIn: boolean) {
  const [collapsed, setCollapsed] = useState<boolean>(readStoredCollapsed)

  useEffect(() => {
    if (!signedIn) return
    let cancelled = false
    void getMySettings().then((result) => {
      if (cancelled || !result.ok) return
      const stored = result.data.values['sidebar_collapsed']
      if (typeof stored === 'boolean') {
        setCollapsed(stored)
        try {
          localStorage.setItem(SIDEBAR_STORAGE_KEY, String(stored))
        } catch {
          /* storage unavailable; the server copy still holds */
        }
      }
    })
    return () => {
      cancelled = true
    }
  }, [signedIn])

  const toggle = useCallback(() => {
    setCollapsed((previous) => {
      const next = !previous
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next))
      } catch {
        /* storage unavailable */
      }
      void updateMySetting('sidebar_collapsed', next)
      return next
    })
  }, [])

  return { collapsed, toggle }
}
