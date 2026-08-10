// Entry point. Decides which of three states the app is in — unclaimed, signed
// out, signed in — and renders the shell for the third.

// Fonts are npm dependencies, bundled at build time. Nothing is fetched from a
// CDN at runtime: SPEC §7 rules out cloud dependencies, and two of the three
// access paths in §2.1 are plain HTTP on a private network with no route out.
import '@fontsource/archivo-narrow/400.css'
import '@fontsource/archivo-narrow/500.css'
import '@fontsource/archivo-narrow/600.css'
import '@fontsource/archivo-narrow/700.css'
import '@fontsource/archivo/400.css'
import '@fontsource/archivo/500.css'
import '@fontsource/archivo/600.css'

import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
import './styles/modules.css'

import { StrictMode, useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import {
  getHouseholdSettings,
  getMe,
  getSetupStatus,
  logout as apiLogout,
  type CurrentUser,
  type SetupStatus,
} from './api/client'
import { AppShell } from './components/AppShell'
import { useTheme } from './hooks/usePreferences'
import { matchNavItem } from './lib/nav'
import { usePathname } from './lib/router'
import { CalendarPage } from './pages/CalendarPage'
import { HouseholdPage } from './pages/HouseholdPage'
import { ContactsPage } from './pages/ContactsPage'
import { HealthPage } from './pages/HealthPage'
import { KitchenPage } from './pages/KitchenPage'
import { ShoppingPage } from './pages/ShoppingPage'
import { NotesPage } from './pages/NotesPage'
import { SettingsPage } from './pages/SettingsPage'
import { TasksPage } from './pages/TasksPage'
import { SetupPage, SignInPage } from './pages/EntryPage'

type Session =
  | { readonly state: 'loading' }
  | { readonly state: 'setup'; readonly status: SetupStatus }
  | { readonly state: 'signed-out' }
  | { readonly state: 'signed-in'; readonly user: CurrentUser }
  | { readonly state: 'unreachable'; readonly message: string }

function useSession() {
  const [session, setSession] = useState<Session>({ state: 'loading' })

  const refresh = useCallback(async () => {
    try {
      const me = await getMe()
      if (me.ok) {
        setSession({ state: 'signed-in', user: me.data })
        return
      }

      const setup = await getSetupStatus()
      setSession(
        setup.ok && setup.data.needs_setup
          ? { state: 'setup', status: setup.data }
          : { state: 'signed-out' },
      )
    } catch (error) {
      setSession({
        state: 'unreachable',
        message: error instanceof Error ? error.message : 'The API did not respond.',
      })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { session, refresh }
}

/** Household name, so the header says whose house this is rather than "Home Ops". */
function useHouseholdName(signedIn: boolean): string {
  const [name, setName] = useState('Household')

  useEffect(() => {
    if (!signedIn) return
    let cancelled = false
    void getHouseholdSettings().then((result) => {
      if (cancelled || !result.ok) return
      const value = result.data.values['household_name']
      if (typeof value === 'string' && value.trim()) setName(value)
    })
    return () => {
      cancelled = true
    }
  }, [signedIn])

  return name
}

/**
 * Which page a path renders.
 *
 * Explicit rather than "anything falls through to Household": that fallback is
 * what let the Settings nav link navigate to /settings, highlight itself, and
 * then quietly show the Household page. A route with nothing behind it should
 * say so.
 */
function Route({ pathname, me }: { readonly pathname: string; readonly me: CurrentUser }) {
  const item = matchNavItem(pathname)

  switch (item?.path) {
    case '/':
      return <HouseholdPage me={me} />
    case '/calendar':
      return <CalendarPage me={me} />
    case '/tasks':
      return <TasksPage me={me} />
    case '/notes':
      return <NotesPage me={me} />
    case '/kitchen':
      return <KitchenPage me={me} />
    case '/shopping':
      return <ShoppingPage me={me} />
    case '/contacts':
      return <ContactsPage me={me} />
    case '/health':
      return <HealthPage me={me} />
    case '/settings':
      return <SettingsPage me={me} />
    default:
      break
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>{item ? item.label : 'Not found'}</h1>
      </div>
      <p className="page-intro">
        {item?.arrivesInPhase
          ? `This part of the app arrives in phase ${item.arrivesInPhase}.`
          : `There is nothing at ${pathname}.`}
      </p>
    </div>
  )
}

function App() {
  const { session, refresh } = useSession()
  const signedIn = session.state === 'signed-in'
  const householdName = useHouseholdName(signedIn)
  const pathname = usePathname()

  // Applies the theme on the signed-out screens too, so setup and sign-in
  // already honour the machine's preference before there is an account to store
  // one against.
  useTheme(signedIn)

  const signOut = useCallback(() => {
    void apiLogout().then(refresh)
  }, [refresh])

  switch (session.state) {
    case 'loading':
      return <p className="loading">Loading…</p>
    case 'unreachable':
      return (
        <div className="entry">
          <p className="alert" role="alert">
            Could not reach the API: {session.message}
          </p>
        </div>
      )
    case 'setup':
      return <SetupPage status={session.status} onDone={() => void refresh()} />
    case 'signed-out':
      return <SignInPage onDone={() => void refresh()} />
    case 'signed-in':
      return (
        <AppShell user={session.user} householdName={householdName} onSignOut={signOut}>
          <Route pathname={pathname} me={session.user} />
        </AppShell>
      )
  }
}

const container = document.getElementById('root')
if (!container) throw new Error('#root is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
