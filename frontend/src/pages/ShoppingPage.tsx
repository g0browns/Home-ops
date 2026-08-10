// Shopping (SPEC §4.12, phase 6).
//
// Many lists, side by side, because the thing you can do here that you could
// not do before is move a line from one to another — and a target you cannot
// see is a target you cannot drag to. Columns, like the task board, for the
// same reason the task board has them.
//
// Three constraints shape the rest, all inherited from Phase 5d and all still
// true:
//
//   * **The whole row is the tick.** A 16px checkbox is not a target for a
//     thumb holding a trolley, so the row is a <label> and the box is only the
//     thing it points at.
//   * **Every drag has a keyboard equivalent.** HTML5 drag-and-drop is
//     pointer-only, so a drag-only transfer does not exist for anyone
//     navigating by keyboard — or on a phone, which is where this page is
//     actually used. Every row carries a "Move to…" select.
//   * **It polls with an ETag.** One request for every list, and a 304 while
//     nothing changes. Unlike a websocket it behaves the same over all three
//     access paths (§2.1).
//
// Colour is never the only signal: a line that could not be combined carries
// the words "couldn't add these", a list says who can see it in words, and who
// ticked something is an initials mark beside a name.

import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'

import {
  addShoppingItem,
  clearCheckedShoppingItems,
  createShoppingList,
  deleteShoppingList,
  errorMessage,
  generateShoppingList,
  getHouseholdSettings,
  listShoppingLists,
  listShoppingSections,
  listUsers,
  removeShoppingItem,
  updateShoppingItem,
  updateShoppingList,
  type CurrentUser,
  type HouseholdMember,
  type ShoppingItem,
  type ShoppingList,
  type Visibility,
} from '../api/client'
import { MemberMark } from '../components/MemberMark'
import {
  addDays,
  isoDate,
  isWeekStart,
  startOfDay,
  startOfWeek,
  type WeekStart,
} from '../lib/dates'
import { amountOf, bySection } from '../lib/shopping'
import { UNITS } from '../lib/units'

/** How often to re-ask, while the tab is actually in front of somebody. */
const POLL_MS = 6000

const DRAG_TYPE = 'application/x-home-ops-shopping'


const VISIBILITY_WORDS: Record<Visibility, string> = {
  private: 'Only you',
  assignees: 'Only some of us',
  household: 'Everyone',
}

