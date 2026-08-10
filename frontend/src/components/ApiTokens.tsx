// API tokens (SPEC §4.10): create one, see the list, see when each was last
// used, revoke.
//
// Three things this UI has to get right, and each is a decision rather than a
// layout:
//
// **The plaintext is shown once and the screen has to say so before it is too
// late.** The warning sits above the token, not below it: underneath, it is
// read after the dialog has been dismissed, which is exactly when it stops
// being useful. There is no "show again" because there is nothing to show —
// the server keeps a hash.
//
// **Copying must not depend on the clipboard API.** `navigator.clipboard` is
// secure-context only, and SPEC §2.1 puts two of the three access paths on
// plain HTTP, where it is simply absent. So the token is a readonly input that
// selects itself on focus and can be copied with the keyboard; the button is a
// convenience that appears when the API is there and is never the only way.
//
// **An unnarrowed token is offered last, not first.** It is the honest default
// on the server — a token with no scope rows means "everything I can do" — but
// a form that opens on it invites you to accept it. The picker opens narrowed
// and makes you say so.

import { useCallback, useEffect, useState } from 'react'

import {
  clearRevokedTokens,
  createToken,
  errorMessage,
  listTokens,
  revokeToken,
  type Access,
  type ApiToken,
  type CurrentUser,
  type TokenCreated,
} from '../api/client'
import { MODULES, moduleLabel } from '../lib/modules'
import { Modal } from './Modal'

/** What a token may do in a module. `none` is not offered: leaving a module out
    is the same thing and is one fewer way to say it. */
const SCOPE_CHOICES: readonly { value: '' | Access; label: string }[] = [
  { value: '', label: 'No access' },
  { value: 'read', label: 'Can look' },
  { value: 'write', label: 'Can change' },
]

