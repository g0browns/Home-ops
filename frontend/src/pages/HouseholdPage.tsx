// The Phase 2 authenticated page (SPEC §5).
//
// Trivial in logic — it lists members — but deliberately built as a dense table
// rather than a friendly card grid, because a card grid would prove nothing
// about whether the Rota direction holds up under the screens that follow it.
// Every device here is one the task list, meal plan and shopping list will
// reuse: leading colour bar, banded rows, uppercase column heads, tabular
// figures, and a word alongside every colour.

import { useCallback, useEffect, useState } from 'react'

import {
  changeOwnPassword,
  clearPermission,
  createUser,
  deleteUser,
  getBelongings,
  errorMessage,
  listPermissions,
  listUsers,
  resetPassword,
  setPermission,
  updateUser,
  type Access,
  type Belongings,
  type CurrentUser,
  type HouseholdMember,
  type PermissionEntry,
} from '../api/client'
import { MemberBarCell, MemberMark } from '../components/MemberMark'
import { Modal } from '../components/Modal'
import { MODULES } from '../lib/modules'

const ROLES = ['admin', 'adult', 'limited', 'readonly'] as const


const ACCESS: readonly { value: Access | ''; label: string }[] = [
  { value: '', label: 'Same as their role' },
  { value: 'none', label: 'No access' },
  { value: 'read', label: 'Can look' },
  { value: 'write', label: 'Can change' },
]

type Load =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly members: readonly HouseholdMember[] }
  | { readonly state: 'error'; readonly message: string }

const ROLE_TONE: Record<string, string> = {
  admin: 'accent',
  adult: 'default',
  limited: 'default',
  readonly: 'default',
}