export function ShoppingPage({ me }: { readonly me: CurrentUser }) {
  const [lists, setLists] = useState<ShoppingList[]>([])
  const [members, setMembers] = useState<HouseholdMember[]>([])
  const [sections, setSections] = useState<string[]>([])
  const [weekStart, setWeekStart] = useState<WeekStart>('monday')
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<ShoppingList | 'new' | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [over, setOver] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  // A ref, not state: it changes on every poll and nothing renders from it, so
  // in state it would re-render every list ten times a minute for no reason.
  const etag = useRef<string | null>(null)

  const canWrite = me.permissions['shopping'] === 'write'
  // Building reads the meal plan and recipes, so the server also requires
  // Kitchen read. Hiding a button that would 403 is the honest thing to draw.
  const canBuild = canWrite && me.permissions['kitchen'] !== undefined

  const refresh = useCallback(async (force = false) => {
    const result = await listShoppingLists(force ? null : etag.current)
    etag.current = result.etag
    // A 304 means "keep what is on screen" — the entire point of the header
    // exchange — so it must not fall through to setLists([]).
    if (result.lists) setLists(result.lists)
    setLoading(false)
  }, [])

  const refreshSections = useCallback(async () => {
    const result = await listShoppingSections()
    if (result.ok) setSections(result.data)
  }, [])

  useEffect(() => {
    void refresh(true)
    void refreshSections()
    void listUsers().then((result) => {
      if (result.ok) setMembers(result.data)
    })
    void getHouseholdSettings().then((result) => {
      if (!result.ok) return
      const value = result.data.values['week_starts_on']
      if (isWeekStart(value)) setWeekStart(value)
    })
  }, [refresh, refreshSections])

  useEffect(() => {
    // Polling stops when the page is hidden. A phone in a pocket asking every
    // six seconds is somebody's battery.
    const tick = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    const timer = setInterval(tick, POLL_MS)
    document.addEventListener('visibilitychange', tick)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [refresh])

  const memberById = new Map(members.map((member) => [member.id, member]))
  const outstanding = lists.reduce(
    (total, list) => total + list.items.filter((item) => !item.is_checked).length,
    0,
  )

  const changed = useCallback(async () => {
    await refresh(true)
    await refreshSections()
  }, [refresh, refreshSections])

  const moveItem = useCallback(
    async (itemId: string, listId: string) => {
      await updateShoppingItem(itemId, { list_id: listId })
      await refresh(true)
    },
    [refresh],
  )

  async function drop(event: DragEvent<HTMLElement>, listId: string) {
    event.preventDefault()
    setOver(null)
    setDragging(null)

    // From dataTransfer, never from React state: state set in dragstart is not
    // guaranteed visible here before a re-render, so a fast or programmatic
    // drag arrives before the component knows anything is happening.
    const payload =
      event.dataTransfer.getData(DRAG_TYPE) || event.dataTransfer.getData('text/plain')
    if (!payload) return
    await moveItem(payload, listId)
  }

  async function build() {
    const target = lists.find((list) => list.is_meal_plan_target)
    if (!target) {
      setError(
        'No list is set as the meal-plan list. Edit one and tick "Build the meal plan into this list".',
      )
      return
    }

    const first = startOfWeek(startOfDay(new Date()), weekStart)
    setBusy(true)
    setError(null)
    const result = await generateShoppingList(isoDate(first), isoDate(addDays(first, 6)), target.id)
    setBusy(false)

    if (!result.ok) {
      setError(errorMessage(result.data, 'Could not build the list.'))
      return
    }
    await refresh(true)

    // Everything the build could not do, said out loud. A list that is quietly
    // short is worse than one that admits what it missed.
    const { hidden_meals, text_meals, uncombined, kept_on_other_lists } = result.data
    const parts: string[] = []
    if (hidden_meals > 0) {
      parts.push(
        `${hidden_meals} planned ${hidden_meals === 1 ? 'meal is' : 'meals are'} not yours to see, so nothing was added for ${hidden_meals === 1 ? 'it' : 'them'}.`,
      )
    }
    if (text_meals.length > 0) {
      parts.push(
        `${text_meals.join(', ')} ${text_meals.length === 1 ? 'is not a recipe' : 'are not recipes'}, so nothing was added for ${text_meals.length === 1 ? 'it' : 'them'}.`,
      )
    }
    if (kept_on_other_lists > 0) {
      parts.push(
        `${kept_on_other_lists} ${kept_on_other_lists === 1 ? 'item is' : 'items are'} already on another list, left where you put ${kept_on_other_lists === 1 ? 'it' : 'them'}.`,
      )
    }
    if (uncombined > 0) {
      parts.push(`${uncombined} lines could not be added together — they are listed separately.`)
    }
    setNotice(parts.join(' ') || null)
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Shopping</h1>
        <p className="page-summary">
          <strong>{outstanding}</strong> still to get
          {lists.length > 0 && (
            <span className="muted">
              {' '}
              · {lists.length} {lists.length === 1 ? 'list' : 'lists'}
            </span>
          )}
        </p>
      </div>

      <div className="toolbar">
        {canBuild && (
          <button type="button" className="button" onClick={() => void build()} disabled={busy}>
            {busy ? 'Building…' : 'Build from this week'}
          </button>
        )}
        {canWrite && (
          <button type="button" onClick={() => setEditing('new')}>
            New list
          </button>
        )}
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}{' '}
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </p>
      )}

      {/* One datalist for the page: every section input offers the same
          vocabulary, and duplicate ids would be invalid anyway. */}
      <datalist id="known-sections">
        {sections.map((section) => (
          <option key={section} value={section} />
        ))}
      </datalist>

      {loading ? (
        <p className="loading">Loading…</p>
      ) : lists.length === 0 ? (
        <p className="empty">
          No lists yet. Groceries, the hardware shop, presents — anything the household has to buy.
        </p>
      ) : (
        <div className="lists">
          {lists.map((list) => (
            <ListColumn
              key={list.id}
              list={list}
              lists={lists}
              me={me}
              canWrite={canWrite}
              memberById={memberById}
              dragging={dragging}
              receiving={over === list.id}
              onDragStart={setDragging}
              onDragEnd={() => setDragging(null)}
              onDragOver={() => setOver(list.id)}
              onDragLeave={() => setOver((current) => (current === list.id ? null : current))}
              onDrop={(event) => void drop(event, list.id)}
              onMove={moveItem}
              onEdit={() => setEditing(list)}
              onChanged={changed}
            />
          ))}
        </div>
      )}

      {editing && (
        <ListEditor
          list={editing === 'new' ? null : editing}
          members={members}
          me={me}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await refresh(true)
          }}
        />
      )}
    </div>
  )
}

