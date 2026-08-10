// The notes board (SPEC §4.5): markdown with a rendered reader view and an edit
// toggle, pinning, tags, full-text search and a per-author filter.

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  createNote,
  deleteNote,
  listNoteTags,
  listNotes,
  listUsers,
  reorderNotes,
  updateNote,
  type CurrentUser,
  type HouseholdMember,
  type Note,
} from '../api/client'
import { MemberChip } from '../components/MemberMark'
import { Icon } from '../components/icons'
import { Modal } from '../components/Modal'
import { previewOf, renderMarkdown } from '../lib/markdown'
import { hueVar } from '../lib/members'
import { moveEarlier, moveLater, reorder } from '../lib/reorder'

export function NotesPage({ me }: { readonly me: CurrentUser }) {
  const [notes, setNotes] = useState<Note[] | null>(null)
  const [members, setMembers] = useState<HouseholdMember[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [tag, setTag] = useState('')
  const [author, setAuthor] = useState('')
  const [editing, setEditing] = useState<Note | 'new' | null>(null)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)

  const canWrite = me.permissions['notes'] === 'write'

  const refresh = useCallback(async () => {
    const [noteResult, memberResult, tagResult] = await Promise.all([
      listNotes({
        search: search || undefined,
        tag: tag || undefined,
        author_id: author || undefined,
      }),
      listUsers(),
      listNoteTags(),
    ])
    if (noteResult.ok) setNotes(noteResult.data)
    if (memberResult.ok) setMembers(memberResult.data)
    if (tagResult.ok) setTags(tagResult.data)
  }, [search, tag, author])

  useEffect(() => {
    // Debounced so typing in the search box does not fire a request per keystroke.
    const timer = setTimeout(() => void refresh(), 200)
    return () => clearTimeout(timer)
  }, [refresh])

  const byId = useMemo(() => new Map(members.map((m) => [m.id, m])), [members])

  async function togglePin(note: Note) {
    await updateNote(note.id, { is_pinned: !note.is_pinned })
    await refresh()
  }

  async function remove(note: Note) {
    await deleteNote(note.id)
    setEditing(null)
    await refresh()
  }

  // Reordering only makes sense on the unfiltered board. While searching, the
  // order is relevance, and dragging a card would either lie about what it did
  // or silently reorder notes that are not on screen.
  const canReorder = canWrite && !search && !tag && !author

  /** Apply a new order optimistically, then persist it. */
  async function applyOrder(next: Note[]) {
    setNotes(next)
    const result = await reorderNotes(next.map((note) => note.id))
    // On failure, go back to whatever the server actually has rather than
    // leaving the board showing an order that was never saved.
    if (!result.ok) await refresh()
  }

  /**
   * Where the dragged note started, taken from the drop event rather than from
   * React state. `dataTransfer` carries the id across the drag, so this works
   * even when the drop lands before a re-render has told the component that a
   * drag is in progress. `dragIndex` stays as the fallback and drives the
   * visual affordances.
   */
  function handleDrop(event: React.DragEvent, to: number) {
    const draggedId = event.dataTransfer.getData('text/plain')
    const from =
      notes?.findIndex((note) => note.id === draggedId) ?? -1
    const source = from >= 0 ? from : dragIndex

    setDragIndex(null)
    setOverIndex(null)
    if (source === null || source < 0 || notes === null || source === to) return
    void applyOrder(reorder(notes, source, to))
  }

  function nudge(index: number, direction: 'earlier' | 'later') {
    if (notes === null) return
    const next = direction === 'earlier' ? moveEarlier(notes, index) : moveLater(notes, index)
    if (next !== notes) void applyOrder(next)
  }

  if (notes === null) return <p className="loading">Loading notes…</p>

  return (
    <div className="page">
      <div className="page-head">
        <h1>Notes</h1>
        <p className="page-summary">
          <strong>{notes.length}</strong> {notes.length === 1 ? 'note' : 'notes'}
          {(search || tag || author) && ' matching'}
        </p>
      </div>

      <div className="toolbar">
        <input
          type="search"
          className="search"
          placeholder="Search notes…"
          aria-label="Search notes"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
        />

        <select value={tag} onChange={(e) => setTag(e.currentTarget.value)} aria-label="Filter by tag">
          <option value="">All tags</option>
          {tags.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        <select
          value={author}
          onChange={(e) => setAuthor(e.currentTarget.value)}
          aria-label="Filter by author"
        >
          <option value="">Everyone</option>
          {members.map((member) => (
            <option key={member.id} value={member.id}>
              {member.display_name}
            </option>
          ))}
        </select>

        {canWrite && (
          <button type="button" className="button" onClick={() => setEditing('new')}>
            New note
          </button>
        )}
      </div>

      {editing && (
        <NoteEditor
          note={editing === 'new' ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await refresh()
          }}
        />
      )}

      {notes.length === 0 ? (
        <p className="empty">
          {search || tag || author ? 'Nothing matches those filters.' : 'No notes yet.'}
        </p>
      ) : (
        <>
          {canReorder && (
            <p className="reorder-hint">
              Drag a note to reposition it, or use the arrows on each card. The
              order is shared with the household.
            </p>
          )}
          <ul className="note-grid">
            {notes.map((note, index) => (
              <NoteCard
                key={note.id}
                note={note}
                index={index}
                total={notes.length}
                author={byId.get(note.owner_id)}
                canWrite={canWrite && (note.owner_id === me.id || me.role === 'admin')}
                canReorder={canReorder}
                isDragging={dragIndex === index}
                isOver={overIndex === index && dragIndex !== null && dragIndex !== index}
                onDragStart={() => setDragIndex(index)}
                onDragEnter={() => setOverIndex(index)}
                onDragEnd={() => {
                  setDragIndex(null)
                  setOverIndex(null)
                }}
                onDrop={(event) => handleDrop(event, index)}
                onNudge={(direction) => nudge(index, direction)}
                onPin={() => void togglePin(note)}
                onEdit={() => setEditing(note)}
                onDelete={() => void remove(note)}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function NoteCard({
  note,
  index,
  total,
  author,
  canWrite,
  canReorder,
  isDragging,
  isOver,
  onDragStart,
  onDragEnter,
  onDragEnd,
  onDrop,
  onNudge,
  onPin,
  onEdit,
  onDelete,
}: {
  readonly note: Note
  readonly index: number
  readonly total: number
  readonly author: HouseholdMember | undefined
  readonly canWrite: boolean
  readonly canReorder: boolean
  readonly isDragging: boolean
  readonly isOver: boolean
  readonly onDragStart: () => void
  readonly onDragEnter: () => void
  readonly onDragEnd: () => void
  readonly onDrop: (event: React.DragEvent) => void
  readonly onNudge: (direction: 'earlier' | 'later') => void
  readonly onPin: () => void
  readonly onEdit: () => void
  readonly onDelete: () => void
}) {
  const [reading, setReading] = useState(false)

  return (
    <li
      className="note"
      data-pinned={note.is_pinned}
      data-dragging={isDragging}
      data-over={isOver}
      draggable={canReorder}
      onDragStart={(event) => {
        onDragStart()
        event.dataTransfer.effectAllowed = 'move'
        // Firefox refuses to start a drag with no payload.
        event.dataTransfer.setData('text/plain', note.id)
      }}
      onDragEnter={onDragEnter}
      onDragOver={(event) => {
        // preventDefault is what marks this a valid drop target; without it the
        // browser rejects the drop and the card springs back.
        if (canReorder) event.preventDefault()
      }}
      onDragEnd={onDragEnd}
      onDrop={(event) => {
        event.preventDefault()
        onDrop(event)
      }}
    >
      <span
        className="note-bar"
        style={{ background: hueVar(author?.avatar_color) }}
        aria-hidden="true"
      />

      <div className="note-body">
        <div className="note-head">
          <h2>{note.title}</h2>
          <div className="note-head-actions">
            {canReorder && (
              // The keyboard path. HTML5 drag-and-drop is mouse-only, so
              // without these the feature would simply not exist for anyone
              // navigating by keyboard — SPEC §6 asks for real keyboard
              // navigation, not a pointer-shaped approximation of it.
              <>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => onNudge('earlier')}
                  disabled={index === 0}
                  title="Move earlier"
                >
                  <Icon name="up" />
                  <span className="visually-hidden">
                    Move “{note.title}” earlier
                  </span>
                </button>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => onNudge('later')}
                  disabled={index === total - 1}
                  title="Move later"
                >
                  <Icon name="down" />
                  <span className="visually-hidden">Move “{note.title}” later</span>
                </button>
              </>
            )}
            <button
              type="button"
              className="icon-button"
              aria-pressed={note.is_pinned}
              onClick={onPin}
              title={note.is_pinned ? 'Unpin' : 'Pin'}
            >
              <Icon name="pin" />
              <span className="visually-hidden">
                {note.is_pinned ? 'Unpin' : 'Pin'} “{note.title}”
              </span>
            </button>
          </div>
        </div>

        {reading ? (
          // The rendered reader view. The HTML has been through DOMPurify in
          // lib/markdown.ts — nothing from the server is trusted as markup.
          <div
            className="markdown"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderMarkdown(note.body) }}
          />
        ) : (
          <p className="note-preview">{previewOf(note.body) || <span className="muted">Empty</span>}</p>
        )}

        <div className="note-meta">
          {author && <MemberChip member={author} />}
          {note.tags.map((name) => (
            <span key={name} className="badge">
              {name}
            </span>
          ))}
          {note.visibility === 'private' && (
            <span className="badge" data-tone="accent">
              private
            </span>
          )}
        </div>

        <div className="note-actions">
          <button type="button" onClick={() => setReading((value) => !value)}>
            {reading ? 'Collapse' : 'Read'}
          </button>
          {canWrite && (
            <>
              <button type="button" onClick={onEdit}>
                Edit
              </button>
              <button type="button" onClick={onDelete}>
                Delete
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  )
}

function NoteEditor({
  note,
  onCancel,
  onSaved,
}: {
  readonly note: Note | null
  readonly onCancel: () => void
  readonly onSaved: () => void
}) {
  const [body, setBody] = useState(note?.body ?? '')
  const [preview, setPreview] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const payload = {
      title: String(data.get('title') ?? '').trim(),
      body,
      visibility: String(data.get('visibility') ?? 'household') as Note['visibility'],
      tags: String(data.get('tags') ?? '')
        .split(',')
        .map((value) => value.trim())
        .filter(Boolean),
    }

    setBusy(true)
    const result = note ? await updateNote(note.id, payload) : await createNote(payload)
    setBusy(false)

    if (result.ok) onSaved()
    else setError('Could not save that note.')
  }

  return (
    <Modal
      title={note ? 'Edit note' : 'New note'}
      onClose={onCancel}
      wide
      labelledBy="note-editor-title"
    >
      <form className="note-editor" onSubmit={save}>
      <div className="field">
        <label htmlFor="note-title">Title</label>
        <input id="note-title" name="title" defaultValue={note?.title ?? ''} required autoFocus />
      </div>

      <div className="field">
        <div className="editor-head">
          <label htmlFor="note-body">Body — markdown</label>
          <div className="segmented" role="group" aria-label="Editor mode">
            <button type="button" aria-pressed={!preview} onClick={() => setPreview(false)}>
              Write
            </button>
            <button type="button" aria-pressed={preview} onClick={() => setPreview(true)}>
              Preview
            </button>
          </div>
        </div>

        {preview ? (
          <div
            className="markdown editor-preview"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderMarkdown(body) }}
          />
        ) : (
          <textarea
            id="note-body"
            name="body"
            rows={10}
            value={body}
            onChange={(event) => setBody(event.currentTarget.value)}
            placeholder="# Heading&#10;&#10;Anything markdown."
          />
        )}
      </div>

      <div className="editor-row">
        <div className="field">
          <label htmlFor="note-tags">Tags — comma separated</label>
          <input id="note-tags" name="tags" defaultValue={note?.tags.join(', ') ?? ''} />
        </div>

        <div className="field">
          <label htmlFor="note-visibility">Who can see this</label>
          <select
            id="note-visibility"
            name="visibility"
            defaultValue={note?.visibility ?? 'household'}
          >
            <option value="household">Everyone in the household</option>
            <option value="private">Only me</option>
          </select>
        </div>
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      <div className="editor-actions">
        <button type="submit" className="button" disabled={busy}>
          {busy ? 'Saving…' : 'Save note'}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      </form>
    </Modal>
  )
}
