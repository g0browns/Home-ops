// The contact directory (SPEC §4.7, phase 7).
//
// A directory is looked *up*, not browsed: the thing somebody wants is a number
// for the plumber, and they want it in one screen with no clicks. So the list
// carries the numbers and emails rather than hiding them behind a record, and
// the search box is the first control on the page.
//
// Three states, like the Kitchen: the list, one contact open to read, and the
// editor. Reading is the default and editing is the detour.
//
// **No CardDAV**, settled 2026-07-31 — vCard import and export only. The import
// previews before it writes, because it is a bulk write over records somebody
// already has, and that is the arrangement the Mealie import proved.

import { useCallback, useEffect, useState } from 'react'

import {
  contactsExportUrl,
  createContact,
  deleteContact,
  errorMessage,
  getContact,
  importContacts,
  listContactTags,
  listContacts,
  updateContact,
  type Contact,
  type ContactAddress,
  type ContactImportResult,
  type ContactSummary,
  type CurrentUser,
  type Visibility,
} from '../api/client'
import { Modal } from '../components/Modal'

/** "City, ST 62704" — the US line, skipping whichever parts are missing. */
function cityLine(address: ContactAddress): string {
  const cityState = [address.locality, address.region].filter(Boolean).join(', ')
  return [cityState, address.postcode].filter(Boolean).join(' ')
}

type Mode =
  | { readonly kind: 'list' }
  | { readonly kind: 'read'; readonly contact: Contact }
  | { readonly kind: 'edit'; readonly contact: Contact | null }