/** One list, and everything on it. */
function ListColumn({
  list,
  lists,
  me,
  canWrite,
  memberById,
  dragging,
  receiving,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDragLeave,
  onDrop,
  onMove,
  onEdit,
  onChanged,
}: {
  readonly list: ShoppingList
  readonly lists: readonly ShoppingList[]
  readonly me: CurrentUser
  readonly canWrite: boolean
  readonly memberById: Map<string, HouseholdMember>
  readonly dragging: string | null
  readonly receiving: boolean
  readonly onDragStart: (id: string) => void
  readonly onDragEnd: () => void
  readonly onDragOver: () => void
  readonly onDragLeave: () => void
  readonly onDrop: (event: DragEvent<HTMLElement>) => void
  readonly onMove: (itemId: string, listId: string) => Promise<void>
  readonly onEdit: () => void
  readonly onChanged: () => Promise<void>
}) {
  const groups = bySection(list.items)
  const ticked = list.items.filter((item) => item.is_checked).length

  return (
    <section
      className="list-column"
      data-receiving={receiving}
      aria-label={list.name}
      onDragOver={(event) => {
        if (!canWrite) return
        event.preventDefault()
        onDragOver()
      }}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <header className="list-head">
        <h2>
          {list.name}
          {list.is_meal_plan_target && (
            <span className="list-badge" title="The meal planner builds into this list">
              meal plan
            </span>
          )}
        </h2>
        <p className="list-meta">
          {/* The word, not only a mark: who can see a list is the one thing here
              nobody should have to guess at. */}
          <span data-visibility={list.visibility}>{VISIBILITY_WORDS[list.visibility]}</span>
          {list.visibility === 'assignees' &&
            list.shared_with.map((id) => {
              const member = memberById.get(id)
              return member ? (
                <span key={id} className="shared-mark">
                  <MemberMark member={member} size="sm" />
                  <span className="visually-hidden">{member.display_name}</span>
                </span>
              ) : null
            })}
          {canWrite && (
            <button type="button" className="link-button" onClick={onEdit}>
              Edit
            </button>
          )}
        </p>
      </header>

      {canWrite && <AddLine list={list} onAdded={onChanged} />}

      {list.items.length === 0 ? (
        <p className="list-empty">Nothing on this one.</p>
      ) : (
        groups.map((group) => (
          <div key={group.section} className="aisle">
            <h3>{group.section}</h3>
            <ul className="shopping-list">
              {group.items.map((item) => (
                <Row
                  key={item.id}
                  item={item}
                  lists={lists}
                  me={me}
                  canWrite={canWrite}
                  shopper={item.checked_by_id ? memberById.get(item.checked_by_id) : undefined}
                  dragging={dragging === item.id}
                  onDragStart={onDragStart}
                  onDragEnd={onDragEnd}
                  onMove={onMove}
                  onChanged={onChanged}
                />
              ))}
            </ul>
          </div>
        ))
      )}

      {canWrite && ticked > 0 && (
        <button
          type="button"
          className="link-button clear-ticked"
          onClick={async () => {
            await clearCheckedShoppingItems(list.id)
            await onChanged()
          }}
        >
          Clear {ticked} ticked
        </button>
      )}
    </section>
  )
}

