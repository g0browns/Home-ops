// The weekly meal planner (SPEC §4.6, phase 5c).
//
// Seven day columns, honouring the household's week start like the calendar
// does. Within a day, only the slots that are actually in use are drawn, plus
// dinner — the household's own Mealie history is 58 dinners and 18 sides and not
// one breakfast, and a grid with two permanently empty rows is a grid that
// teaches you to ignore two rows.
//
// Drag and drop follows the two rules this project learned the hard way:
//
//   * **Resolve the dragged item from `dataTransfer`, never from React state.**
//     State set in `dragstart` is not guaranteed visible to the drop handler
//     before a re-render, so a fast drag arrives before the component knows
//     anything is happening.
//   * **Every drag has a keyboard equivalent.** HTML5 drag-and-drop is
//     pointer-only, so a drag-only planner simply does not exist for anyone
//     navigating by keyboard. Each entry carries move controls.

import { useCallback, useEffect, useMemo, useState, type DragEvent } from 'react'

import {
  addMealPlanEntry,
  getHouseholdSettings,
  listRecipes,
  moveMealPlanEntry,
  recipeImageUrl,
  removeMealPlanEntry,
  savePlannedMealAsRecipe,
  listMealPlan,
  type CurrentUser,
  type MealPlanEntry,
  type MealSlot,
  type RecipeSummary,
} from '../api/client'
import { RecipePreview } from './RecipePreview'
import {
  addDays,
  formatMonth,
  isoDate,
  isSameDay,
  isWeekStart,
  startOfDay,
  startOfWeek,
  type WeekStart,
} from '../lib/dates'

const SLOTS: { key: MealSlot; label: string }[] = [
  { key: 'breakfast', label: 'Breakfast' },
  { key: 'lunch', label: 'Lunch' },
  { key: 'dinner', label: 'Dinner' },
  { key: 'side', label: 'Side' },
]

const DRAG_TYPE = 'application/x-home-ops'

