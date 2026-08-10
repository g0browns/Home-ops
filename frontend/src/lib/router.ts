// A deliberately tiny router.
//
// Phase 2 has two real routes. Pulling in react-router for that would be
// scaffolding for a phase that has not arrived (SPEC §0), and this is thirty
// lines that can be deleted wholesale when Phase 3 brings enough routes to
// justify the dependency.
//
// Real paths rather than hashes, because nginx already serves an SPA history
// fallback and the tailnet/LAN paths behave identically.

import { useEffect, useState } from 'react'

const NAVIGATION_EVENT = 'home-ops:navigate'

export function navigate(to: string): void {
  if (to === window.location.pathname) return
  window.history.pushState({}, '', to)
  window.dispatchEvent(new Event(NAVIGATION_EVENT))
}

export function usePathname(): string {
  const [pathname, setPathname] = useState(() => window.location.pathname)

  useEffect(() => {
    const sync = () => setPathname(window.location.pathname)
    // popstate covers the back button; the custom event covers our own pushes,
    // which deliberately do not fire popstate.
    window.addEventListener('popstate', sync)
    window.addEventListener(NAVIGATION_EVENT, sync)
    return () => {
      window.removeEventListener('popstate', sync)
      window.removeEventListener(NAVIGATION_EVENT, sync)
    }
  }, [])

  return pathname
}

/** Intercepts a plain left-click so in-app links do not reload the document. */
export function linkHandler(to: string) {
  return (event: React.MouseEvent<HTMLAnchorElement>) => {
    // Let the browser handle modified clicks — open-in-new-tab must keep working.
    if (event.defaultPrevented || event.button !== 0) return
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return

    event.preventDefault()
    navigate(to)
  }
}
