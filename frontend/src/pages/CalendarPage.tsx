// The calendar (SPEC §4.3): a month grid and an agenda over the same window.
//
// The server returns *occurrences*, already expanded, so nothing here parses an
// RRULE or reasons about daylight saving. What this page owns is the grid, the
// filters, and asking which occurrences an edit should apply to — the question
// §4.3 calls the hard part.
//
// Colour on this page means *calendar*, not member: an event block is filled
// with its calendar's hue and the people it is for ride on top as initials. See
// lib/calendars.ts for why those are two palettes and not one.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'

import {
  getAgenda,
  getHouseholdSettings,
  listCalendars,
  updateEvent,
  listUsers,
  type AgendaTask,
  type CalendarSummary,
  type CurrentUser,
  type HouseholdMember,
  type Occurrence,
} from '../api/client'
import { MemberMark } from '../components/MemberMark'
import { Modal } from '../components/Modal'
import { calendarHueVar } from '../lib/calendars'
import { coversDay, daysCovered, isMultiDay, rangeLabel } from '../lib/occurrences'
import {
  addDays,
  addMonths,
  formatMonth,
  formatTime,
  isSameDay,
  isSameMonth,
  isWeekStart,
  monthGrid,
  startOfDay,
  weekdayLabels,
  type WeekStart,
} from '../lib/dates'
import { layoutWeek, weeksOf, type LayoutEntry, type Span } from '../lib/monthLayout'
import { ContextMenu } from '../components/ContextMenu'
import { contextMenuPoint } from '../lib/menuPosition'
import { EventEditor, type EditingEvent } from './EventEditor'

/** What a month cell can hold: an appointment, or something merely due. */
type Entry =
  | { readonly kind: 'event'; readonly occurrence: Occurrence }
  | { readonly kind: 'task'; readonly task: AgendaTask }