export function MealPlanPage({ me }: { readonly me: CurrentUser }) {
  const [weekStart, setWeekStart] = useState<WeekStart>('monday')
  const [anchor, setAnchor] = useState(() => startOfDay(new Date()))
  const [entries, setEntries] = useState<MealPlanEntry[]>([])
  const [recipes, setRecipes] = useState<RecipeSummary[]>([])
  const [search, setSearch] = useState('')
  const [showAllSlots, setShowAllSlots] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [over, setOver] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const canWrite = me.permissions['kitchen'] === 'write'

  const days = useMemo(() => {
    const first = startOfWeek(anchor, weekStart)
    return Array.from({ length: 7 }, (_, index) => addDays(first, index))
  }, [anchor, weekStart])

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

  const refresh = useCallback(async () => {
    const first = days[0]
    const last = days[days.length - 1]
    if (!first || !last) return
    setLoading(true)
    const result = await listMealPlan(isoDate(first), isoDate(last))
    setLoading(false)
    if (result.ok) setEntries(result.data)
  }, [days])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    const timer = setTimeout(() => {
      void listRecipes({ search: search.trim() || undefined }).then((result) => {
        if (result.ok) setRecipes(result.data)
      })
    }, 150)
    return () => clearTimeout(timer)
  }, [search])

  // Slots that are in use this week, plus dinner, which is always offered.
  const visibleSlots = useMemo(() => {
    if (showAllSlots) return SLOTS
    const used = new Set(entries.map((entry) => entry.slot))
    used.add('dinner')
    return SLOTS.filter((slot) => used.has(slot.key))
  }, [entries, showAllSlots])

  const cellFor = useCallback(
    (day: Date, slot: MealSlot) =>
      entries
        .filter((entry) => entry.plan_date === isoDate(day) && entry.slot === slot)
        .sort((a, b) => a.position - b.position),
    [entries],
  )

  async function drop(event: DragEvent<HTMLElement>, day: Date, slot: MealSlot) {
    event.preventDefault()
    setOver(null)
    setDragging(null)

    // From dataTransfer, never from state: state set in dragstart is not
    // guaranteed visible here before a re-render.
    const payload = event.dataTransfer.getData(DRAG_TYPE) || event.dataTransfer.getData('text/plain')
    if (!payload) return

    const [kind, id] = payload.split(':')
    if (!id) return

    if (kind === 'recipe') {
      await addMealPlanEntry({ plan_date: isoDate(day), slot, recipe_id: id })
    } else if (kind === 'entry') {
      await moveMealPlanEntry(id, { plan_date: isoDate(day), slot })
    }
    await refresh()
  }

  async function moveBy(entry: MealPlanEntry, deltaDays: number) {
    const current = new Date(`${entry.plan_date}T00:00:00`)
    await moveMealPlanEntry(entry.id, {
      plan_date: isoDate(addDays(current, deltaDays)),
      slot: entry.slot,
    })
    await refresh()
  }

  async function moveToSlot(entry: MealPlanEntry, slot: MealSlot) {
    await moveMealPlanEntry(entry.id, { plan_date: entry.plan_date, slot })
    await refresh()
  }

  const first = days[0]
  const today = startOfDay(new Date())

  return (
    // No page wrapper and no <h1>: the Kitchen page owns both, so switching to
    // the planner keeps the header you switched from rather than replacing it
    // with a month.
    <>
      <div className="toolbar">
        <div className="month-nav" role="group" aria-label="Change week">
          <button type="button" onClick={() => setAnchor(addDays(anchor, -7))} aria-label="Previous week">
            ‹
          </button>
          <button type="button" onClick={() => setAnchor(startOfDay(new Date()))}>
            This week
          </button>
          <button type="button" onClick={() => setAnchor(addDays(anchor, 7))} aria-label="Next week">
            ›
          </button>
        </div>

        <span className="planner-range tabular">
          {first ? formatMonth(first) : ''}
        </span>

        <label className="check">
          <input
            type="checkbox"
            checked={showAllSlots}
            onChange={(event) => setShowAllSlots(event.currentTarget.checked)}
          />
          Show every meal
        </label>

        <span className="page-summary">
          <strong>{entries.length}</strong> {entries.length === 1 ? 'meal' : 'meals'} planned
        </span>
      </div>

      <div className="planner">
        <div className="planner-grid" style={{ ['--slot-count' as string]: visibleSlots.length }}>
          {days.map((day) => (
            <div key={isoDate(day)} className="planner-day" data-today={isSameDay(day, today)}>
              <h2 className="planner-day-head">
                <span>{day.toLocaleDateString(undefined, { weekday: 'short' })}</span>
                <span className="tabular muted">{day.getDate()}</span>
              </h2>

              {visibleSlots.map((slot) => {
                const key = `${isoDate(day)}:${slot.key}`
                const planned = cellFor(day, slot.key)
                return (
                  <section
                    key={slot.key}
                    className="planner-slot"
                    data-receiving={over === key}
                    aria-label={`${slot.label} on ${day.toDateString()}`}
                    onDragOver={(event) => {
                      if (!canWrite) return
                      event.preventDefault()
                      setOver(key)
                    }}
                    onDragLeave={() => setOver((current) => (current === key ? null : current))}
                    onDrop={(event) => void drop(event, day, slot.key)}
                  >
                    <h3>{slot.label}</h3>

                    {planned.map((entry) => (
                      <article
                        key={entry.id}
                        className="planned"
                        data-hidden={entry.hidden_recipe}
                        data-dragging={dragging === entry.id}
                        draggable={canWrite}
                        onDragStart={(event) => {
                          event.dataTransfer.setData(DRAG_TYPE, `entry:${entry.id}`)
                          event.dataTransfer.setData('text/plain', `entry:${entry.id}`)
                          event.dataTransfer.effectAllowed = 'move'
                          setDragging(entry.id)
                        }}
                        onDragEnd={() => setDragging(null)}
                      >
                        {entry.recipe_id && entry.image_key && (
                          <img
                            className="planned-thumb"
                            src={recipeImageUrl(entry.recipe_id, entry.image_key, true)}
                            alt=""
                            loading="lazy"
                          />
                        )}
                        <span className="planned-title">{entry.title}</span>

                        {canWrite && (
                          // The keyboard path. HTML5 drag is pointer-only, so
                          // without these the planner does not exist for anyone
                          // navigating by keyboard.
                          <span className="row-actions">
                            <button
                              type="button"
                              onClick={() => void moveBy(entry, -1)}
                              aria-label={`Move ${entry.title} a day earlier`}
                            >
                              ←
                            </button>
                            <button
                              type="button"
                              onClick={() => void moveBy(entry, 1)}
                              aria-label={`Move ${entry.title} a day later`}
                            >
                              →
                            </button>
                            <label className="visually-hidden" htmlFor={`slot-${entry.id}`}>
                              Meal for {entry.title}
                            </label>
                            <select
                              id={`slot-${entry.id}`}
                              value={entry.slot}
                              onChange={(event) =>
                                void moveToSlot(entry, event.currentTarget.value as MealSlot)
                              }
                            >
                              {SLOTS.map((option) => (
                                <option key={option.key} value={option.key}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            {!entry.recipe_id && !entry.hidden_recipe && (
                              // §4.6: a planned meal can be saved back as a
                              // recipe. Only offered for the free-text ones,
                              // because that is the case it exists for.
                              <button
                                type="button"
                                onClick={async () => {
                                  await savePlannedMealAsRecipe(entry.id)
                                  await refresh()
                                }}
                                aria-label={`Save ${entry.title} as a recipe`}
                                title="Save as a recipe"
                              >
                                ↧
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={async () => {
                                await removeMealPlanEntry(entry.id)
                                await refresh()
                              }}
                              aria-label={`Take ${entry.title} off the plan`}
                            >
                              ×
                            </button>
                          </span>
                        )}
                      </article>
                    ))}

                    {canWrite && (
                      <form
                        className="planner-add"
                        onSubmit={async (event) => {
                          event.preventDefault()
                          const input = new FormData(event.currentTarget).get('title')
                          const title = String(input ?? '').trim()
                          if (!title) return
                          event.currentTarget.reset()
                          await addMealPlanEntry({ plan_date: isoDate(day), slot: slot.key, title })
                          await refresh()
                        }}
                      >
                        <label className="visually-hidden" htmlFor={`add-${key}`}>
                          Add to {slot.label} on {day.toDateString()}
                        </label>
                        <input
                          id={`add-${key}`}
                          name="title"
                          placeholder="Add…"
                          autoComplete="off"
                        />
                      </form>
                    )}
                  </section>
                )
              })}
            </div>
          ))}
        </div>

        {canWrite && (
          <aside className="planner-picker" aria-label="Recipes to plan">
            <h2>Recipes</h2>
            <input
              type="search"
              className="search"
              placeholder="Search recipes…"
              aria-label="Search recipes to plan"
              value={search}
              onChange={(event) => setSearch(event.currentTarget.value)}
            />
            <p className="reorder-hint">
              Drag one onto a day, or type straight into a slot for something that is not
              a recipe.
            </p>
            {loading && entries.length === 0 ? (
              <p className="loading">Loading…</p>
            ) : (
              <ul className="picker-list">
                {recipes.slice(0, 40).map((recipe) => (
                  <li key={recipe.id}>
                    <div
                      className="picker-item"
                      draggable
                      onDragStart={(event) => {
                        event.dataTransfer.setData(DRAG_TYPE, `recipe:${recipe.id}`)
                        event.dataTransfer.setData('text/plain', `recipe:${recipe.id}`)
                        event.dataTransfer.effectAllowed = 'copy'
                      }}
                    >
                      {recipe.image_key && (
                        <img
                          className="planned-thumb"
                          src={recipeImageUrl(recipe.id, recipe.image_key, true)}
                          alt=""
                          loading="lazy"
                        />
                      )}
                      {/* The name opens a preview. Deciding what Thursday is
                          from a list of fifty titles is the thing this page is
                          actually for, and a title alone rarely settles it.
                          A button inside a draggable row still drags. */}
                      <button
                        type="button"
                        className="planned-title picker-name"
                        onClick={() => setPreview(recipe.id)}
                        title={`Look at ${recipe.title}`}
                      >
                        {recipe.title}
                      </button>
                      <PlanNow recipe={recipe} days={days} onPlanned={refresh} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        )}
      </div>

      {preview && (
        <RecipePreview
          recipeId={preview}
          days={days}
          canPlan={canWrite}
          onClose={() => setPreview(null)}
          onPlanned={refresh}
        />
      )}
    </>
  )
}

/**
 * The keyboard equivalent of dragging a recipe onto a day.
 *
 * A select rather than seven buttons: the picker is a long list, and seven
 * controls per row would be a wall of them.
 */
function PlanNow({
  recipe,
  days,
  onPlanned,
}: {
  readonly recipe: RecipeSummary
  readonly days: readonly Date[]
  readonly onPlanned: () => Promise<void>
}) {
  return (
    <>
      <label className="visually-hidden" htmlFor={`plan-${recipe.id}`}>
        Plan {recipe.title} for a day
      </label>
      <select
        id={`plan-${recipe.id}`}
        value=""
        onChange={async (event) => {
          const value = event.currentTarget.value
          if (!value) return
          event.currentTarget.value = ''
          await addMealPlanEntry({ plan_date: value, slot: 'dinner', recipe_id: recipe.id })
          await onPlanned()
        }}
      >
        <option value="">Plan…</option>
        {days.map((day) => (
          <option key={isoDate(day)} value={isoDate(day)}>
            {day.toLocaleDateString(undefined, { weekday: 'long' })}
          </option>
        ))}
      </select>
    </>
  )
}
