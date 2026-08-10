// Putting a recipe on the plan, from the recipe itself (SPEC §4.6).
//
// A date field and a slot dropdown would have been fewer lines and worse.
// Choosing when to cook something is a question *about the plan* — "is Tuesday
// already spoken for?" — and a bare `<input type="date">` cannot answer it, so
// the answer has to come from the person's memory or from closing this and
// going to look. That is the same mistake the event editor made before it
// started listing a day's existing events above the form.
//
// So the dialog shows a fortnight of the real plan, with what is already in the
// chosen slot on each day, and you pick a day by pointing at it.
//
// A fortnight rather than a week: "next Tuesday" is the ordinary request and a
// single week makes it a paging exercise. Rather than the month the calendar
// draws, because a month of meals is mostly rows you are not going to use and
// each day would be too narrow to say what is in it.

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  addMealPlanEntry,
  errorMessage,
  getHouseholdSettings,
  listMealPlan,
  type MealPlanEntry,
  type MealSlot,
  type Recipe,
} from '../api/client'
import { Modal } from '../components/Modal'
import {
  addDays,
  isoDate,
  isSameDay,
  isWeekStart,
  startOfDay,
  startOfWeek,
  type WeekStart,
} from '../lib/dates'

const SLOTS: readonly { key: MealSlot; label: string }[] = [
  { key: 'breakfast', label: 'Breakfast' },
  { key: 'lunch', label: 'Lunch' },
  { key: 'dinner', label: 'Dinner' },
  { key: 'side', label: 'Side' },
]

/** Two weeks, drawn as two rows of seven so the columns line up as weekdays. */
const DAYS_SHOWN = 14