function Row({
  item,
  lists,
  me,
  canWrite,
  shopper,
  dragging,
  onDragStart,
  onDragEnd,
  onMove,
  onChanged,
}: {
  readonly item: ShoppingItem
  readonly lists: readonly ShoppingList[]
  readonly me: CurrentUser
  readonly canWrite: boolean
  readonly shopper: HouseholdMember | undefined
  readonly dragging: boolean
  readonly onDragStart: (id: string) => void
  readonly onDragEnd: () => void
  readonly onMove: (itemId: string, listId: string) => Promise<void>
  readonly onChanged: () => Promise<void>
}) {
  // Optimistic, because a tick that waits for a round-trip on a shop's wifi
  // feels broken. The write is idempotent, so a failed one is put right by the
  // next poll.
  const [checked, setChecked] = useState(item.is_checked)
  useEffect(() => setChecked(item.is_checked), [item.is_checked])

  const amount = amountOf(item)
  const elsewhere = lists.filter((list) => list.id !== item.list_id)

  return (
    <li
      className="shopping-row"
      data-checked={checked}
      data-dragging={dragging}
      draggable={canWrite}
      onDragStart={(event) => {
        event.dataTransfer.setData(DRAG_TYPE, item.id)
        event.dataTransfer.setData('text/plain', item.id)
        event.dataTransfer.effectAllowed = 'move'
        onDragStart(item.id)
      }}
      onDragEnd={onDragEnd}
    >
      <label className="shopping-tick">
        <input
          type="checkbox"
          checked={checked}
          disabled={!canWrite}
          onChange={async (event) => {
            const next = event.currentTarget.checked
            setChecked(next)
            await updateShoppingItem(item.id, { is_checked: next })
            await onChanged()
          }}
        />
        <span className="shopping-name">
          {item.name}
          {item.note && <span className="muted"> — {item.note}</span>}
        </span>
      </label>

      {/* The amount, editable in place. A list built from the plan is a
          starting point — "the recipes need two, buy four" is the ordinary
          case, not an edge one — and an amount you can only change by deleting
          the line and retyping it is an amount you stop changing. */}
      {canWrite ? (
        <AmountField item={item} onChanged={onChanged} />
      ) : (
        amount && <span className="shopping-amount tabular">{amount}</span>
      )}

      {item.is_uncombined && (
        // The words, not just a colour. §4.6 asked the aggregation to combine
        // "or flag that it can't", and this is the flag arriving where somebody
        // can act on it.
        <span
          className="uncombined"
          title="These are different kinds of measure — grams and cups cannot be added without a density"
        >
          couldn&rsquo;t add these
        </span>
      )}

      {shopper && (
        <span className="shopping-by">
          <MemberMark member={shopper} size="sm" />
          <span className="shopping-by-name">
            {shopper.id === me.id ? 'You' : shopper.display_name}
          </span>
        </span>
      )}

      {canWrite && <SectionPicker item={item} onChanged={onChanged} />}

      {canWrite && elsewhere.length > 0 && (
        // The keyboard — and phone — equivalent of dragging between columns.
        // Without it the transfer does not exist for anyone not using a mouse.
        <>
          <label className="visually-hidden" htmlFor={`move-${item.id}`}>
            Move {item.name} to another list
          </label>
          <select
            id={`move-${item.id}`}
            className="move-to"
            value=""
            onChange={(event) => {
              const value = event.currentTarget.value
              if (value) void onMove(item.id, value)
            }}
          >
            {/* An arrow, not "Move to…": in a column this control sits beside
                the name and a wide one wraps every row onto two lines. The
                accessible name comes from the label above, which says the whole
                sentence. */}
            <option value="">→</option>
            {elsewhere.map((list) => (
              <option key={list.id} value={list.id}>
                {list.name}
              </option>
            ))}
          </select>
        </>
      )}

      {canWrite && (
        <button
          type="button"
          className="icon-button"
          onClick={async () => {
            await removeShoppingItem(item.id)
            await onChanged()
          }}
          aria-label={`Take ${item.name} off the list`}
        >
          ×
        </button>
      )}
    </li>
  )
}

/**
 * The amount on a line, editable where it sits.
 *
 * Commits on blur and on Enter, and has no Set button, for the reason the
 * section picker gives: a button beside a text field is a blur race.
 */