export function CalendarPage({ me }: { readonly me: CurrentUser }) {
  const [month, setMonth] = useState(() => startOfDay(new Date()))
  const [view, setView] = useState<'month' | 'agenda'>('month')
  const [weekStart, setWeekStart] = useState<WeekStart>('monday')
  const [occurrences, setOccurrences] = useState<Occurrence[]>([])
  const [tasks, setTasks] = useState<AgendaTask[]>([])
  const [members, setMembers] = useState<HouseholdMember[]>([])
  const [calendars, setCalendars] = useState<CalendarSummary[]>([])
  const [mineOnly, setMineOnly] = useState(false)
  const [showTasks, setShowTasks] = useState(true)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<EditingEvent | null>(null)
  //: The day whose modal is open. Clicking a day opens the whole day rather
  //: than the nearest bar, which is the thing people actually want from a month
  //: grid: "what is happening on the 14th".
  const [openDay, setOpenDay] = useState<Date | null>(null)
  //: The right-click menu: which event, and where the pointer was. Held here
  //: rather than per-bar so only one can ever be open, and so the agenda and the
  //: month grid share it instead of growing one each.
  const [menu, setMenu] = useState<{ occurrence: Occurrence; at: { x: number; y: number } } | null>(
    null,
  )
  //: A drag that the server refused. Shown rather than swallowed: a bar that
  //: springs back with no explanation reads as the app being broken.
  const [dragError, setDragError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const canWrite = me.permissions['calendar'] === 'write'

  const grid = useMemo(() => monthGrid(month, weekStart), [month, weekStart])
  const windowStart = grid[0] ?? month
  const windowEnd = useMemo(() => addDays(grid[grid.length - 1] ?? month, 1), [grid, month])

  useEffect(() => {
    let cancelled = false
    void getHouseholdSettings().then((result) => {
      if (cancelled || !result.ok) return
      const value = result.data.values['week_starts_on']
      if (isWeekStart(value)) setWeekStart(value)
    })
    void listUsers().then((result) => {
      if (!cancelled && result.ok) setMembers(result.data)
    })
    void listCalendars().then((result) => {
      if (!cancelled && result.ok) setCalendars(result.data)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    const result = await getAgenda({
      // The grid already covers whole weeks, so the request window is exactly
      // what is on screen — no more, and never the unbounded span the API
      // refuses anyway.
      start: windowStart.toISOString(),
      end: windowEnd.toISOString(),
      assignee_id: mineOnly ? me.id : undefined,
      search: search.trim() || undefined,
      include_tasks: showTasks,
    })
    setLoading(false)
    if (result.ok) {
      setOccurrences(result.data.occurrences)
      setTasks(result.data.tasks)
    }
  }, [windowStart, windowEnd, mineOnly, search, showTasks, me.id])

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 150)
    return () => clearTimeout(timer)
  }, [refresh])

  const memberById = useMemo(() => new Map(members.map((m) => [m.id, m])), [members])
  const hueOf = useMemo(() => {
    const byId = new Map(calendars.map((c) => [c.id, c.color_key]))
    return (calendarId: string) => calendarHueVar(byId.get(calendarId))
  }, [calendars])

  const entries: LayoutEntry<Entry>[] = useMemo(
    () => [
      ...occurrences.map((occurrence) => ({
        item: { kind: 'event' as const, occurrence },
        start: new Date(occurrence.starts_at),
        end: new Date(occurrence.ends_at),
        rank: 0,
      })),
      ...(showTasks ? tasks : []).map((task) => ({
        item: { kind: 'task' as const, task },
        start: new Date(task.due_at),
        end: new Date(task.due_at),
        rank: 1,
      })),
    ],
    [occurrences, tasks, showTasks],
  )

  const today = startOfDay(new Date())

  function openNew(day?: Date) {
    const start = day ? new Date(day) : new Date()
    if (day) start.setHours(9, 0, 0, 0)
    setEditing({ mode: 'new', start })
  }

  const openMenu = useCallback(
    (occurrence: Occurrence, at: { x: number; y: number }) => setMenu({ occurrence, at }),
    [],
  )

  /** Everything on one day, in order, events before deadlines. */
  const eventsOn = useCallback(
    (day: Date) => occurrences.filter((o) => coversDay(o, day)),
    [occurrences],
  )

  /**
   * A bar dragged to a new day, or stretched by one edge.
   *
   * `days` shifts the whole event and keeps its length; `startDays` and
   * `endDays` move one edge. All three go through the ordinary PATCH, so a
   * recurring event is edited with the narrowest scope — dragging one instance
   * of a weekly swimming lesson must not move the whole series, which is the
   * same rule the editor's scope picker exists to enforce.
   */
  async function nudge(
    occurrence: Occurrence,
    { days = 0, startDays = 0, endDays = 0 }: { days?: number; startDays?: number; endDays?: number },
  ) {
    const start = new Date(occurrence.starts_at)
    const end = new Date(occurrence.ends_at)
    start.setDate(start.getDate() + days + startDays)
    end.setDate(end.getDate() + days + endDays)
    if (end.getTime() <= start.getTime()) return

    // A one-off has a single occurrence, so `all` *is* this one — and `this`
    // would be refused, because the narrow scopes require an `original_start`
    // that a non-recurring event does not carry. A repeating event uses the
    // narrowest scope: dragging one week of a swimming lesson must not move
    // the whole series, which is the rule the editor's scope picker exists for.
    const result = await updateEvent(
      occurrence.event_id,
      occurrence.is_recurring
        ? {
            starts_at: start.toISOString(),
            ends_at: end.toISOString(),
            scope: 'this',
            original_start: occurrence.original_start,
          }
        : {
            starts_at: start.toISOString(),
            ends_at: end.toISOString(),
            scope: 'all',
          },
    )
    if (result.ok) await refresh()
    else setDragError('That could not be moved.')
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>{formatMonth(month)}</h1>
        <p className="page-summary">
          <strong>{occurrences.length}</strong>{' '}
          {occurrences.length === 1 ? 'event' : 'events'}
          {showTasks && tasks.length > 0 && (
            <>
              {' '}
              · <strong>{tasks.length}</strong> due
            </>
          )}
        </p>
      </div>

      <div className="toolbar">
        <div className="month-nav" role="group" aria-label="Change month">
          <button type="button" onClick={() => setMonth(addMonths(month, -1))} aria-label="Previous month">
            ‹
          </button>
          <button type="button" onClick={() => setMonth(startOfDay(new Date()))}>
            Today
          </button>
          <button type="button" onClick={() => setMonth(addMonths(month, 1))} aria-label="Next month">
            ›
          </button>
        </div>

        <div className="segmented" role="group" aria-label="View">
          {(['month', 'agenda'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={view === option}
              onClick={() => setView(option)}
            >
              {option === 'month' ? 'Month' : 'Agenda'}
            </button>
          ))}
        </div>

        <input
          type="search"
          className="search"
          placeholder="Search events…"
          aria-label="Search events"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
        />

        <label className="check">
          <input
            type="checkbox"
            checked={mineOnly}
            onChange={(event) => setMineOnly(event.currentTarget.checked)}
          />
          Assigned to me
        </label>

        <label className="check">
          <input
            type="checkbox"
            checked={showTasks}
            onChange={(event) => setShowTasks(event.currentTarget.checked)}
          />
          Show task deadlines
        </label>

        {canWrite && (
          <button type="button" className="button" onClick={() => openNew()}>
            New event
          </button>
        )}
      </div>

      {calendars.length > 1 && <CalendarKey calendars={calendars} />}

      {dragError && (
        <p className="alert" role="alert">
          {dragError}{' '}
          <button type="button" className="link-button" onClick={() => setDragError(null)}>
            Dismiss
          </button>
        </p>
      )}

      {openDay && !editing && (
        <DayModal
          day={openDay}
          occurrences={eventsOn(openDay)}
          tasks={showTasks ? tasks.filter((t) => isSameDay(new Date(t.due_at), openDay)) : []}
          memberById={memberById}
          hueOf={hueOf}
          canWrite={canWrite}
          onClose={() => setOpenDay(null)}
          onOpen={(occurrence) => setEditing({ mode: 'existing', occurrence })}
          onAdd={() => openNew(openDay)}
        />
      )}

      {editing && (
        <EventEditor
          editing={editing}
          members={members}
          calendars={calendars}
          dayEvents={eventsOn(
            editing.mode === 'existing'
              ? new Date(editing.occurrence.starts_at)
              : editing.start,
          )}
          weekStart={weekStart}
          onCancel={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            setOpenDay(null)
            await refresh()
          }}
        />
      )}

      {loading && entries.length === 0 ? (
        <p className="loading">Loading…</p>
      ) : view === 'month' ? (
        <MonthGrid
          grid={grid}
          month={month}
          today={today}
          weekStart={weekStart}
          entries={entries}
          hueOf={hueOf}
          canWrite={canWrite}
          onOpen={(occurrence) => setEditing({ mode: 'existing', occurrence })}
          onOpenMenu={openMenu}
          onAddOn={openNew}
          onOpenDay={setOpenDay}
          onNudge={canWrite ? nudge : undefined}
        />
      ) : (
        <AgendaList
          occurrences={occurrences}
          tasks={showTasks ? tasks : []}
          memberById={memberById}
          hueOf={hueOf}
          onOpen={(occurrence) => setEditing({ mode: 'existing', occurrence })}
          onOpenMenu={openMenu}
        />
      )}

      {menu && (
        <ContextMenu
          at={menu.at}
          label={menu.occurrence.title}
          onClose={() => setMenu(null)}
          items={[
            {
              label: 'Edit',
              onSelect: () => setEditing({ mode: 'existing', occurrence: menu.occurrence }),
            },
            {
              label: 'Delete…',
              danger: true,
              // Opens the editor already asking, rather than deleting from here.
              // The recurrence scope has exactly one home and this is not it:
              // deleting "this one" and deleting the series are different acts,
              // and a menu item cannot tell which was meant.
              onSelect: () =>
                setEditing({ mode: 'existing', occurrence: menu.occurrence, intent: 'delete' }),
            },
          ]}
        />
      )}
    </div>
  )
}

