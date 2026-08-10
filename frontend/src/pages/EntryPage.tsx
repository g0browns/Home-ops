// Sign in, and first-run setup. The two unauthenticated screens, restyled onto
// the Rota tokens — same type, same accent, same squared radii as the shell, so
// the app does not visibly change identity at the door.

import { useState, type FormEvent } from 'react'

import { claimHousehold, errorMessage, login as apiLogin, type SetupStatus } from '../api/client'

function Brand() {
  return (
    <div className="entry-brand">
      <span className="brand-rule" aria-hidden="true" />
      <span>Home Ops</span>
    </div>
  )
}

export function SignInPage({ onDone }: { readonly onDone: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)

    const form = new FormData(event.currentTarget)
    const result = await apiLogin({
      username: String(form.get('username') ?? ''),
      password: String(form.get('password') ?? ''),
    })

    setBusy(false)
    if (result.ok) {
      onDone()
      return
    }
    setError(
      result.status === 429
        ? 'Too many failed attempts. Wait a few minutes, then try again.'
        : errorMessage(result.data, 'Could not sign in.'),
    )
  }

  return (
    <div className="entry">
      <div className="entry-card">
        <Brand />
        <h1>Sign in</h1>

        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="username">Username</label>
            <input id="username" name="username" autoComplete="username" required autoFocus />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
            />
          </div>

          {error && (
            <p className="alert" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="button" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}

export function SetupPage({
  status,
  onDone,
}: {
  readonly status: SetupStatus
  readonly onDone: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)

    const form = new FormData(event.currentTarget)
    const result = await claimHousehold({
      username: String(form.get('username') ?? ''),
      display_name: String(form.get('display_name') ?? ''),
      password: String(form.get('password') ?? ''),
    })

    setBusy(false)
    if (result.ok) onDone()
    else setError(errorMessage(result.data, 'Could not set up the household.'))
  }

  if (!status.can_setup_here) {
    return (
      <div className="entry">
        <div className="entry-card">
          <Brand />
          <h1>Set up this household</h1>
          <p className="alert">
            {status.reason ?? 'Setup is not available over this connection.'}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="entry">
      <div className="entry-card">
        <Brand />
        <h1>Set up this household</h1>
        <p className="entry-note">
          No accounts exist yet. The one you create here becomes the administrator.
        </p>

        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="setup-username">Username</label>
            <input
              id="setup-username"
              name="username"
              autoComplete="username"
              required
              autoFocus
            />
          </div>

          <div className="field">
            <label htmlFor="setup-display">Display name</label>
            <input id="setup-display" name="display_name" autoComplete="name" required />
          </div>

          <div className="field">
            <label htmlFor="setup-password">Password</label>
            <input
              id="setup-password"
              name="password"
              type="password"
              autoComplete="new-password"
              minLength={12}
              required
            />
            <span className="field-hint">
              At least 12 characters. Length matters more than punctuation.
            </span>
          </div>

          {error && (
            <p className="alert" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="button" disabled={busy}>
            {busy ? 'Creating…' : 'Create administrator'}
          </button>
        </form>
      </div>
    </div>
  )
}
