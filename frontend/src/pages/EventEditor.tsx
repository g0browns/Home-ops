// Creating and editing events (SPEC §4.3).
//
// The scope selector is the whole point. Editing one occurrence of a repeating
// event is ambiguous by nature — "move swimming to 7pm" could mean this week,
// every week from now on, or every week ever — and guessing is how a household
// arrives at a calendar nobody trusts. So when an event repeats, the choice is
// made explicit and nothing is saved until it has been.

import { useState, type CSSProperties, type FormEvent } from 'react'

import {
  createEvent,
  deleteEvent,
  updateEvent,
  type CalendarSummary,
  type EditScope,
  type HouseholdMember,
  type Occurrence,
  type Visibility,
} from '../api/client'
import { DayRangePicker } from '../components/DayRangePicker'
import { MemberMark } from '../components/MemberMark'
import { Modal } from '../components/Modal'
import { calendarHueVar } from '../lib/calendars'
import { formatTime, isoDate, type WeekStart } from '../lib/dates'
import {
  clockOf,
  dayCount,
  instantsFor,
  rangeLabel,
  spansMultipleDays,
  type DayRange,
} from '../lib/dayRange'

export type EditingEvent =
  | { readonly mode: 'new'; readonly start: Date }
  | {
      readonly mode: 'existing'
      readonly occurrence: Occurrence
      /** Open with the delete confirmation already showing.
       *
       *  Set by the calendar's right-click menu. Delete is offered there, but the
       *  *deciding* stays here, because for a repeating event "delete" is not one
       *  act: this occurrence, this and everything after, or the whole series.
       *  The scope picker is the only place that question is asked, and a second
       *  copy of it in a menu is a second place to get it wrong. */
      readonly intent?: 'delete'
    }

const SCOPES: { value: EditScope; label: string; hint: string }[] = [
  { value: 'this', label: 'This event only', hint: 'Leaves the rest of the series alone' },
  {
    value: 'this_and_following',
    label: 'This and all later ones',
    hint: 'Splits the series here',
  },
  { value: 'all', label: 'The whole series', hint: 'Every occurrence, past and future' },
]

const VISIBILITIES: { value: Visibility; label: string }[] = [
  { value: 'household', label: 'Everyone in the household' },
  { value: 'assignees', label: 'Only me and the people it is for' },
  { value: 'private', label: 'Only me' },
]

const REPEATS: { value: string; label: string }[] = [
  { value: '', label: 'Does not repeat' },
  { value: 'FREQ=DAILY', label: 'Daily' },
  { value: 'FREQ=WEEKLY', label: 'Weekly' },
  { value: 'FREQ=MONTHLY', label: 'Monthly' },
  { value: 'FREQ=YEARLY', label: 'Yearly' },
]

/**
 * The scope, as the middle of a sentence about deleting.
 *
 * Written out rather than reusing the picker's own labels: "This event only" is a
 * fine thing to choose and a poor thing to be asked ("Delete This event only of
 * Swimming?"). A confirmation has to read like a question.
 */
function scopeSentence(scope: EditScope): string {
  if (scope === 'this') return 'this one occurrence'
  if (scope === 'this_and_following') return 'this one and every later occurrence'
  return 'every occurrence'
}

/** The browser's own zone. An IANA name — never an offset, which cannot carry DST. */
function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

function calendarHueOf(calendars: readonly CalendarSummary[], id: string): string {
  return calendarHueVar(calendars.find((calendar) => calendar.id === id)?.color_key)
}