/**
 * What each fill means.
 *
 * Only shown once there is more than one calendar, because with a single one
 * the colour carries no information and a legend for it is noise.
 */
function CalendarKey({ calendars }: { readonly calendars: readonly CalendarSummary[] }) {
  return (
    <ul className="calendar-key" aria-label="Calendars">
      {calendars.map((calendar) => (
        <li key={calendar.id}>
          <span
            className="key-swatch"
            style={{ background: calendarHueVar(calendar.color_key) }}
            aria-hidden="true"
          />
          {calendar.name}
        </li>
      ))}
    </ul>
  )
}

function MonthGrid({
  grid,
  month,
  today,
  weekStart,
  entries,
  hueOf,
  canWrite,
  onOpen,
  onOpenMenu,
  onAddOn,
  onOpenDay,
  onNudge,
}: {
  readonly grid: readonly Date[]
  readonly month: Date
  readonly today: Date
  readonly weekStart: WeekStart
  readonly entries: readonly LayoutEntry<Entry>[]
  readonly hueOf: (calendarId: string) => string
  readonly canWrite: boolean
  readonly onOpen: (occurrence: Occurrence) => void
  readonly onOpenMenu: (occurrence: Occurrence, at: { x: number; y: number }) => void
  readonly onAddOn: (day: Date) => void
  readonly onOpenDay: (day: Date) => void
  readonly onNudge?:
    | ((
        occurrence: Occurrence,
        delta: { days?: number; startDays?: number; endDays?: number },
      ) => Promise<void>)
    | undefined
}) {
  const body = useRef<HTMLDivElement>(null)
  //: Which of the 42 days the pointer is over, so that day can highlight. Held
  //: here rather than in CSS because the background and the day number are
  //: siblings in the same grid, not parent and child — the same reason
  //: `data-today` is set on both.
  const [hovered, setHovered] = useState<number | null>(null)
  //: **The gesture belongs to the whole grid, not to one week row.** It used to
  //: live on the row, which is where a bar physically sits, and that quietly
  //: made the month six separate drop targets: the pointer could leave the row
  //: but the arithmetic could not, so an event could be moved within its week
  //: and nowhere else. A month grid is 42 consecutive days and a drag across it
  //: is a whole number of days like any other.
  const [drag, setDrag] = useState<DragState | null>(null)
  //: A press that has not yet become a drag.
  //:
  //: **This is the fix for a real bug, not a refinement.** Capture used to be
  //: taken on `pointerdown`, and once an element holds pointer capture the
  //: browser dispatches the derived mouse events — `click` and `dblclick` among
  //: them — to the *capturing* element. So pressing a bar sent its own click to
  //: `.month-body`, the bar's handler never ran, and for three days there was no
  //: way to open an event from the month grid at all. Worse, `onGrab` is only
  //: passed when the caller can write, so the people who could edit were exactly
  //: the ones who could not reach the editor.
  //:
  //: Held in a ref rather than state: it is written and read inside one pointer
  //: sequence and must not wait for a render, which is the same trap the
  //: drag-and-drop notes describe for `dataTransfer`.
  const pending = useRef<PendingGrab | null>(null)

  /** Cancels a press that never became anything, and any long-press timer. */
  const clearPending = useCallback(() => {
    if (pending.current?.timer !== undefined) window.clearTimeout(pending.current.timer)
    pending.current = null
  }, [])

  /**
   * Which of the 42 days a point falls on, 0..41.
   *
   * Two measurements rather than one: the row from `clientY`, then the column
   * from `clientX` **within that row**. Rows are not the same height — a week
   * with four overlapping events is taller than an empty one — so a single
   * box-relative ratio down the whole body would drift a row out by the middle
   * of the month.
   */
  const dayIndexAt = useCallback((clientX: number, clientY: number): number => {
    const rows = body.current ? Array.from(body.current.children) : []
    if (rows.length === 0) return 0

    let weekIndex = rows.findIndex((row) => {
      const box = row.getBoundingClientRect()
      return clientY >= box.top && clientY < box.bottom
    })
    if (weekIndex === -1) {
      // Above the first row or below the last. Clamped rather than abandoned:
      // a pointer that strays off the top of the grid mid-drag should land on
      // the first week, not cancel the gesture.
      const first = rows[0]?.getBoundingClientRect()
      weekIndex = first && clientY < first.top ? 0 : rows.length - 1
    }

    const box = rows[weekIndex]?.getBoundingClientRect()
    if (!box || box.width === 0) return weekIndex * 7
    const ratio = (clientX - box.left) / box.width
    return weekIndex * 7 + Math.min(6, Math.max(0, Math.floor(ratio * 7)))
  }, [])

  /** Commit whatever the pointer drag was doing, and stop. */
  function finishDrag(clientX: number, clientY: number) {
    if (drag) {
      // Throws if the capture has already gone — a pointercancel, or the
      // element being replaced mid-gesture. Nothing to do about it either way.
      try {
        body.current?.releasePointerCapture(drag.pointerId)
      } catch {
        // Already released.
      }
    }
    if (!drag || !onNudge) return setDrag(null)
    // An index delta *is* a day delta, because the grid is 42 consecutive days.
    // That is what makes a drag from the first week to the third ordinary
    // rather than a special case.
    const moved = dayIndexAt(clientX, clientY) - drag.fromIndex
    setDrag(null)
    if (moved === 0) return
    if (drag.edge === 'move') void onNudge(drag.occurrence, { days: moved })
    else if (drag.edge === 'start') void onNudge(drag.occurrence, { startDays: moved })
    else void onNudge(drag.occurrence, { endDays: moved })
  }

  /**
   * Turn a press into a drag, once the pointer has actually gone somewhere.
   *
   * Everything `onGrab` used to do on `pointerdown` happens here instead, on the
   * first move past the threshold. That single move is the whole difference
   * between a bar you can double-click and one you cannot.
   */
  function promote(at: { x: number; y: number }) {
    const grab = pending.current
    if (!grab) return
    clearPending()

    try {
      body.current?.setPointerCapture(grab.pointerId)
    } catch {
      // Unsupported or already gone. The handlers on this element still fire for
      // anything inside the grid, which is the case that matters.
    }
    setDrag({
      occurrence: grab.occurrence,
      edge: grab.edge,
      fromIndex: dayIndexAt(grab.x, grab.y),
      overIndex: dayIndexAt(at.x, at.y),
      pointerId: grab.pointerId,
      x: at.x,
      y: at.y,
      offsetX: grab.offsetX,
      offsetY: grab.offsetY,
      width: grab.width,
    })
  }

  return (
    <div className="month">
      <div className="month-head" aria-hidden="true">
        {weekdayLabels(weekStart).map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>

      {/* Pointer events, not HTML5 drag-and-drop: a drag here has to give live
          feedback across a grid the pointer never leaves, and `dragover` cannot
          say which of forty-two days it is over without the same maths anyway.
          Captured on the body rather than on a row or a bar, so the gesture
          survives crossing both. */}
      <div
        ref={body}
        className="month-body"
        // Text selection during a drag makes the whole grid flash blue and the
        // gesture read as a failed highlight rather than a move.
        data-dragging={drag !== null}
        onPointerMove={(event) => {
          const index = dayIndexAt(event.clientX, event.clientY)
          setHovered(index)

          // A pending press becomes a drag here, or stays a click. The threshold
          // is what keeps a click a click: see `pending` above for the bug that
          // taking capture on pointerdown caused.
          const grab = pending.current
          if (grab) {
            const travelled = Math.hypot(event.clientX - grab.x, event.clientY - grab.y)
            if (travelled > DRAG_THRESHOLD_PX) promote({ x: event.clientX, y: event.clientY })
            return
          }

          setDrag((current) =>
            current ? { ...current, overIndex: index, x: event.clientX, y: event.clientY } : current,
          )
        }}
        onPointerLeave={() => setHovered(null)}
        onPointerUp={(event) => {
          // Cleared before the click lands. A press that never moved is a click,
          // and the browser is about to deliver it to the bar itself.
          clearPending()
          if (drag) finishDrag(event.clientX, event.clientY)
        }}
        onPointerCancel={() => {
          clearPending()
          setDrag(null)
        }}
      >
        {weeksOf(grid).map((week, weekIndex) => (
          <MonthWeek
            key={week[0]?.toISOString()}
            week={week}
            weekIndex={weekIndex}
            month={month}
            today={today}
            entries={entries}
            hueOf={hueOf}
            canWrite={canWrite}
            hovered={hovered}
            drag={drag}
            onOpen={onOpen}
            onAddOn={onAddOn}
            onOpenDay={onOpenDay}
            onOpenMenu={onOpenMenu}
            onGrab={
              onNudge
                ? (occurrence, edge, grab) => {
                    // Recorded, not acted on. Nothing is captured and no drag
                    // starts until the pointer moves — which is what leaves the
                    // click and the double-click to reach the bar.
                    pending.current = {
                      occurrence,
                      edge,
                      x: grab.x,
                      y: grab.y,
                      pointerId: grab.pointerId,
                      offsetX: grab.x - grab.rect.left,
                      offsetY: grab.y - grab.rect.top,
                      width: grab.rect.width,
                      // Touch has no `contextmenu` event, so a press that stays
                      // still is how the menu is reached without a mouse. The
                      // timer is cancelled by any move past the threshold, and by
                      // the pointer coming up.
                      //
                      // **Touch only, and that is not tidiness.** Armed for a
                      // mouse as well, somebody who presses a bar, hesitates half
                      // a second and then drags gets a context menu instead of a
                      // drag — the timer has already fired and thrown the pending
                      // grab away. A mouse has right-click and needs none of this.
                      timer:
                        grab.pointerType === 'touch'
                          ? window.setTimeout(() => {
                              const held = pending.current
                              clearPending()
                              if (held) onOpenMenu(held.occurrence, { x: held.x, y: held.y })
                            }, LONG_PRESS_MS)
                          : undefined,
                    }
                  }
                : undefined
            }
          />
        ))}
      </div>

      {/* The bar, lifted.

          Rendered into `document.body` rather than into the grid: a fixed
          element is positioned against the viewport *unless* an ancestor has a
          transform, a filter or containment, any one of which would silently
          re-anchor it and leave the ghost sitting somewhere arbitrary. A portal
          removes the question rather than betting on the ancestors staying as
          they are. */}
      {drag?.edge === 'move' &&
        createPortal(
          <div
            className="drag-ghost"
            aria-hidden="true"
            style={
              {
                left: drag.x - drag.offsetX,
                top: drag.y - drag.offsetY,
                width: drag.width,
                '--event-hue': hueOf(drag.occurrence.calendar_id),
              } as CSSProperties
            }
          >
            <span className="chip-event">
              {!drag.occurrence.is_all_day && (
                <span className="chip-time tabular">
                  {formatTime(new Date(drag.occurrence.starts_at))}
                </span>
              )}
              <span className="chip-title">{drag.occurrence.title}</span>
            </span>
          </div>,
          document.body,
        )}
    </div>
  )
}

/**
 * One row of the grid.
 *
 * A single CSS grid, seven columns wide: the day backgrounds span every row so
 * the banding is unbroken, the day numbers sit on row 1, and each bar occupies
 * the columns it covers on its own lane. That is what lets a Monday-to-Wednesday
 * event be one element rather than three.
 */
function MonthWeek({
  week,
  weekIndex,
  month,
  today,
  entries,
  hueOf,
  canWrite,
  hovered,
  drag,
  onOpen,
  onOpenMenu,
  onAddOn,
  onOpenDay,
  onGrab,
}: {
  readonly week: readonly Date[]
  /** Which row this is, 0..5. The offset that turns a column into one of the
      grid's 42 days, which is the unit the drag works in. */
  readonly weekIndex: number
  readonly month: Date
  readonly today: Date
  readonly entries: readonly LayoutEntry<Entry>[]
  readonly hueOf: (calendarId: string) => string
  readonly canWrite: boolean
  readonly hovered: number | null
  readonly drag: DragState | null
  readonly onOpen: (occurrence: Occurrence) => void
  readonly onOpenMenu: (occurrence: Occurrence, at: { x: number; y: number }) => void
  readonly onAddOn: (day: Date) => void
  readonly onOpenDay: (day: Date) => void
  readonly onGrab?:
    | ((occurrence: Occurrence, edge: 'move' | 'start' | 'end', grab: Grab) => void)
    | undefined
}) {
  const { spans, laneCount } = layoutWeek(week, entries)
  /** This row's column N as one of the grid's 42 days. */
  const dayIndex = (column: number) => weekIndex * 7 + column

  return (
    <div
      className="month-week"
      // The trailing 1fr soaks up any spare height, so the backgrounds spanning
      // `1 / -1` reach the bottom of a sparse week instead of stopping under the
      // last bar.
      // `repeat(0, auto)` is **invalid CSS** — the count has to be a positive
      // integer — so on a week with nothing in it the browser discarded this
      // whole declaration, fell back to auto-placed rows, and centred the date
      // number in the box instead of leaving it at the top. The repeat is only
      // emitted when there is a lane to repeat.
      style={
        {
          gridTemplateRows:
            laneCount > 0 ? `auto repeat(${laneCount}, auto) 1fr` : 'auto 1fr',
        } as CSSProperties
      }
    >
      {week.map((day, column) => (
        <button
          type="button"
          key={`bg-${day.toISOString()}`}
          className="month-cell"
          data-outside={!isSameMonth(day, month)}
          data-today={isSameDay(day, today)}
          data-hovered={hovered === dayIndex(column)}
          // The day it would land on, wherever that is. This is what carries a
          // cross-week drag: the bar cannot be drawn in another row, but the
          // day it is heading for lights up under the pointer.
          data-receiving={drag !== null && drag.overIndex === dayIndex(column)}
          style={{ gridColumn: column + 1, gridRow: '1 / -1' }}
          // The whole day opens the day, which is what somebody looking at a
          // month grid is usually after: "what is happening on the 14th".
          onClick={() => onOpenDay(day)}
          aria-label={`Everything on ${day.toDateString()}`}
        />
      ))}

      {week.map((day, column) => (
        <div
          key={`head-${day.toISOString()}`}
          className="month-cell-head"
          // Carried on the head as well as the background: the two are
          // siblings in the same grid area, not parent and child, so a CSS
          // sibling selector between them cannot reach across the other five
          // backgrounds that sit between.
          data-today={isSameDay(day, today)}
          data-outside={!isSameMonth(day, month)}
          data-hovered={hovered === dayIndex(column)}
          style={{ gridColumn: column + 1, gridRow: 1 }}
        >
          <span className="tabular">{day.getDate()}</span>
          {canWrite && (
            <button
              type="button"
              className="day-add"
              onClick={() => onAddOn(day)}
              title="Add an event"
            >
              +<span className="visually-hidden"> add an event on {day.toDateString()}</span>
            </button>
          )}
        </div>
      ))}

      {spans.map((span) => {
        // While a bar is being dragged it is drawn where it *would* land, not
        // where it still is. Without this the grid does not move until the
        // mouse comes up, and the whole gesture feels like it did nothing.
        const preview = previewColumns(span, drag, weekIndex)
        return (
        <div
          key={keyFor(span)}
          className="month-span"
          data-previewing={preview !== null}
          style={{
            gridColumn: `${(preview?.start ?? span.startColumn) + 1} / ${(preview?.end ?? span.endColumn) + 2}`,
            gridRow: span.lane + 2,
          }}
        >
          {span.item.kind === 'event' ? (
            <EventBar
              span={span}
              occurrence={span.item.occurrence}
              hueOf={hueOf}
              onOpen={onOpen}
              dragging={drag?.occurrence.event_id === span.item.occurrence.event_id}
              onOpenMenu={onOpenMenu}
              onGrab={
                onGrab
                  ? (edge, grab) =>
                      onGrab((span.item as { occurrence: Occurrence }).occurrence, edge, grab)
                  : undefined
              }
            />
          ) : (
            <TaskBar task={span.item.task} />
          )}
        </div>
        )
      })}
    </div>
  )
}

/**
 * Where a **stretching** span should be drawn mid-drag, or null.
 *
 * Only the edge gestures preview in place. A `move` is drawn by the lifted copy
 * that follows the cursor instead: two things moving at once — a ghost under
 * the pointer *and* the original snapping between columns — read as the grid
 * fighting the gesture, and only one of them can cross into another week.
 *
 * Clamped to the week, and only while the pointer is still in this bar's own
 * row. A bar is a child of one week's CSS grid and cannot be drawn into
 * another, so previewing it clamped to this row once the pointer has left would
 * be worse than showing nothing: it would state the wrong answer confidently.
 */
function previewColumns(
  span: Span<Entry>,
  drag: DragState | null,
  weekIndex: number,
): { start: number; end: number } | null {
  if (!drag || drag.edge === 'move' || span.item.kind !== 'event') return null
  if (span.item.occurrence.event_id !== drag.occurrence.event_id) return null
  if (Math.floor(drag.overIndex / 7) !== weekIndex) return null

  const delta = drag.overIndex - drag.fromIndex
  if (delta === 0) return null

  const clamp = (column: number) => Math.min(6, Math.max(0, column))
  if (drag.edge === 'start') {
    // An edge cannot cross the other one: a bar with a negative width is not a
    // preview of anything.
    return { start: clamp(Math.min(span.startColumn + delta, span.endColumn)), end: span.endColumn }
  }
  return { start: span.startColumn, end: clamp(Math.max(span.endColumn + delta, span.startColumn)) }
}

/**
 * One day, opened from the grid.
 *
 * Everything on it in order, all-day events first because that is how a day
 * reads: the things that are true all day, then the things with a time. The
 * deadlines ride along at the bottom, outlined rather than filled, because a
 * task due today is not an appointment.
 */
function DayModal({
  day,
  occurrences,
  tasks,
  memberById,
  hueOf,
  canWrite,
  onClose,
  onOpen,
  onAdd,
}: {
  readonly day: Date
  readonly occurrences: readonly Occurrence[]
  readonly tasks: readonly AgendaTask[]
  readonly memberById: Map<string, HouseholdMember>
  readonly hueOf: (calendarId: string) => string
  readonly canWrite: boolean
  readonly onClose: () => void
  readonly onOpen: (occurrence: Occurrence) => void
  readonly onAdd: () => void
}) {
  const ordered = [...occurrences].sort((a, b) => {
    if (a.is_all_day !== b.is_all_day) return a.is_all_day ? -1 : 1
    return new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime()
  })

  return (
    <Modal
      title={day.toLocaleDateString(undefined, {
        weekday: 'long',
        month: 'long',
        day: 'numeric',
        year: 'numeric',
      })}
      onClose={onClose}
      labelledBy="day-modal-title"
      footer={
        canWrite ? (
          <button type="button" className="button" onClick={onAdd}>
            Add an event
          </button>
        ) : undefined
      }
    >
      {ordered.length === 0 && tasks.length === 0 ? (
        <p className="empty">Nothing on this day.</p>
      ) : (
        <ul className="day-events">
          {ordered.map((occurrence) => (
            <li key={`${occurrence.event_id}-${occurrence.original_start}`}>
              <button
                type="button"
                className="agenda-row"
                style={{ '--event-hue': hueOf(occurrence.calendar_id) } as CSSProperties}
                onClick={() => onOpen(occurrence)}
              >
                <span className="agenda-fill">
                  <span className="tabular agenda-time">
                    {occurrence.is_all_day
                      ? 'All day'
                      : formatTime(new Date(occurrence.starts_at))}
                  </span>
                  <span className="agenda-title">{occurrence.title}</span>
                  <span className="agenda-people">
                    {occurrence.assignee_ids.map((id) => {
                      const member = memberById.get(id)
                      return member ? <MemberMark key={id} member={member} size="sm" /> : null
                    })}
                  </span>
                </span>
                {isMultiDay(occurrence) && (
                  <span className="badge">{rangeLabel(occurrence)}</span>
                )}
                {occurrence.location && (
                  <span className="muted agenda-where">{occurrence.location}</span>
                )}
              </button>
            </li>
          ))}

          {tasks.map((task) => (
            <li key={task.task_id}>
              <span className="agenda-row agenda-row-task">
                <span className="agenda-fill">
                  <span className="tabular agenda-time">Due</span>
                  <span className="agenda-title">{task.title}</span>
                </span>
                <span className="badge">task</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  )
}

/**
 * How far the pointer must travel before a press counts as a drag.
 *
 * Small enough that a deliberate drag feels immediate, large enough that the
 * hand tremor in an ordinary click never crosses it. Below this a press is a
 * click, and the bar gets its own `click` and `dblclick` — which it does not if
 * capture has been taken, and that is the bug this constant exists to fix.
 */
const DRAG_THRESHOLD_PX = 4

/**
 * How long a still press opens the context menu after.
 *
 * The touch equivalent of a right-click: there is no `contextmenu` event on a
 * touchscreen, so without this the menu is unreachable on two of SPEC §2.1's
 * three access paths. 500ms is the platform convention.
 */
const LONG_PRESS_MS = 500

/** A press that has not yet been decided: a click, a drag, or a long press. */
interface PendingGrab {
  readonly occurrence: Occurrence
  readonly edge: 'move' | 'start' | 'end'
  readonly x: number
  readonly y: number
  readonly pointerId: number
  readonly offsetX: number
  readonly offsetY: number
  readonly width: number
  /** The long-press timer, so it can be cancelled by a move or a release.
   *  Absent for a mouse, which needs no long press.
   *
   *  `| undefined` written out because `exactOptionalPropertyTypes` is on: an
   *  explicit `undefined` is not the same as an absent key, and this is assigned
   *  from a conditional that produces one. */
  readonly timer?: number | undefined
}

/** A gesture in flight: which bar, which edge, and where it started.
 *
 *  Both positions are **indices into the whole 42-day grid**, not columns in a
 *  week. That is the entire difference between a drag that can leave its row
 *  and one that cannot, and the subtraction that commits it is the same either
 *  way — the grid is 42 consecutive days, so a delta of 14 is a fortnight. */
interface DragState {
  readonly occurrence: Occurrence
  /** `move` slides the whole bar; `start` and `end` stretch one edge. */
  readonly edge: 'move' | 'start' | 'end'
  readonly fromIndex: number
  readonly overIndex: number
  /** Captured on the grid, so the gesture survives the pointer leaving it and
      a release outside still commits rather than stranding the bar. */
  readonly pointerId: number
  /** Where the pointer is now. */
  readonly x: number
  readonly y: number
  /** Where **in the bar** it was picked up, and how wide the bar was. The
      lifted copy hangs off the cursor at exactly the point it was grabbed,
      which is what makes it feel picked up rather than teleported. */
  readonly offsetX: number
  readonly offsetY: number
  readonly width: number
}

/** What a bar reports when somebody takes hold of it. */
interface Grab {
  readonly x: number
  readonly y: number
  readonly pointerId: number
  /** `mouse`, `touch` or `pen`. Decides whether a long press is armed: see
      LONG_PRESS_MS. */
  readonly pointerType: string
  readonly rect: DOMRect
}

/** A grab reported from one of the edge handles.
 *
 *  The handle is a child of the bar, so `currentTarget` is the handle and its
 *  box is 10px wide. The measurement wanted is the *bar's*, or an edge-stretch
 *  would report a ten-pixel-wide thing being picked up. */
function grabFrom(event: React.PointerEvent<HTMLElement>): Grab {
  const bar = event.currentTarget.closest('.chip-event') ?? event.currentTarget
  return {
    x: event.clientX,
    y: event.clientY,
    pointerId: event.pointerId,
    pointerType: event.pointerType,
    rect: bar.getBoundingClientRect(),
  }
}

function keyFor(span: Span<Entry>): string {
  return span.item.kind === 'event'
    ? `${span.item.occurrence.event_id}-${span.item.occurrence.original_start}`
    : `task-${span.item.task.task_id}`
}

function EventBar({
  span,
  occurrence,
  hueOf,
  onOpen,
  dragging = false,
  onOpenMenu,
  onGrab,
}: {
  readonly span: Span<Entry>
  readonly occurrence: Occurrence
  readonly hueOf: (calendarId: string) => string
  readonly onOpen: (occurrence: Occurrence) => void
  readonly dragging?: boolean
  readonly onOpenMenu?: ((occurrence: Occurrence, at: { x: number; y: number }) => void) | undefined
  readonly onGrab?: ((edge: 'move' | 'start' | 'end', grab: Grab) => void) | undefined
}) {
  // A bar cut by the week boundary loses its rounded end on that side, so it
  // reads as continuing rather than as two separate events.
  //
  // The edge handles only appear on the end that is really there: stretching a
  // bar by an edge that is a week boundary rather than the event's own start
  // would move a date the person cannot see.
  return (
    <button
      type="button"
      className="chip-event"
      data-continues-before={span.continuesBefore}
      data-continues-after={span.continuesAfter}
      data-dragging={dragging}
      data-draggable={onGrab !== undefined}
      style={{ '--event-hue': hueOf(occurrence.calendar_id) } as CSSProperties}
      // **Double-click, not click.** A single click on a bar that is also
      // draggable is ambiguous, and resolving that ambiguity by guessing is what
      // produced a month grid whose events could not be opened at all. Two
      // gestures, two meanings: drag moves it, double-click opens it.
      onDoubleClick={() => onOpen(occurrence)}
      // Enter and Space arrive here as a click on a <button>, which is the
      // keyboard equivalent SPEC §4.3 requires of every pointer gesture. A
      // single *mouse* click deliberately does nothing.
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen(occurrence)
        }
      }}
      onContextMenu={(event) => {
        if (!onOpenMenu) return
        event.preventDefault()
        // Shift+F10 and the Menu key fire this too, with no usable coordinates —
        // `contextMenuPoint` falls back to the bar's own box for those, which is
        // what makes the menu reachable without a mouse.
        const box = event.currentTarget.getBoundingClientRect()
        onOpenMenu(occurrence, contextMenuPoint(event, box))
      }}
      onPointerDown={(event) => {
        // Only a plain primary-button press can begin a drag, and nothing is
        // captured here or yet: see `pending` in MonthGrid. A right-click must
        // fall through to onContextMenu above rather than starting a gesture.
        if (!onGrab || event.button !== 0) return
        onGrab('move', {
          x: event.clientX,
          y: event.clientY,
          pointerId: event.pointerId,
          pointerType: event.pointerType,
          // The bar's own box, so the lifted copy is the same size and is held
          // at the point it was grabbed.
          rect: event.currentTarget.getBoundingClientRect(),
        })
      }}
    >
      {onGrab && !span.continuesBefore && (
        <span
          className="chip-handle"
          data-edge="start"
          aria-hidden="true"
          onPointerDown={(event) => {
            event.stopPropagation()
            onGrab('start', grabFrom(event))
          }}
        />
      )}
      {span.continuesBefore && <span aria-hidden="true">←</span>}
      {!occurrence.is_all_day && !span.continuesBefore && (
        <span className="chip-time tabular">{formatTime(new Date(occurrence.starts_at))}</span>
      )}
      <span className="chip-title">{occurrence.title}</span>
      {occurrence.is_recurring && (
        <span className="chip-repeat" aria-label="repeats">
          ↻
        </span>
      )}
      {span.continuesAfter && <span aria-hidden="true">→</span>}

      {onGrab && !span.continuesAfter && (
        <span
          className="chip-handle"
          data-edge="end"
          aria-hidden="true"
          onPointerDown={(event) => {
            event.stopPropagation()
            onGrab('end', grabFrom(event))
          }}
        />
      )}
    </button>
  )
}

/** A deadline, not an appointment — outlined rather than filled, on purpose. */
function TaskBar({ task }: { readonly task: AgendaTask }) {
  return (
    <span className="chip-task" data-done={task.status === 'done'}>
      <span className="chip-title">{task.title}</span>
      <span className="visually-hidden"> — task due</span>
    </span>
  )
}

function AgendaList({
  occurrences,
  tasks,
  memberById,
  hueOf,
  onOpenMenu,
  onOpen,
}: {
  readonly occurrences: readonly Occurrence[]
  readonly tasks: readonly AgendaTask[]
  readonly memberById: Map<string, HouseholdMember>
  readonly hueOf: (calendarId: string) => string
  readonly onOpen: (occurrence: Occurrence) => void
  /** The same menu the month grid uses. A single click already opens the editor
      here -- there is no drag to disambiguate from -- so this is for parity: once
      right-click-for-a-menu exists anywhere, it is expected everywhere. */
  readonly onOpenMenu: (occurrence: Occurrence, at: { x: number; y: number }) => void
}) {
  type Row =
    | { kind: 'event'; at: Date; occurrence: Occurrence }
    | { kind: 'task'; at: Date; task: AgendaTask }

  // Multi-day events come out of the day-by-day list and get their own
  // section. Repeating a week-long trip under all seven of its days is how an
  // agenda stops being scannable — and the thing somebody wants to know about
  // one is the range, which a day heading cannot tell them.
  const spanning = occurrences
    .filter(isMultiDay)
    .sort((a, b) => new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime())

  const rows: Row[] = [
    ...occurrences.filter((occurrence) => !isMultiDay(occurrence)).map((occurrence) => ({
      kind: 'event' as const,
      at: new Date(occurrence.starts_at),
      occurrence,
    })),
    ...tasks.map((task) => ({ kind: 'task' as const, at: new Date(task.due_at), task })),
  ].sort((a, b) => a.at.getTime() - b.at.getTime())

  if (rows.length === 0 && spanning.length === 0) {
    return <p className="empty">Nothing this month.</p>
  }

  const days = new Map<string, Row[]>()
  for (const row of rows) {
    const key = row.at.toDateString()
    days.set(key, [...(days.get(key) ?? []), row])
  }

  return (
    <div className="agenda">
      {spanning.length > 0 && (
        <section className="agenda-day agenda-spanning">
          <h2>Across several days</h2>
          <ul>
            {spanning.map((occurrence) => (
              <li key={`${occurrence.event_id}-${occurrence.original_start}`}>
                <button
                  type="button"
                  className="agenda-row"
                  style={{ '--event-hue': hueOf(occurrence.calendar_id) } as CSSProperties}
                  onClick={() => onOpen(occurrence)}
                  onContextMenu={(event) => {
                    event.preventDefault()
                    onOpenMenu(occurrence, contextMenuPoint(event, event.currentTarget.getBoundingClientRect()))
                  }}
                >
                  <span className="agenda-fill">
                    {/* The range, where a single-day row shows a time. It is
                        the thing that is actually being asked about. */}
                    <span className="tabular agenda-time agenda-range">
                      {rangeLabel(occurrence)}
                    </span>
                    <span className="agenda-title">{occurrence.title}</span>
                    <span className="agenda-people">
                      {occurrence.assignee_ids.map((id) => {
                        const member = memberById.get(id)
                        return member ? <MemberMark key={id} member={member} size="sm" /> : null
                      })}
                    </span>
                  </span>
                  <span className="badge">
                    {daysCovered(occurrence)} days
                  </span>
                  {occurrence.location && (
                    <span className="muted agenda-where">{occurrence.location}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {[...days.entries()].map(([key, forDay]) => (
        <section key={key} className="agenda-day">
          <h2>{key}</h2>
          <ul>
            {forDay.map((row) =>
              row.kind === 'event' ? (
                <li key={`${row.occurrence.event_id}-${row.occurrence.original_start}`}>
                  <button
                    type="button"
                    className="agenda-row"
                    style={{ '--event-hue': hueOf(row.occurrence.calendar_id) } as CSSProperties}
                    onClick={() => onOpen(row.occurrence)}
                    onContextMenu={(event) => {
                      event.preventDefault()
                      onOpenMenu(row.occurrence, contextMenuPoint(event, event.currentTarget.getBoundingClientRect()))
                    }}
                  >
                    <span className="agenda-fill">
                      <span className="tabular agenda-time">
                        {row.occurrence.is_all_day ? 'All day' : formatTime(row.at)}
                      </span>
                      <span className="agenda-title">{row.occurrence.title}</span>
                      <span className="agenda-people">
                        {row.occurrence.assignee_ids.map((id) => {
                          const member = memberById.get(id)
                          return member ? <MemberMark key={id} member={member} size="sm" /> : null
                        })}
                      </span>
                    </span>
                    {row.occurrence.location && (
                      <span className="muted agenda-where">{row.occurrence.location}</span>
                    )}
                    {row.occurrence.recurrence_label && (
                      <span className="badge">{row.occurrence.recurrence_label}</span>
                    )}
                  </button>
                </li>
              ) : (
                <li key={row.task.task_id}>
                  <span className="agenda-row agenda-row-task">
                    <span className="agenda-fill">
                      <span className="tabular agenda-time">Due</span>
                      <span className="agenda-title">{row.task.title}</span>
                    </span>
                    <span className="badge">task</span>
                  </span>
                </li>
              ),
            )}
          </ul>
        </section>
      ))}
    </div>
  )
}