function joined(iso: string): string {
  // Locale-aware, and never a bare slash-separated date — 03/04 is ambiguous
  // across exactly the two locales most likely to be in one household.
  return new Date(iso).toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

export function HouseholdPage({ me }: { readonly me: CurrentUser }) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [permissions, setPermissions] = useState<PermissionEntry[]>([])
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<HouseholdMember | null>(null)
  const [resetting, setResetting] = useState<HouseholdMember | null>(null)
  const [removing, setRemoving] = useState<HouseholdMember | null>(null)
  const [changingOwn, setChangingOwn] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const canManage = me.permissions['users'] === 'write'

  const refresh = useCallback(async () => {
    const [people, deviations] = await Promise.all([listUsers(), listPermissions()])
    setLoad(
      people.ok
        ? { state: 'ready', members: people.data }
        : { state: 'error', message: 'Could not load the household.' },
    )
    // A member without `users` write cannot read the deviations. The grid is
    // simply not drawn for them rather than the whole page failing.
    if (deviations.ok) setPermissions(deviations.data)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  if (load.state === 'loading') return <p className="loading">Loading household…</p>
  if (load.state === 'error') {
    return (
      <div className="page">
        <p className="alert">{load.message}</p>
      </div>
    )
  }

  const admins = load.members.filter((m) => m.role === 'admin').length
  const inactive = load.members.filter((m) => !m.is_active).length

  return (
    <div className="page">
      <div className="page-head">
        <h1>Household</h1>
        <p className="page-summary">
          <strong>{load.members.length}</strong> members · <strong>{admins}</strong> admin
          {admins === 1 ? '' : 's'}
          {inactive > 0 && (
            <>
              {' '}
              · <strong>{inactive}</strong> inactive
            </>
          )}
        </p>
      </div>

      <div className="toolbar">
        {canManage && (
          <button type="button" className="button" onClick={() => setAdding(true)}>
            Add a member
          </button>
        )}
        <button type="button" onClick={() => setChangingOwn(true)}>
          Change my password
        </button>
      </div>

      {notice && (
        <p className="notice" role="status">
          {notice}{' '}
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </p>
      )}

      <p className="page-intro">
        Everyone here owns a colour. It follows them into tasks, the calendar and
        the shopping list, so you can find your own rows without reading them.
      </p>

      <div className="table-frame">
        <table className="data">
          <caption className="visually-hidden">
            Household members, their roles and when they joined
          </caption>
          <thead>
            <tr>
              <th className="lead">
                <span className="visually-hidden">Member colour</span>
              </th>
              <th scope="col">Member</th>
              <th scope="col">Username</th>
              <th scope="col">Role</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Joined
              </th>
              {canManage && (
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {load.members.map((member) => (
              <tr key={member.id}>
                <MemberBarCell member={member} />
                <td>
                  <span className="cell-name">
                    <MemberMark member={member} size="sm" />
                    {member.display_name}
                    {member.id === me.id && (
                      <span className="badge" data-tone="accent">
                        You
                      </span>
                    )}
                  </span>
                </td>
                <td className="muted">{member.username}</td>
                <td>
                  <span className="badge" data-tone={ROLE_TONE[member.role] ?? 'default'}>
                    {member.role}
                  </span>
                </td>
                <td>
                  {member.is_active ? (
                    <span className="muted">Active</span>
                  ) : (
                    <span className="badge" data-tone="danger">
                      Inactive
                    </span>
                  )}
                </td>
                <td className="num muted">{joined(member.created_at)}</td>
                {canManage && (
                  <td className="row-actions">
                    <button type="button" onClick={() => setEditing(member)}>
                      Edit
                    </button>
                    {/* Not your own: changing your own password needs the
                        current one, and this form does not ask for it. */}
                    {member.id !== me.id && (
                      <>
                        <button type="button" onClick={() => setResetting(member)}>
                          Reset password
                        </button>
                        {/* Suspend first, and by far the more common thing to
                            want: it stops them signing in and keeps everything
                            they wrote. */}
                        <button
                          type="button"
                          onClick={async () => {
                            await updateUser(member.id, { is_active: !member.is_active })
                            setNotice(
                              member.is_active
                                ? `${member.display_name} can no longer sign in. Nothing they own has been touched.`
                                : `${member.display_name} can sign in again.`,
                            )
                            await refresh()
                          }}
                        >
                          {member.is_active ? 'Suspend' : 'Reinstate'}
                        </button>
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => setRemoving(member)}
                        >
                          Delete
                        </button>
                      </>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {canManage && (
        <PermissionGrid
          members={load.members}
          permissions={permissions}
          onChanged={refresh}
        />
      )}

      {adding && (
        <MemberEditor
          member={null}
          onClose={() => setAdding(false)}
          onSaved={async (message) => {
            setAdding(false)
            setNotice(message)
            await refresh()
          }}
        />
      )}

      {editing && (
        <MemberEditor
          member={editing}
          onClose={() => setEditing(null)}
          onSaved={async (message) => {
            setEditing(null)
            setNotice(message)
            await refresh()
          }}
        />
      )}

      {resetting && (
        <PasswordReset
          member={resetting}
          onClose={() => setResetting(null)}
          onDone={(message) => {
            setResetting(null)
            setNotice(message)
          }}
        />
      )}

      {removing && (
        <DeleteMember
          member={removing}
          onClose={() => setRemoving(null)}
          onDeleted={async (message) => {
            setRemoving(null)
            setNotice(message)
            await refresh()
          }}
        />
      )}

      {changingOwn && (
        <OwnPassword onClose={() => setChangingOwn(false)} onDone={setNotice} />
      )}
    </div>
  )
}


/**
 * What each member may reach, module by module.
 *
 * Only *deviations* from the role defaults are stored, so every control offers
 * "same as their role" first and choosing it **deletes** the row rather than
 * writing one. A grid that treated the default as an explicit setting would
 * quietly freeze everybody's permissions the day a role default changed.
 */
function PermissionGrid({
  members,
  permissions,
  onChanged,
}: {
  readonly members: readonly HouseholdMember[]
  readonly permissions: readonly PermissionEntry[]
  readonly onChanged: () => Promise<void>
}) {
  const [subject, setSubject] = useState(members[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const deviationFor = (module: string): Access | '' =>
    permissions.find(
      (entry) =>
        entry.subject_type === 'user' && entry.subject_id === subject && entry.module === module,
    )?.access ?? ''

  async function choose(module: string, access: Access | '') {
    setBusy(true)
    setError(null)
    const result =
      access === ''
        ? await clearPermission({ subject_type: 'user', subject_id: subject, module })
        : await setPermission({ subject_type: 'user', subject_id: subject, module, access })
    setBusy(false)
    if (!result.ok) {
      setError(errorMessage(result.data, 'Could not change that permission.'))
      return
    }
    await onChanged()
  }

  return (
    <section className="permissions">
      <h2>What they can reach</h2>
      <p className="field-hint">
        Everything follows the member&rsquo;s role unless you change it here. An administrator
        reaches every module whatever this says &mdash; but nobody, administrator included, can
        see a private record belonging to somebody else.
      </p>

      <div className="toolbar">
        <label className="visually-hidden" htmlFor="permission-subject">
          Whose permissions
        </label>
        <select
          id="permission-subject"
          value={subject}
          onChange={(event) => setSubject(event.currentTarget.value)}
        >
          {members.map((option) => (
            <option key={option.id} value={option.id}>
              {option.display_name} — {option.role}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      <ul className="permission-list">
        {MODULES.map(([key, label]) => (
          <li key={key}>
            <label htmlFor={`perm-${key}`}>{label}</label>
            <select
              id={`perm-${key}`}
              value={deviationFor(key)}
              disabled={busy || !subject}
              onChange={(event) => void choose(key, event.currentTarget.value as Access | '')}
            >
              {ACCESS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * A new password and its confirmation.
 *
 * Typed twice because nobody can read what they typed: a single slip in a
 * masked field is invisible until somebody cannot sign in, and on the reset
 * form the person locked out is not even the one who made the mistake. The
 * check lives here rather than on the server because it is about the typing,
 * not about the password — the API takes one value and is right to.
 */
function PasswordPair({
  id,
  label,
  hint,
  value,
  confirmation,
  onValue,
  onConfirmation,
  autoFocus = false,
}: {
  readonly id: string
  readonly label: string
  readonly hint?: string
  readonly value: string
  readonly confirmation: string
  readonly onValue: (next: string) => void
  readonly onConfirmation: (next: string) => void
  readonly autoFocus?: boolean
}) {
  // Only once there is something to compare: complaining at somebody halfway
  // through typing the second field is noise.
  const mismatched = confirmation.length > 0 && value !== confirmation

  return (
    <>
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input
          id={id}
          type="password"
          value={value}
          onChange={(event) => onValue(event.currentTarget.value)}
          autoComplete="new-password"
          autoFocus={autoFocus}
          required
        />
        {hint && <p className="field-hint">{hint}</p>}
      </div>

      <div className="field">
        <label htmlFor={`${id}-again`}>Type it again</label>
        <input
          id={`${id}-again`}
          type="password"
          value={confirmation}
          onChange={(event) => onConfirmation(event.currentTarget.value)}
          autoComplete="new-password"
          aria-invalid={mismatched}
          required
        />
        {mismatched && (
          <p className="alert" role="alert">
            These two do not match.
          </p>
        )}
      </div>
    </>
  )
}

/** True when a pair is safe to submit. */
function pairReady(value: string, confirmation: string): boolean {
  return value.length > 0 && value === confirmation
}

function MemberEditor({
  member,
  onClose,
  onSaved,
}: {
  readonly member: HouseholdMember | null
  readonly onClose: () => void
  readonly onSaved: (message: string) => Promise<void>
}) {
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState(member?.display_name ?? '')
  const [password, setPassword] = useState('')
  const [passwordAgain, setPasswordAgain] = useState('')
  const [role, setRole] = useState<string>(member?.role ?? 'adult')
  const [isActive, setIsActive] = useState(member?.is_active ?? true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  return (
    <Modal
      title={member ? `Edit ${member.display_name}` : 'Add a member'}
      onClose={onClose}
      labelledBy="member-editor-title"
    >
      <form
        className="note-editor"
        onSubmit={async (event) => {
          event.preventDefault()
          setBusy(true)
          setError(null)
          const result = member
            ? await updateUser(member.id, {
                display_name: displayName.trim(),
                role,
                is_active: isActive,
              })
            : await createUser({
                username: username.trim(),
                display_name: displayName.trim(),
                password,
                role,
              })
          setBusy(false)

          if (!result.ok) {
            setError(errorMessage(result.data, 'Could not save that member.'))
            return
          }
          await onSaved(member ? 'Saved.' : `${displayName.trim()} can sign in now.`)
        }}
      >
        {!member && (
          <div className="field">
            <label htmlFor="member-username">Username</label>
            <input
              id="member-username"
              value={username}
              onChange={(event) => setUsername(event.currentTarget.value)}
              autoComplete="off"
              autoFocus
              required
            />
          </div>
        )}

        <div className="field">
          <label htmlFor="member-name">Name</label>
          <input
            id="member-name"
            value={displayName}
            onChange={(event) => setDisplayName(event.currentTarget.value)}
            required
          />
        </div>

        {!member && (
          <PasswordPair
            id="member-password"
            label="First password"
            hint="They can change it themselves once they are in."
            value={password}
            confirmation={passwordAgain}
            onValue={setPassword}
            onConfirmation={setPasswordAgain}
          />
        )}

        <div className="field">
          <label htmlFor="member-role">Role</label>
          <select
            id="member-role"
            value={role}
            onChange={(event) => setRole(event.currentTarget.value)}
          >
            {ROLES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        {member && (
          <label className="check">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(event) => setIsActive(event.currentTarget.checked)}
            />
            Can sign in
          </label>
        )}

        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}

        <div className="editor-actions">
          <button
            type="submit"
            className="button"
            disabled={busy || (!member && !pairReady(password, passwordAgain))}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}

/** An administrator setting another member's password. */
function PasswordReset({
  member,
  onClose,
  onDone,
}: {
  readonly member: HouseholdMember
  readonly onClose: () => void
  readonly onDone: (message: string) => void
}) {
  const [password, setPassword] = useState('')
  const [passwordAgain, setPasswordAgain] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  return (
    <Modal
      title={`Reset the password for ${member.display_name}`}
      onClose={onClose}
      labelledBy="reset-title"
    >
      <form
        className="note-editor"
        onSubmit={async (event) => {
          event.preventDefault()
          setBusy(true)
          setError(null)
          const result = await resetPassword(member.id, password)
          setBusy(false)
          if (!result.ok) {
            setError(errorMessage(result.data, 'Could not reset that password.'))
            return
          }
          onDone(`${member.display_name} has a new password and is signed out everywhere.`)
        }}
      >
        <p className="field-hint">
          You are setting this yourself, so tell them what it is and let them change it. This
          signs {member.display_name} out of every device.
        </p>

        <PasswordPair
          id="reset-password"
          label="New password"
          value={password}
          confirmation={passwordAgain}
          onValue={setPassword}
          onConfirmation={setPasswordAgain}
          autoFocus
        />

        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}

        <div className="editor-actions">
          <button
            type="submit"
            className="button"
            disabled={busy || !pairReady(password, passwordAgain)}
          >
            {busy ? 'Setting…' : 'Set password'}
          </button>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}

/** Your own. Needs the current one, and signs out your other sessions. */
function OwnPassword({
  onClose,
  onDone,
}: {
  readonly onClose: () => void
  readonly onDone: (message: string) => void
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [nextAgain, setNextAgain] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  return (
    <Modal title="Change my password" onClose={onClose} labelledBy="own-password-title">
      <form
        className="note-editor"
        onSubmit={async (event) => {
          event.preventDefault()
          setBusy(true)
          setError(null)
          const result = await changeOwnPassword(current, next)
          setBusy(false)
          if (!result.ok) {
            setError(
              errorMessage(result.data, 'Could not change it. Is the current password right?'),
            )
            return
          }
          onDone('Password changed. Your other devices have been signed out.')
          onClose()
        }}
      >
        <div className="field">
          <label htmlFor="current-password">Current password</label>
          <input
            id="current-password"
            type="password"
            value={current}
            onChange={(event) => setCurrent(event.currentTarget.value)}
            autoComplete="current-password"
            autoFocus
            required
          />
        </div>

        <PasswordPair
          id="new-password"
          label="New password"
          value={next}
          confirmation={nextAgain}
          onValue={setNext}
          onConfirmation={setNextAgain}
        />

        <p className="field-hint">This device stays signed in; every other one is signed out.</p>

        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}

        <div className="editor-actions">
          <button
            type="submit"
            className="button"
            disabled={busy || !pairReady(next, nextAgain)}
          >
            {busy ? 'Changing…' : 'Change password'}
          </button>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}


/**
 * Deleting a member, and everything they own.
 *
 * Three things make this a decision rather than a click:
 *
 * * **It says what goes**, counted, from the server. "Are you sure?" is not a
 *   question anybody can answer; "this removes 34 tasks, 12 recipes and their
 *   health records" is.
 * * **The username has to be typed.** Not a checkbox, and not a second button:
 *   typing the name is the one confirmation that cannot be got through by
 *   muscle memory, and this is irreversible.
 * * **Suspending is offered right here**, because it is almost always what
 *   somebody actually wants and it keeps their recipes and past events.
 */
function DeleteMember({
  member,
  onClose,
  onDeleted,
}: {
  readonly member: HouseholdMember
  readonly onClose: () => void
  readonly onDeleted: (message: string) => Promise<void>
}) {
  const [belongings, setBelongings] = useState<Belongings | null>(null)
  const [typed, setTyped] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void getBelongings(member.id).then((result) => {
      if (result.ok) setBelongings(result.data)
    })
  }, [member.id])

  const lines: string[] = []
  if (belongings) {
    const add = (count: number, one: string, many: string) => {
      if (count > 0) lines.push(`${count} ${count === 1 ? one : many}`)
    }
    add(belongings.tasks, 'task', 'tasks')
    add(belongings.notes, 'note', 'notes')
    add(belongings.recipes, 'recipe', 'recipes')
    add(belongings.events, 'calendar event', 'calendar events')
    add(belongings.contacts, 'contact', 'contacts')
    add(belongings.shopping_lists, 'shopping list', 'shopping lists')
    add(belongings.planned_meals, 'planned meal', 'planned meals')
  }

  const confirmed = typed.trim() === member.username

  return (
    <Modal
      title={`Delete ${member.display_name}?`}
      onClose={onClose}
      labelledBy="delete-member-title"
    >
      <p className="alert" role="alert">
        This cannot be undone. Deleting {member.display_name} removes their account{' '}
        <strong>and everything they own</strong>.
      </p>

      {belongings === null ? (
        <p className="loading">Working out what that includes…</p>
      ) : (
        <>
          {lines.length > 0 ? (
            <>
              <p>Their records go with them:</p>
              <ul className="delete-list">
                {lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          ) : (
            <p>They have not created anything yet.</p>
          )}

          {belongings.has_health_records && (
            /* A yes-or-no, never a count: how many health records somebody has
               is health data about them (§4.8). That it exists is what this
               decision needs. */
            <p className="alert">
              {member.display_name} has health records. Those are deleted too, and nobody
              &mdash; including you &mdash; can read them first.
            </p>
          )}
        </>
      )}

      <p className="field-hint">
        If you only want to stop them signing in, suspend them instead. That keeps everything
        above exactly where it is.
      </p>

      <div className="field">
        <label htmlFor="delete-confirm">
          Type <strong>{member.username}</strong> to confirm
        </label>
        <input
          id="delete-confirm"
          value={typed}
          onChange={(event) => setTyped(event.currentTarget.value)}
          autoComplete="off"
          autoFocus
        />
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      <div className="editor-actions">
        <button
          type="button"
          className="button danger"
          disabled={busy || !confirmed}
          onClick={async () => {
            setBusy(true)
            setError(null)
            const result = await deleteUser(member.id)
            setBusy(false)
            if (!result.ok) {
              setError(errorMessage(result.data, 'Could not delete that member.'))
              return
            }
            await onDeleted(`${member.display_name} and everything they owned have been deleted.`)
          }}
        >
          {busy ? 'Deleting…' : 'Delete for good'}
        </button>
        <button
          type="button"
          onClick={async () => {
            await updateUser(member.id, { is_active: false })
            await onDeleted(
              `${member.display_name} can no longer sign in. Nothing they own has been touched.`,
            )
          }}
        >
          Suspend instead
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
    </Modal>
  )
}