function AmountField({
  item,
  onChanged,
}: {
  readonly item: ShoppingItem
  readonly onChanged: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  const [unit, setUnit] = useState(item.unit ?? '')
  const shown = amountOf(item)

  async function commit() {
    setOpen(false)
    const next = value.trim()
    const quantity = next === '' ? null : next
    if (quantity === (item.quantity === null ? null : String(Number(item.quantity)))
        && (unit || null) === item.unit) {
      return
    }
    await updateShoppingItem(item.id, { quantity, unit: unit || null })
    await onChanged()
  }

  if (!open) {
    return (
      <button
        type="button"
        className="shopping-amount tabular amount-set"
        data-overridden={item.quantity_overridden}
        onClick={() => {
          // The stored value, not the formatted one: "1 1/2 cups" is unusable
          // as the starting point for typing a number.
          setValue(item.quantity === null ? '' : String(Number(item.quantity)))
          setUnit(item.unit ?? '')
          setOpen(true)
        }}
        title={
          item.quantity_overridden
            ? 'You set this amount; the weekly build leaves it alone'
            : 'Change how much to get'
        }
      >
        {shown || 'how much?'}
      </button>
    )
  }

  return (
    <span className="amount-edit">
      <label className="visually-hidden" htmlFor={`amount-${item.id}`}>
        How much {item.name}
      </label>
      <input
        id={`amount-${item.id}`}
        className="quantity"
        value={value}
        autoFocus
        inputMode="decimal"
        placeholder="Qty"
        onChange={(event) => setValue(event.currentTarget.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            void commit()
          }
          if (event.key === 'Escape') setOpen(false)
        }}
      />
      <label className="visually-hidden" htmlFor={`amount-unit-${item.id}`}>
        Unit for {item.name}
      </label>
      <select
        id={`amount-unit-${item.id}`}
        value={unit}
        onChange={(event) => setUnit(event.currentTarget.value)}
        onBlur={() => void commit()}
      >
        <option value="">no unit</option>
        {UNITS.map((option) => (
          <option key={option.key} value={option.key}>
            {option.plural}
          </option>
        ))}
      </select>
    </span>
  )
}

/**
 * Where in the shop a line lives.
 *
 * A datalist rather than a select: sections differ by shop, so the ones already
 * in use are offered and a new one can simply be typed. It commits on blur and
 * on Enter and has no Set button on purpose — a button beside a text field is a
 * blur race, where clicking it fires `blur` first and the click lands on a
 * control that has already gone.
 */
function SectionPicker({
  item,
  onChanged,
}: {
  readonly item: ShoppingItem
  readonly onChanged: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState(item.section ?? '')

  async function commit() {
    setOpen(false)
    const next = value.trim() || null
    if (next === item.section) return
    await updateShoppingItem(item.id, { section: next })
    await onChanged()
  }

  if (!open) {
    return (
      <button
        type="button"
        className="aisle-set"
        data-unset={item.section === null}
        onClick={() => {
          setValue(item.section ?? '')
          setOpen(true)
        }}
        aria-label={`Say where ${item.name} lives in the shop`}
        title="Where in the shop is this?"
      >
        {item.section ?? 'aisle?'}
      </button>
    )
  }

  return (
    <>
      <label className="visually-hidden" htmlFor={`section-${item.id}`}>
        Where {item.name} lives in the shop
      </label>
      <input
        id={`section-${item.id}`}
        className="aisle-input"
        list="known-sections"
        value={value}
        autoFocus
        placeholder="Aisle"
        onChange={(event) => setValue(event.currentTarget.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            void commit()
          }
          if (event.key === 'Escape') setOpen(false)
        }}
      />
    </>
  )
}

/**
 * One line, typed.
 *
 * The name goes in as an *ingredient* name so "flour" typed here lands on the
 * same row as a generated one and inherits its aisle — the whole reason
 * ingredients are a table rather than free text. The server falls back to a
 * plain title for everything that is not an ingredient, which on a hardware
 * list is all of it.
 */
function AddLine({
  list,
  onAdded,
}: {
  readonly list: ShoppingList
  readonly onAdded: () => Promise<void>
}) {
  const [name, setName] = useState('')
  const [quantity, setQuantity] = useState('')
  const [unit, setUnit] = useState('')
  const [error, setError] = useState<string | null>(null)

  return (
    <form
      className="shopping-add"
      onSubmit={async (event) => {
        event.preventDefault()
        const trimmed = name.trim()
        if (!trimmed) return

        const result = await addShoppingItem(list.id, {
          ingredient_name: trimmed,
          quantity: quantity.trim() || null,
          unit: unit || null,
        })
        if (!result.ok) {
          setError(errorMessage(result.data, 'Could not add that.'))
          return
        }
        setName('')
        setQuantity('')
        setUnit('')
        setError(null)
        await onAdded()
      }}
    >
      <label className="visually-hidden" htmlFor={`add-${list.id}`}>
        Something to buy for {list.name}
      </label>
      <input
        id={`add-${list.id}`}
        value={name}
        onChange={(event) => setName(event.currentTarget.value)}
        placeholder="Add something…"
        autoComplete="off"
      />

      <label className="visually-hidden" htmlFor={`qty-${list.id}`}>
        How much
      </label>
      <input
        id={`qty-${list.id}`}
        className="quantity"
        value={quantity}
        onChange={(event) => setQuantity(event.currentTarget.value)}
        placeholder="Qty"
        inputMode="decimal"
        autoComplete="off"
      />

      <label className="visually-hidden" htmlFor={`unit-${list.id}`}>
        Unit
      </label>
      <select
        id={`unit-${list.id}`}
        value={unit}
        onChange={(event) => setUnit(event.currentTarget.value)}
      >
        <option value="">no unit</option>
        {UNITS.map((option) => (
          <option key={option.key} value={option.key}>
            {option.plural}
          </option>
        ))}
      </select>

      <button type="submit" className="button">
        Add
      </button>
      {error && <p className="alert">{error}</p>}
    </form>
  )
}