export function EventEditor({
  editing,
  members,
  calendars,
  dayEvents = [],
  weekStart,
  onCancel,
  onSaved,
}: {
  readonly editing: EditingEvent
  readonly members: readonly HouseholdMember[]
  readonly calendars: readonly CalendarSummary[]
  /** Everything already on the day being edited, so the form does not hide it. */
  readonly dayEvents?: readonly Occurrence[]
  /** The household's first day of the week, so the picker's columns match the
      month grid the event was clicked on. */
  readonly weekStart: WeekStart
  readonly onCancel: () => void
  readonly onSaved: () => void
}) {
  const existing = editing.mode === 'existing' ? editing.occurrence : null
  const repeats = existing?.is_recurring ?? false

  const [assignees, setAssignees] = useState<string[]>(existing?.assignee_ids ?? [])
  const [calendarId, setCalendarId] = useState<string>(
    existing?.calendar_id ?? calendars.find((c) => c.is_default)?.id ?? '',
  )
  const [scope, setScope] = useState<EditScope>(repeats ? 'this' : 'all')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  //: Delete asked for, not yet done.
  //:
  //: **There was no confirmation here at all** until 2026-08-10 — the button
  //: called `remove()` on the first click. That was survivable while Delete was
  //: buried in a form somebody had deliberately opened; it is not once a
  //: right-click menu offers it two pixels from Edit. Opening straight into this
  //: state is what that menu item does.
  const [confirmingDelete, setConfirmingDelete] = useState(
    editing.mode === 'existing' && editing.intent === 'delete',
  )

  const start =
    editing.mode === 'existing' ? new Date(editing.occurrence.starts_at) : editing.start
  const end =
    editing.mode === 'existing'
      ? new Date(editing.occurrence.ends_at)
      : new Date(editing.start.getTime() + 60 * 60 * 1000)

  //: The day or days this covers, inclusive at both ends, replacing the pair of
  //: `datetime-local` fields that used to make a one-day event a two-field job.
  //:
  //: **Reading an existing all-day event back needs the millisecond.** An all-day
  //: event stores an *exclusive* end — one day on the 3rd ends at 00:00 on the
  //: 4th — so taking the end date as written makes every single-day all-day event
  //: look like a two-day range. `lib/occurrences.ts` has the same subtraction for
  //: the same reason.
  const [range, setRange] = useState<DayRange>(() => ({
    from: isoDate(start),
    to: isoDate(
      existing?.is_all_day ? new Date(end.getTime() - 1) : end,
    ),
  }))
  const [allDay, setAllDay] = useState(existing?.is_all_day ?? false)
  const [fromTime, setFromTime] = useState(() => clockOf(start))
  const [toTime, setToTime] = useState(() => clockOf(end))

  const multiDay = spansMultipleDays(range)

  function toggleAssignee(id: string) {
    setAssignees((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    )
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)

    const when = instantsFor(range, { allDay, fromTime, toTime })
    const body: Record<string, unknown> = {
      title: String(data.get('title') ?? '').trim(),
      description: String(data.get('description') ?? '') || null,
      location: String(data.get('location') ?? '') || null,
      starts_at: when.startsAt,
      ends_at: when.endsAt,
      is_all_day: when.isAllDay,
      visibility: String(data.get('visibility') ?? 'household'),
      assignee_ids: assignees,
      ...(calendarId ? { calendar_id: calendarId } : {}),
    }

    setBusy(true)
    setError(null)

    let result
    if (existing) {
      const rule = String(data.get('recurrence_rule') ?? '')
      result = await updateEvent(existing.event_id, {
        ...body,
        scope,
        // Which occurrence the edit was made from. The server needs it for any
        // scope other than the whole series.
        original_start: existing.original_start,
        ...(rule ? { recurrence_rule: rule } : {}),
      })
    } else {
      result = await createEvent({
        ...body,
        tzid: localZone(),
        recurrence_rule: String(data.get('recurrence_rule') ?? '') || null,
      })
    }

    setBusy(false)
    if (result.ok) onSaved()
    else setError('Could not save that event.')
  }

  async function remove() {
    if (!existing) return
    setBusy(true)
    const result = await deleteEvent(existing.event_id, {
      scope,
      original_start: existing.original_start,
    })
    setBusy(false)
    if (result.ok) onSaved()
    else setError('Could not delete that event.')
  }

  const heading = existing ? 'Edit event' : 'New event'
  const onDay = editing.mode === 'existing' ? new Date(editing.occurrence.starts_at) : editing.start

  return (
    <Modal title={heading} onClose={onCancel} wide labelledBy="event-editor-title">
      {/* The day's other events, beside the form. Deciding when something goes
          means knowing what is already there, and a form that hides the day it
          is editing makes you close it to find out. */}
      {dayEvents.length > 0 && (
        <section className="editor-day">
          <h3>
            Already on {onDay.toLocaleDateString(undefined, {
              weekday: 'long',
              month: 'long',
              day: 'numeric',
            })}
          </h3>
          <ul className="day-events">
            {dayEvents.map((other) => (
              <li
                key={`${other.event_id}-${other.original_start}`}
                data-current={existing?.event_id === other.event_id}
              >
                <span className="tabular agenda-time">
                  {other.is_all_day ? 'All day' : formatTime(new Date(other.starts_at))}
                </span>
                <span className="agenda-title">{other.title}</span>
                {existing?.event_id === other.event_id && (
                  <span className="muted">the one you are editing</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <form className="note-editor" onSubmit={save} aria-label={heading}>
        {existing?.recurrence_label && (
          <p className="editor-head">
            <span className="badge" data-tone="accent">
              repeats {existing.recurrence_label.toLowerCase()}
            </span>
          </p>
        )}

      {repeats && (
        <fieldset className="field scope-picker">
          <legend>This change applies to</legend>
          <div className="scope-options">
            {SCOPES.map((option) => (
              <label key={option.value} className="scope-option" data-chosen={scope === option.value}>
                <input
                  type="radio"
                  name="scope"
                  value={option.value}
                  checked={scope === option.value}
                  onChange={() => setScope(option.value)}
                />
                <span>
                  <strong>{option.label}</strong>
                  <small>{option.hint}</small>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <div className="editor-row">
        <div className="field">
          <label htmlFor="event-title">Title</label>
          <input
            id="event-title"
            name="title"
            defaultValue={existing?.title ?? ''}
            required
            autoFocus
          />
        </div>

        {calendars.length > 0 && (
          <div className="field">
            <label htmlFor="event-calendar">Calendar</label>
            <select
              id="event-calendar"
              value={calendarId}
              onChange={(event) => setCalendarId(event.currentTarget.value)}
              // The swatch on the control itself, so the colour the event will
              // take on the grid is visible while choosing rather than after.
              style={{ '--event-hue': calendarHueOf(calendars, calendarId) } as CSSProperties}
              className="with-swatch"
            >
              {calendars.map((calendar) => (
                <option key={calendar.id} value={calendar.id}>
                  {calendar.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div className="editor-row">
        <div className="field">
          <span className="field-label" id="event-when-label">
            When
          </span>
          <DayRangePicker
            range={range}
            weekStart={weekStart}
            labelledBy="event-when-label"
            onChange={setRange}
          />
        </div>

        <div className="field">
          <p className="when-summary">
            <strong>{rangeLabel(range)}</strong>
            {multiDay && <span className="badge">{dayCount(range)} days · all day</span>}
          </p>

          {/* Times only when there is one day to put them on. A range is all-day
              by the household's rule, and offering a start time for it would be
              offering something that gets thrown away. */}
          {multiDay ? (
            <p className="field-hint">
              More than one day, so this is an all-day event. Pick a single day if
              you want to give it a time.
            </p>
          ) : (
            <>
              <label className="check">
                <input
                  type="checkbox"
                  checked={allDay}
                  onChange={(event) => setAllDay(event.currentTarget.checked)}
                />
                All day
              </label>

              {!allDay && (
                <div className="editor-row">
                  <div className="field">
                    <label htmlFor="event-from">From</label>
                    <input
                      id="event-from"
                      type="time"
                      value={fromTime}
                      onChange={(event) => setFromTime(event.currentTarget.value)}
                      required
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="event-to">To</label>
                    <input
                      id="event-to"
                      type="time"
                      value={toTime}
                      onChange={(event) => setToTime(event.currentTarget.value)}
                      required
                    />
                  </div>
                </div>
              )}

              {/* The one way to express an overnight event now that there is a
                  single date. Said out loud, because an end before a start looks
                  like a mistake unless the form tells you it is not. */}
              {!allDay && toTime <= fromTime && (
                <p className="field-hint">
                  Ends the next morning, {toTime} on{' '}
                  {new Date(`${range.to}T00:00:00`).toLocaleDateString(undefined, {
                    weekday: 'long',
                  })}
                  &rsquo;s following day.
                </p>
              )}
            </>
          )}
        </div>

        <div className="field">
          <label htmlFor="event-repeat">Repeats</label>
          <select
            id="event-repeat"
            name="recurrence_rule"
            defaultValue={existing?.is_recurring ? 'FREQ=WEEKLY' : ''}
            disabled={Boolean(existing) && scope !== 'all'}
          >
            {REPEATS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {existing && scope !== 'all' && (
            <span className="field-hint">
              Changing how often it repeats applies to the whole series.
            </span>
          )}
        </div>
      </div>

      <div className="field">
        <label htmlFor="event-location">Where</label>
        <input id="event-location" name="location" defaultValue={existing?.location ?? ''} />
      </div>

      <div className="field">
        <label htmlFor="event-description">Notes</label>
        <textarea
          id="event-description"
          name="description"
          rows={3}
          defaultValue={existing?.description ?? ''}
        />
      </div>

      <fieldset className="field assignee-picker">
        <legend>Who it is for</legend>
        <div className="assignee-options">
          {members.map((member) => {
            const chosen = assignees.includes(member.id)
            return (
              <label key={member.id} className="assignee-option" data-chosen={chosen}>
                <input type="checkbox" checked={chosen} onChange={() => toggleAssignee(member.id)} />
                <MemberMark member={member} size="sm" />
                <span>{member.display_name}</span>
              </label>
            )
          })}
        </div>
      </fieldset>

      <div className="field">
        <label htmlFor="event-visibility">Who can see this</label>
        <select
          id="event-visibility"
          name="visibility"
          defaultValue={existing?.visibility ?? 'household'}
        >
          {VISIBILITIES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

        {/* The confirmation reads back the scope, because on a repeating event
            that is the whole question. "Delete every occurrence, past and
            future" is a different sentence from "delete this one", and the
            person clicking should see which one they are about to do. */}
        {confirmingDelete && existing && (
          <div className="alert" role="alert">
            <p>
              <strong>
                {repeats
                  ? `Delete ${scopeSentence(scope)} of “${existing.title}”?`
                  : `Delete “${existing.title}”?`}
              </strong>
            </p>
            <p className="field-hint">
              This cannot be undone.
              {repeats && scope !== 'all' && ' The rest of the series stays as it is.'}
              {repeats && scope === 'all' && ' Every occurrence goes, including past ones.'}
            </p>
            <div className="editor-actions">
              <button
                type="button"
                className="button danger"
                onClick={() => void remove()}
                disabled={busy}
              >
                {busy ? 'Deleting…' : 'Yes, delete'}
              </button>
              <button type="button" onClick={() => setConfirmingDelete(false)} disabled={busy}>
                Keep it
              </button>
            </div>
          </div>
        )}

        <div className="editor-actions">
          <button type="submit" className="button" disabled={busy || confirmingDelete}>
            {busy ? 'Saving…' : existing ? 'Save event' : 'Create event'}
          </button>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          {existing && !confirmingDelete && (
            <button type="button" onClick={() => setConfirmingDelete(true)} disabled={busy}>
              Delete{repeats && scope !== 'all' ? ' this one' : ''}
            </button>
          )}
        </div>
      </form>
    </Modal>
  )
}