export function AddToPlan({
  recipe,
  onClose,
  onAdded,
}: {
  readonly recipe: Recipe
  readonly onClose: () => void
  /** Told what landed where, so the recipe can say so in words. */
  readonly onAdded: (entry: MealPlanEntry) => void
}) {
  const [weekStart, setWeekStart] = useState<WeekStart>('monday')
  //: Whole weeks from the current one, **not** a stored anchor date. The week
  //: start arrives from the server a moment after this opens, and an anchor
  //: computed before it lands would keep pointing at the old first day.
  const [weekOffset, setWeekOffset] = useState(0)
  const [entries, setEntries] = useState<MealPlanEntry[]>([])
  // Dinner: the household's own history is 58 dinners and 18 sides and not one
  // breakfast, so it is the answer far more often than it is not.
  const [slot, setSlot] = useState<MealSlot>('dinner')
  const [selected, setSelected] = useState(() => startOfDay(new Date()))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const today = startOfDay(new Date())

  useEffect(() => {
    let cancelled = false
    void getHouseholdSettings().then((result) => {
      if (cancelled || !result.ok) return
      const value = result.data.values['week_starts_on']
      if (isWeekStart(value)) setWeekStart(value)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const days = useMemo(() => {
    const first = addDays(startOfWeek(today, weekStart), weekOffset * 7)
    return Array.from({ length: DAYS_SHOWN }, (_, index) => addDays(first, index))
    // `today` is a fresh Date on every render; the day it represents is what
    // matters and that is fixed for the life of the dialog.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekStart, weekOffset])

  const refresh = useCallback(async () => {
    const first = days[0]
    const last = days[days.length - 1]
    if (!first || !last) return
    const result = await listMealPlan(isoDate(first), isoDate(last))
    if (result.ok) setEntries(result.data)
  }, [days])

  useEffect(() => {
    void refresh()
  }, [refresh])

  /** What is already in the chosen slot on a day. The collision that matters:
   *  a second dinner is a decision, a dinner beside a breakfast is not. */
  const takenOn = useCallback(
    (day: Date) =>
      entries
        .filter((entry) => entry.plan_date === isoDate(day) && entry.slot === slot)
        .sort((a, b) => a.position - b.position),
    [entries, slot],
  )

  async function add() {
    if (busy) return
    setBusy(true)
    setError(null)
    const result = await addMealPlanEntry({
      plan_date: isoDate(selected),
      slot,
      recipe_id: recipe.id,
    })
    setBusy(false)
    if (!result.ok) {
      setError(errorMessage(result.data, 'That could not be added to the plan.'))
      return
    }
    onAdded(result.data)
  }

  const slotLabel = SLOTS.find((option) => option.key === slot)?.label ?? 'Dinner'
  const dayLabel = selected.toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })

  return (
    <Modal
      title={`Add “${recipe.title}” to the plan`}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          {/* Names the whole choice. A bare "Add" beside a grid of fourteen
              days makes you look back up to check what you picked. */}
          <button type="button" className="button" onClick={() => void add()} disabled={busy}>
            Add to {slotLabel.toLowerCase()} on {dayLabel}
          </button>
        </>
      }
    >
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      <div className="field">
        <span className="field-label" id="plan-slot-label">
          Which meal
        </span>
        <div className="segmented" role="group" aria-labelledby="plan-slot-label">
          {SLOTS.map((option) => (
            <button
              key={option.key}
              type="button"
              aria-pressed={slot === option.key}
              onClick={() => setSlot(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="plan-picker-nav">
        <button type="button" onClick={() => setWeekOffset((current) => current - 1)}>
          ‹<span className="visually-hidden"> earlier fortnight</span>
        </button>
        <strong>{rangeLabel(days)}</strong>
        <button type="button" onClick={() => setWeekOffset((current) => current + 1)}>
          ›<span className="visually-hidden"> later fortnight</span>
        </button>
        {weekOffset !== 0 && (
          <button type="button" className="link-button" onClick={() => setWeekOffset(0)}>
            This week
          </button>
        )}
      </div>

      <div className="plan-picker-grid">
        {days.map((day) => {
          const taken = takenOn(day)
          return (
            <button
              key={day.toISOString()}
              type="button"
              className="plan-day"
              data-selected={isSameDay(day, selected)}
              data-today={isSameDay(day, today)}
              // Still selectable: somebody writing up what they actually cooked
              // last night is a real thing to want. Quieter, not refused.
              data-past={day < today}
              data-taken={taken.length > 0}
              aria-pressed={isSameDay(day, selected)}
              aria-label={`${day.toDateString()}${
                taken.length
                  ? `, ${slotLabel.toLowerCase()}: ${taken.map((entry) => entry.title).join(', ')}`
                  : `, no ${slotLabel.toLowerCase()} planned`
              }`}
              onClick={() => setSelected(day)}
            >
              <span className="plan-day-head">
                <span className="plan-day-weekday">
                  {day.toLocaleDateString(undefined, { weekday: 'short' })}
                </span>
                <span className="plan-day-date tabular">{day.getDate()}</span>
              </span>

              {/* The titles where there is room for them, and a mark where
                  there is not — seven columns on a phone is about 45px, which
                  fits a date and nothing else. */}
              <span className="plan-day-taken" aria-hidden="true">
                {taken.map((entry) => entry.title).join(', ')}
              </span>
              <span className="plan-day-mark" aria-hidden="true" />
            </button>
          )
        })}
      </div>

      <p className="field-hint">
        Showing what is already down for {slotLabel.toLowerCase()}. Adding does
        not replace anything — a day can hold more than one.
      </p>
    </Modal>
  )
}

/**
 * "August 9 – 22", and across a boundary "August 28 – September 10".
 *
 * `formatRange` rather than two formatted dates with a dash between them: it
 * knows to drop the repeated month, and it knows where the month goes, which
 * hand-assembly gets wrong the moment the locale is not the one it was written
 * in. Assembling it by hand produced "9 – August 22".
 */
function rangeLabel(days: readonly Date[]): string {
  const first = days[0]
  const last = days[days.length - 1]
  if (!first || !last) return ''

  const format = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long' })
  return format.formatRange(first, last)
}