/** Create or edit a list: its name, who can see it, and whether the plan fills it. */
function ListEditor({
  list,
  members,
  me,
  onClose,
  onSaved,
}: {
  readonly list: ShoppingList | null
  readonly members: readonly HouseholdMember[]
  readonly me: CurrentUser
  readonly onClose: () => void
  readonly onSaved: () => Promise<void>
}) {
  const [name, setName] = useState(list?.name ?? '')
  const [visibility, setVisibility] = useState<Visibility>(list?.visibility ?? 'household')
  const [sharedWith, setSharedWith] = useState<string[]>(list?.shared_with ?? [])
  const [isTarget, setIsTarget] = useState(list?.is_meal_plan_target ?? false)
  const [error, setError] = useState<string | null>(null)

  const others = members.filter((member) => member.id !== me.id)

  return (
    <div className="list-editor" role="dialog" aria-label={list ? `Edit ${list.name}` : 'New list'}>
      <form
        className="note-editor"
        onSubmit={async (event) => {
          event.preventDefault()
          const body = {
            name: name.trim(),
            visibility,
            // Sent whatever the visibility, so switching to "only some of us"
            // and back does not silently drop who it was shared with.
            shared_with: sharedWith,
            is_meal_plan_target: isTarget,
          }
          const result = list
            ? await updateShoppingList(list.id, body)
            : await createShoppingList(body)
          if (!result.ok) {
            setError(errorMessage(result.data, 'Could not save that list.'))
            return
          }
          await onSaved()
        }}
      >
        <div className="field">
          <label htmlFor="list-name">Name</label>
          <input
            id="list-name"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            placeholder="Groceries"
            autoFocus
            required
          />
        </div>

        <div className="field">
          <label htmlFor="list-visibility">Who can see it</label>
          <select
            id="list-visibility"
            value={visibility}
            onChange={(event) => setVisibility(event.currentTarget.value as Visibility)}
          >
            <option value="household">Everyone in the household</option>
            <option value="assignees">Only the people I pick</option>
            <option value="private">Only me</option>
          </select>
        </div>

        {visibility === 'assignees' && (
          <fieldset className="field">
            <legend>Shared with</legend>
            {others.length === 0 ? (
              <p className="muted">There is nobody else to share it with yet.</p>
            ) : (
              others.map((member) => (
                <label key={member.id} className="check">
                  <input
                    type="checkbox"
                    checked={sharedWith.includes(member.id)}
                    onChange={(event) =>
                      setSharedWith((current) =>
                        event.currentTarget.checked
                          ? [...current, member.id]
                          : current.filter((id) => id !== member.id),
                      )
                    }
                  />
                  <MemberMark member={member} size="sm" />
                  {member.display_name}
                </label>
              ))
            )}
          </fieldset>
        )}

        <label className="check">
          <input
            type="checkbox"
            checked={isTarget}
            onChange={(event) => setIsTarget(event.currentTarget.checked)}
          />
          Build the meal plan into this list
        </label>
        <p className="field-hint">
          Only one list can be the meal-plan list. Ticking this takes it off whichever list has it
          now.
        </p>

        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}

        <div className="editor-actions">
          <button type="submit" className="button">
            Save
          </button>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          {list && (
            <button
              type="button"
              className="link-button"
              onClick={async () => {
                await deleteShoppingList(list.id)
                await onSaved()
              }}
            >
              Delete this list and everything on it
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