export function ContactsPage({ me }: { readonly me: CurrentUser }) {
  const [contacts, setContacts] = useState<ContactSummary[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [tag, setTag] = useState('')
  const [mode, setMode] = useState<Mode>({ kind: 'list' })
  const [importing, setImporting] = useState(false)
  const [loading, setLoading] = useState(true)

  const canWrite = me.permissions['contacts'] === 'write'

  const refresh = useCallback(async () => {
    setLoading(true)
    const [found, knownTags] = await Promise.all([
      listContacts({ search: search.trim() || undefined, tag: tag || undefined }),
      listContactTags(),
    ])
    setLoading(false)
    if (found.ok) setContacts(found.data)
    if (knownTags.ok) setTags(knownTags.data)
  }, [search, tag])

  useEffect(() => {
    // Debounced, so typing does not fire a request per keystroke.
    const timer = setTimeout(() => void refresh(), 150)
    return () => clearTimeout(timer)
  }, [refresh])

  async function open(id: string) {
    const result = await getContact(id)
    if (result.ok) setMode({ kind: 'read', contact: result.data })
  }

  async function reopen(id: string) {
    const result = await getContact(id)
    setMode(result.ok ? { kind: 'read', contact: result.data } : { kind: 'list' })
    await refresh()
  }

  if (mode.kind === 'read') {
    return (
      <ContactView
        contact={mode.contact}
        canWrite={canWrite}
        onBack={() => {
          setMode({ kind: 'list' })
          void refresh()
        }}
        onEdit={() => setMode({ kind: 'edit', contact: mode.contact })}
        onDelete={async () => {
          await deleteContact(mode.contact.id)
          setMode({ kind: 'list' })
          await refresh()
        }}
      />
    )
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Contacts</h1>
        <p className="page-summary">
          <strong>{contacts.length}</strong> {contacts.length === 1 ? 'contact' : 'contacts'}
        </p>
      </div>

      <div className="toolbar">
        <input
          type="search"
          className="search"
          placeholder="Search a name, a number, an address…"
          aria-label="Search contacts"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
        />

        <label className="visually-hidden" htmlFor="contact-tag">
          Filter by tag
        </label>
        <select id="contact-tag" value={tag} onChange={(event) => setTag(event.currentTarget.value)}>
          <option value="">All tags</option>
          {tags.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        {canWrite && (
          <button
            type="button"
            className="button"
            onClick={() => setMode({ kind: 'edit', contact: null })}
          >
            New contact
          </button>
        )}

        {/* A plain link, not a fetch: the browser downloads it with the session
            cookie attached, and a relative URL works on all three access paths
            (§2.1). The filters ride along so what you exported is what you were
            looking at. */}
        <a
          className="link-button"
          href={contactsExportUrl({ search: search.trim() || undefined, tag: tag || undefined })}
          download="contacts.vcf"
        >
          Export vCard
        </a>

        {canWrite && (
          <button type="button" className="link-button" onClick={() => setImporting(true)}>
            Import vCard
          </button>
        )}
      </div>

      {mode.kind === 'edit' && (
        <ContactEditor
          contact={mode.contact}
          knownTags={tags}
          onCancel={() =>
            mode.contact ? void reopen(mode.contact.id) : setMode({ kind: 'list' })
          }
          onSaved={(saved) => void reopen(saved.id)}
        />
      )}

      {importing && (
        <VCardImport
          onClose={() => setImporting(false)}
          onImported={async () => {
            setImporting(false)
            await refresh()
          }}
        />
      )}

      {loading && contacts.length === 0 ? (
        <p className="loading">Loading…</p>
      ) : contacts.length === 0 ? (
        <p className="empty">
          {search || tag
            ? 'Nobody matches that.'
            : 'No contacts yet. Add the plumber, the doctor and the school and this page starts earning its place.'}
        </p>
      ) : (
        <ul className="directory">
          {contacts.map((contact) => (
            <li key={contact.id} className="contact-row">
              <button type="button" className="contact-name" onClick={() => void open(contact.id)}>
                {contact.display_name}
              </button>

              <p className="contact-meta">
                {contact.job_title && <span>{contact.job_title}</span>}
                {contact.organisation && <span>{contact.organisation}</span>}
                {contact.visibility === 'private' && (
                  // The word, not a tint. Whether a record is private is the one
                  // thing on this page nobody should have to guess at.
                  <span className="private-flag">Only you</span>
                )}
              </p>

              {/* The numbers, on the list. `tel:` and `mailto:` because this is
                  read on a phone standing in a hallway. */}
              <ul className="contact-lines">
                {contact.phones.map((phone) => (
                  <li key={phone.number}>
                    <a href={`tel:${phone.number.replace(/\s/g, '')}`}>{phone.number}</a>
                    {phone.label && <span className="muted"> {phone.label}</span>}
                  </li>
                ))}
                {contact.emails.map((email) => (
                  <li key={email.address}>
                    <a href={`mailto:${email.address}`}>{email.address}</a>
                  </li>
                ))}
              </ul>

              {contact.tags.length > 0 && (
                <p className="contact-tags">
                  {/* `.badge`, the same chip the notes board uses for its tags.
                      A second look for the same idea would be a second thing to
                      keep in step. */}
                  {contact.tags.map((name) => (
                    <span key={name} className="badge">
                      {name}
                    </span>
                  ))}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ContactView({
  contact,
  canWrite,
  onBack,
  onEdit,
  onDelete,
}: {
  readonly contact: Contact
  readonly canWrite: boolean
  readonly onBack: () => void
  readonly onEdit: () => void
  readonly onDelete: () => Promise<void>
}) {
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="page">
      <div className="page-head">
        <button type="button" className="link-button" onClick={onBack}>
          ← All contacts
        </button>
      </div>

      <article className="contact-detail">
        <h1>{contact.display_name}</h1>
        <p className="contact-meta">
          {contact.job_title && <span>{contact.job_title}</span>}
          {contact.organisation && <span>{contact.organisation}</span>}
          {contact.visibility === 'private' && <span className="private-flag">Only you</span>}
        </p>

        {contact.phones.length > 0 && (
          <section>
            <h2>Phone</h2>
            <ul className="contact-lines">
              {contact.phones.map((phone) => (
                <li key={phone.number}>
                  <a href={`tel:${phone.number.replace(/\s/g, '')}`}>{phone.number}</a>
                  {phone.label && <span className="muted"> {phone.label}</span>}
                </li>
              ))}
            </ul>
          </section>
        )}

        {contact.emails.length > 0 && (
          <section>
            <h2>Email</h2>
            <ul className="contact-lines">
              {contact.emails.map((email) => (
                <li key={email.address}>
                  <a href={`mailto:${email.address}`}>{email.address}</a>
                  {email.label && <span className="muted"> {email.label}</span>}
                </li>
              ))}
            </ul>
          </section>
        )}

        {contact.addresses.length > 0 && (
          <section>
            <h2>Address</h2>
            {contact.addresses.map((address, index) => (
              <address key={index}>
                {address.label && <span className="muted">{address.label}</span>}
                {address.street && <span>{address.street}</span>}
                {/* "City, ST 62704" on one line, which is how a US address is
                    written and how somebody reads it back to a driver. */}
                {cityLine(address) && <span>{cityLine(address)}</span>}
                {address.country && <span>{address.country}</span>}
              </address>
            ))}
          </section>
        )}

        {contact.website && (
          <section>
            <h2>Website</h2>
            <a href={contact.website} target="_blank" rel="noreferrer noopener">
              {contact.website}
            </a>
          </section>
        )}

        {contact.notes && (
          <section>
            <h2>Notes</h2>
            <p className="contact-notes">{contact.notes}</p>
          </section>
        )}

        {contact.tags.length > 0 && (
          <p className="contact-tags">
            {contact.tags.map((name) => (
              <span key={name} className="badge">
                {name}
              </span>
            ))}
          </p>
        )}
      </article>

      {canWrite && (
        <div className="editor-actions">
          <button type="button" className="button" onClick={onEdit}>
            Edit
          </button>
          {confirming ? (
            <>
              <button type="button" onClick={() => void onDelete()}>
                Delete for good
              </button>
              <button type="button" onClick={() => setConfirming(false)}>
                Keep it
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setConfirming(true)}>
              Delete
            </button>
          )}
        </div>
      )}
    </div>
  )
}

interface RowDraft {
  label: string
  value: string
}

function ContactEditor({
  contact,
  knownTags,
  onCancel,
  onSaved,
}: {
  readonly contact: Contact | null
  readonly knownTags: readonly string[]
  readonly onCancel: () => void
  readonly onSaved: (saved: Contact) => void
}) {
  const [displayName, setDisplayName] = useState(contact?.display_name ?? '')
  const [organisation, setOrganisation] = useState(contact?.organisation ?? '')
  const [jobTitle, setJobTitle] = useState(contact?.job_title ?? '')
  const [website, setWebsite] = useState(contact?.website ?? '')
  const [notes, setNotes] = useState(contact?.notes ?? '')
  const [visibility, setVisibility] = useState<Visibility>(contact?.visibility ?? 'household')
  const [tagText, setTagText] = useState((contact?.tags ?? []).join(', '))
  const [phones, setPhones] = useState<RowDraft[]>(
    (contact?.phones ?? []).map((row) => ({ label: row.label ?? '', value: row.number })),
  )
  const [emails, setEmails] = useState<RowDraft[]>(
    (contact?.emails ?? []).map((row) => ({ label: row.label ?? '', value: row.address })),
  )
  // The column names are vCard's own — `locality`, `region`, `postal code` are
  // what RFC 6350 calls them, and keeping them is what makes the import and
  // export mapping obvious. What a person reads is Street Address, City, State
  // and Zip.
  const [street, setStreet] = useState(contact?.addresses[0]?.street ?? '')
  const [city, setCity] = useState(contact?.addresses[0]?.locality ?? '')
  const [state, setState] = useState(contact?.addresses[0]?.region ?? '')
  const [zip, setZip] = useState(contact?.addresses[0]?.postcode ?? '')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const name = displayName.trim()
    if (!name) return

    const address = {
      street: street.trim(),
      locality: city.trim(),
      region: state.trim(),
      postcode: zip.trim(),
    }
    const body = {
      display_name: name,
      // Empty means "no value", and the server stores null rather than "".
      organisation: organisation.trim() || null,
      job_title: jobTitle.trim() || null,
      website: website.trim() || null,
      notes: notes.trim(),
      visibility,
      tags: tagText
        .split(',')
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean),
      phones: phones
        .filter((row) => row.value.trim())
        .map((row) => ({ label: row.label.trim() || null, number: row.value.trim() })),
      emails: emails
        .filter((row) => row.value.trim())
        .map((row) => ({ label: row.label.trim() || null, address: row.value.trim() })),
      addresses: Object.values(address).some(Boolean) ? [address] : [],
    }

    setSaving(true)
    const result = contact ? await updateContact(contact.id, body) : await createContact(body)
    setSaving(false)

    if (result.ok) onSaved(result.data)
    else setError(errorMessage(result.data, 'Could not save that contact.'))
  }

  return (
    <Modal
      title={contact ? `Edit ${contact.display_name}` : 'New contact'}
      onClose={onCancel}
      wide
      labelledBy="contact-editor-title"
    >
      <form className="note-editor" onSubmit={save}>
        <div className="field">
          <label htmlFor="contact-name">Name</label>
          <input
            id="contact-name"
            value={displayName}
            onChange={(event) => setDisplayName(event.currentTarget.value)}
            placeholder="Springfield Plumbing"
            autoFocus
            required
          />
        </div>

        <div className="editor-row">
          <div className="field">
            <label htmlFor="contact-org">Organisation</label>
            <input
              id="contact-org"
              value={organisation}
              onChange={(event) => setOrganisation(event.currentTarget.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="contact-title">What they do</label>
            <input
              id="contact-title"
              value={jobTitle}
              onChange={(event) => setJobTitle(event.currentTarget.value)}
              placeholder="Plumber"
            />
          </div>
        </div>

        <RowEditor
          legend="Phone numbers"
          rows={phones}
          onChange={setPhones}
          placeholder="(555) 123-4567"
          labelPlaceholder="Office"
          inputMode="tel"
        />

        <RowEditor
          legend="Email"
          rows={emails}
          onChange={setEmails}
          placeholder="hello@example.com"
          labelPlaceholder="Work"
          inputMode="email"
        />

        <fieldset className="field">
          <legend>Address</legend>
          {/* Street on its own line, then City / State / Zip across — the way a
              US address is written and therefore the way it is read back. */}
          <div className="field">
            <label htmlFor="contact-street">Street Address</label>
            <input
              id="contact-street"
              value={street}
              onChange={(event) => setStreet(event.currentTarget.value)}
              placeholder="1247 Maple Avenue"
              autoComplete="address-line1"
            />
          </div>
          <div className="editor-row address-row">
            <div className="field">
              <label htmlFor="contact-city">City</label>
              <input
                id="contact-city"
                value={city}
                onChange={(event) => setCity(event.currentTarget.value)}
                placeholder="Springfield"
                autoComplete="address-level2"
              />
            </div>
            <div className="field">
              <label htmlFor="contact-state">State</label>
              <input
                id="contact-state"
                value={state}
                onChange={(event) => setState(event.currentTarget.value)}
                placeholder="IL"
                autoComplete="address-level1"
                maxLength={100}
              />
            </div>
            <div className="field">
              <label htmlFor="contact-zip">Zip</label>
              <input
                id="contact-zip"
                value={zip}
                onChange={(event) => setZip(event.currentTarget.value)}
                placeholder="62704"
                autoComplete="postal-code"
                inputMode="numeric"
              />
            </div>
          </div>
        </fieldset>

        <div className="editor-row">
          <div className="field">
            <label htmlFor="contact-website">Website</label>
            <input
              id="contact-website"
              value={website}
              onChange={(event) => setWebsite(event.currentTarget.value)}
              placeholder="https://…"
            />
          </div>
          <div className="field">
            <label htmlFor="contact-tags">Tags</label>
            <input
              id="contact-tags"
              value={tagText}
              onChange={(event) => setTagText(event.currentTarget.value)}
              list="known-contact-tags"
              placeholder="plumber, emergency"
            />
            {/* Offering what exists is the only thing standing between free
                tags and "Plumber", "plumber" and "Plumbers". */}
            <datalist id="known-contact-tags">
              {knownTags.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </div>
        </div>

        <div className="field">
          <label htmlFor="contact-notes">Notes</label>
          <textarea
            id="contact-notes"
            rows={4}
            value={notes}
            onChange={(event) => setNotes(event.currentTarget.value)}
            placeholder="Call before 10am. Ask for Dave."
          />
        </div>

        <div className="field">
          <label htmlFor="contact-visibility">Who can see it</label>
          <select
            id="contact-visibility"
            value={visibility}
            onChange={(event) => setVisibility(event.currentTarget.value as Visibility)}
          >
            <option value="household">Everyone in the household</option>
            <option value="private">Only me</option>
          </select>
        </div>

        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}

        <div className="editor-actions">
          <button type="submit" className="button" disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}

/** A repeating label-and-value list — phones, emails. */
function RowEditor({
  legend,
  rows,
  onChange,
  placeholder,
  labelPlaceholder,
  inputMode,
}: {
  readonly legend: string
  readonly rows: readonly RowDraft[]
  readonly onChange: (rows: RowDraft[]) => void
  readonly placeholder: string
  readonly labelPlaceholder: string
  readonly inputMode: 'tel' | 'email'
}) {
  const update = (index: number, patch: Partial<RowDraft>) =>
    onChange(rows.map((row, position) => (position === index ? { ...row, ...patch } : row)))

  return (
    <fieldset className="field">
      <legend>{legend}</legend>
      {/* Its own class, not the kitchen's `.ingredient-rows`.
          It borrowed that one and inherited a grid written for a recipe:
          `5rem 11rem …`, where the first column is a *quantity*. A phone
          number landed in it and showed about four digits. The two lists look
          alike and are not the same shape, which is exactly the reuse trap the
          notes on form primitives warn about. */}
      <ul className="contact-rows">
        {rows.map((row, index) => (
          <li key={index}>
            <input
              value={row.value}
              onChange={(event) => update(index, { value: event.currentTarget.value })}
              placeholder={placeholder}
              inputMode={inputMode}
              aria-label={`${legend} ${index + 1}`}
            />
            <input
              value={row.label}
              onChange={(event) => update(index, { label: event.currentTarget.value })}
              placeholder={labelPlaceholder}
              aria-label={`Label for ${legend.toLowerCase()} ${index + 1}`}
            />
            <span className="row-actions">
              <button
                type="button"
                onClick={() => onChange(rows.filter((_, position) => position !== index))}
                aria-label={`Remove ${legend.toLowerCase()} ${index + 1}`}
              >
                ×
              </button>
            </span>
          </li>
        ))}
      </ul>
      <button type="button" onClick={() => onChange([...rows, { label: '', value: '' }])}>
        Add {legend.toLowerCase()}
      </button>
    </fieldset>
  )
}

/**
 * Reading a vCard file in.
 *
 * Previews first, always. It is a bulk write over records somebody already has,
 * and the same code path runs both halves on the server so the preview cannot
 * disagree with the import.
 */
function VCardImport({
  onClose,
  onImported,
}: {
  readonly onClose: () => void
  readonly onImported: () => Promise<void>
}) {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<ContactImportResult | null>(null)
  const [onConflict, setOnConflict] = useState<'skip' | 'replace'>('skip')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(isPreview: boolean) {
    if (!file) return
    setBusy(true)
    setError(null)
    const result = await importContacts(file, { preview: isPreview, onConflict })
    setBusy(false)

    if (!result.ok) {
      setError(errorMessage(result.data, 'Could not read that file.'))
      return
    }
    if (isPreview) setPreview(result.data)
    else await onImported()
  }

  return (
    <div className="list-editor" role="dialog" aria-label="Import contacts from a vCard file">
      <div className="field">
        <label htmlFor="vcf">A .vcf file exported from a phone or mail client</label>
        <input
          id="vcf"
          type="file"
          accept=".vcf,text/vcard,text/x-vcard"
          onChange={(event) => {
            setFile(event.currentTarget.files?.[0] ?? null)
            setPreview(null)
          }}
        />
      </div>

      <div className="field">
        <label htmlFor="vcf-conflict">If somebody is already here</label>
        <select
          id="vcf-conflict"
          value={onConflict}
          onChange={(event) => {
            setOnConflict(event.currentTarget.value as 'skip' | 'replace')
            setPreview(null)
          }}
        >
          <option value="skip">Keep what is here</option>
          <option value="replace">Replace with the file</option>
        </select>
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      {preview && (
        <div className="notice" role="status">
          <p>
            <strong>{preview.found}</strong> contacts in that file: {preview.imported} to add,{' '}
            {preview.replaced} to replace, {preview.skipped_existing} already here.
            {preview.unreadable > 0 && ` ${preview.unreadable} could not be read.`}
          </p>
          {preview.conflicts.length > 0 && (
            <p className="muted">
              Already here: {preview.conflicts.join(', ')}
              {preview.conflict_count > preview.conflicts.length &&
                ` and ${preview.conflict_count - preview.conflicts.length} more`}
              .
            </p>
          )}
        </div>
      )}

      <div className="editor-actions">
        <button type="button" onClick={() => void run(true)} disabled={!file || busy}>
          {busy ? 'Reading…' : 'Check the file'}
        </button>
        <button
          type="button"
          className="button"
          onClick={() => void run(false)}
          disabled={!file || busy || preview === null}
        >
          Import
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  )
}