function when(value: string | null, never: string): string {
  if (!value) return never
  return new Date(value).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function status(token: ApiToken): { label: string; tone: string } {
  if (token.revoked_at) return { label: 'Revoked', tone: 'danger' }
  if (token.expires_at && new Date(token.expires_at) <= new Date()) {
    return { label: 'Expired', tone: 'danger' }
  }
  return { label: 'Active', tone: 'positive' }
}

export function ApiTokens({ me }: { readonly me: CurrentUser }) {
  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [creating, setCreating] = useState(false)
  const [issued, setIssued] = useState<TokenCreated | null>(null)
  const [revoking, setRevoking] = useState<ApiToken | null>(null)
  const [clearing, setClearing] = useState(false)

  const refresh = useCallback(async () => {
    const result = await listTokens()
    if (result.ok) setTokens(result.data)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function revoke() {
    if (!revoking) return
    await revokeToken(revoking.id)
    setRevoking(null)
    await refresh()
  }

  async function clearRevoked() {
    await clearRevokedTokens()
    setClearing(false)
    await refresh()
  }

  const revoked = tokens.filter((token) => token.revoked_at !== null).length

  return (
    <section className="settings-section">
      <h2>API access</h2>
      <p className="page-intro">
        A token lets a script or another app act as you, without your password.
        It can never do more than you can: take a module away from your account
        and every token you hold loses it too, straight away.
      </p>

      {tokens.length === 0 ? (
        <p className="muted">You have no tokens.</p>
      ) : (
        <div className="table-frame">
          <table className="data token-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Token</th>
                <th scope="col">Can reach</th>
                <th scope="col">Last used</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => {
                const state = status(token)
                return (
                  <tr key={token.id} data-inactive={token.revoked_at ? '' : undefined}>
                    <td>
                      <strong>{token.name}</strong>
                      <span className="token-sub">
                        Created {when(token.created_at, '')}
                        {token.expires_at
                          ? ` · expires ${when(token.expires_at, '')}`
                          : ' · no expiry'}
                      </span>
                    </td>
                    <td>
                      <code className="token-prefix">{token.prefix}…</code>
                    </td>
                    <td>
                      {token.scopes.length === 0 ? (
                        <span className="token-sub">Everything you can</span>
                      ) : (
                        <span className="token-scopes">
                          {token.scopes.map((scope) => (
                            <span key={scope.module} className="badge">
                              {moduleLabel(scope.module)}
                              {scope.access === 'read' ? ' · look' : ' · change'}
                            </span>
                          ))}
                        </span>
                      )}
                    </td>
                    <td>{when(token.last_used_at, 'Never')}</td>
                    <td>
                      {/* A word, not only a colour — see the design rules. */}
                      <span className="badge" data-tone={state.tone}>
                        {state.label}
                      </span>
                    </td>
                    <td className="row-actions">
                      {!token.revoked_at && (
                        <button type="button" onClick={() => setRevoking(token)}>
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="editor-actions">
        <button type="button" className="button" onClick={() => setCreating(true)}>
          New token
        </button>
        {/* Only when it would do something, and it says how many so the number
            is on the button rather than discovered afterwards. */}
        {revoked > 0 && (
          <button type="button" onClick={() => setClearing(true)}>
            Clear {revoked} revoked
          </button>
        )}
      </p>

      {creating && (
        <TokenEditor
          me={me}
          onClose={() => setCreating(false)}
          onCreated={async (token) => {
            setCreating(false)
            setIssued(token)
            await refresh()
          }}
        />
      )}

      {issued && <IssuedToken token={issued} onClose={() => setIssued(null)} />}

      {revoking && (
        <Modal
          title="Revoke this token?"
          onClose={() => setRevoking(null)}
          footer={
            <>
              <button type="button" onClick={() => setRevoking(null)}>
                Keep it
              </button>
              <button type="button" className="button danger" onClick={() => void revoke()}>
                Revoke {revoking.name}
              </button>
            </>
          }
        >
          <p>
            Anything using <strong>{revoking.name}</strong> stops working at
            once. This cannot be undone — a new token is a new secret.
          </p>
          <p className="field-hint">
            The entry stays in this list, so you keep the record of when it was
            last used.
          </p>
        </Modal>
      )}

      {clearing && (
        <Modal
          title={`Clear ${revoked} revoked ${revoked === 1 ? 'token' : 'tokens'}?`}
          onClose={() => setClearing(false)}
          footer={
            <>
              <button type="button" onClick={() => setClearing(false)}>
                Keep them
              </button>
              <button type="button" className="button danger" onClick={() => void clearRevoked()}>
                Clear {revoked}
              </button>
            </>
          }
        >
          <p>
            Removes the rows for tokens you have already revoked.{' '}
            <strong>Nothing that still works is touched</strong> &mdash; a
            revoked token stopped authenticating the moment it was revoked.
          </p>
          <p className="field-hint">
            You lose the last-used record for those rows. The audit log keeps
            when each was created and revoked, so the history survives it.
          </p>
        </Modal>
      )}
    </section>
  )
}

function TokenEditor({
  me,
  onClose,
  onCreated,
}: {
  readonly me: CurrentUser
  readonly onClose: () => void
  readonly onCreated: (token: TokenCreated) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [expires, setExpires] = useState('')
  const [narrowed, setNarrowed] = useState(true)
  const [scopes, setScopes] = useState<Record<string, Access>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Only modules the holder can actually reach are offered. Scoping a token to
  // something you cannot do yourself produces a token that is silently dead in
  // that module, and a form should not offer a choice that does nothing.
  const reachable = MODULES.filter(([key]) => me.permissions[key] !== 'none')

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    const result = await createToken({
      name: name.trim(),
      // Omitted rather than sent empty: an empty object and an absent one mean
      // the same thing to the server, but only one of them says so.
      ...(narrowed ? { scopes } : {}),
      // A date is a local wall-clock day; the server wants an instant. End of
      // that day, so "expires 1 August" means the whole of the 1st.
      expires_at: expires ? new Date(`${expires}T23:59:59`).toISOString() : null,
    })
    setBusy(false)
    if (!result.ok) {
      setError(errorMessage(result.data, 'Could not create that token.'))
      return
    }
    await onCreated(result.data)
  }

  return (
    <Modal
      title="New API token"
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form="token-form" className="button" disabled={busy}>
            Create token
          </button>
        </>
      }
    >
      <form id="token-form" onSubmit={submit}>
        {error && <p className="alert">{error}</p>}

        <label className="field">
          <span className="field-label">What is it for?</span>
          <input
            name="name"
            value={name}
            maxLength={60}
            required
            autoFocus
            placeholder="Home Assistant"
            onChange={(event) => setName(event.target.value)}
          />
          <span className="field-hint">
            A token nobody recognises is a token nobody dares revoke.
          </span>
        </label>

        <label className="field">
          <span className="field-label">Expires</span>
          <input
            type="date"
            name="expires_at"
            value={expires}
            onChange={(event) => setExpires(event.target.value)}
          />
          <span className="field-hint">
            Optional, and worth setting: a token with no expiry outlives whoever
            remembers issuing it.
          </span>
        </label>

        <fieldset className="field">
          <legend className="field-label">What may it reach?</legend>
          <label className="check">
            <input
              type="checkbox"
              checked={narrowed}
              onChange={(event) => setNarrowed(event.target.checked)}
            />
            <span>Limit it to certain things</span>
          </label>

          {narrowed ? (
            <div className="scope-grid">
              {reachable.map(([key, label]) => (
                <label key={key} className="scope-row">
                  <span>{label}</span>
                  <select
                    value={scopes[key] ?? ''}
                    onChange={(event) => {
                      const value = event.target.value as '' | Access
                      setScopes((current) => {
                        const next = { ...current }
                        if (value === '') delete next[key]
                        else next[key] = value
                        return next
                      })
                    }}
                  >
                    {SCOPE_CHOICES.filter(
                      // Do not offer "can change" where the holder can only look.
                      (choice) => choice.value !== 'write' || me.permissions[key] === 'write',
                    ).map((choice) => (
                      <option key={choice.value} value={choice.value}>
                        {choice.label}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
              {Object.keys(scopes).length === 0 && (
                <p className="field-hint">
                  Nothing chosen yet. A token that reaches nothing is refused
                  everywhere, which is safe but not much use.
                </p>
              )}
            </div>
          ) : (
            <p className="field-hint">
              It will be able to do everything you can do — and no more. If your
              own access changes, so does the token&rsquo;s.
            </p>
          )}
        </fieldset>
      </form>
    </Modal>
  )
}

function IssuedToken({
  token,
  onClose,
}: {
  readonly token: TokenCreated
  readonly onClose: () => void
}) {
  const [copied, setCopied] = useState(false)
  // Read once at render: it is a capability check, not state that changes.
  const canCopy = typeof navigator !== 'undefined' && Boolean(navigator.clipboard)

  return (
    <Modal
      title="Copy your token now"
      onClose={onClose}
      footer={
        <button type="button" onClick={onClose}>
          I have saved it
        </button>
      }
    >
      {/* Above the token, not below it: a warning read after the dialog closes
          is a warning that arrived too late. */}
      <p className="alert">
        This is the only time it will be shown. We keep a hash, so it cannot be
        looked up later by anyone &mdash; if you lose it, revoke it and make
        another.
      </p>

      <label className="field">
        <span className="field-label">Token for {token.name}</span>
        <input
          className="token-secret"
          readOnly
          value={token.token}
          autoFocus
          onFocus={(event) => event.target.select()}
        />
      </label>

      {canCopy && (
        <p className="editor-actions">
          <button
            type="button"
            className="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(token.token)
                setCopied(true)
              } catch {
                // Nothing to do: the field above is selected and copyable, and
                // that is the path that always works.
                setCopied(false)
              }
            }}
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
        </p>
      )}

      <p className="field-hint">
        Send it as a header: <code>Authorization: Bearer {token.prefix}…</code>
      </p>
    </Modal>
  )
}
