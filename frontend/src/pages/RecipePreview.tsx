// A quick look at a recipe without leaving the planner (SPEC §4.6).
//
// The point is deciding: you are staring at a shelf of fifty recipes trying to
// work out what Thursday is, and the titles alone do not settle it. So the
// preview shows the picture, the timings and enough of the ingredients and
// method to choose — and offers to plan it, because a preview that makes you
// close it and go back to the row you came from has stopped halfway.
//
// Built on the native `<dialog>` rather than a div with a high z-index. That
// gets focus trapping, Escape, returning focus to whatever opened it, and an
// inert background from the browser — all of which are easy to hand-roll badly
// and tedious to hand-roll well. It is also not a secure-context API, which
// matters: SPEC §2.1 has two of the three access paths on plain HTTP.

import { useEffect, useRef, useState } from 'react'

import {
  addMealPlanEntry,
  getRecipe,
  recipeImageUrl,
  type MealSlot,
  type Recipe,
} from '../api/client'
import { isoDate } from '../lib/dates'
import { labelledDuration } from '../lib/duration'
import { formatAmount } from '../lib/units'

export function RecipePreview({
  recipeId,
  days,
  canPlan,
  onClose,
  onPlanned,
}: {
  readonly recipeId: string
  /** The week on screen, so the preview can plan straight into it. */
  readonly days: readonly Date[]
  readonly canPlan: boolean
  readonly onClose: () => void
  readonly onPlanned: () => Promise<void>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [recipe, setRecipe] = useState<Recipe | null>(null)
  const [failed, setFailed] = useState(false)
  const [planning, setPlanning] = useState(false)

  useEffect(() => {
    // `showModal` rather than `show`: the modal behaviour — focus trap, inert
    // background, Escape — only comes with the former.
    const node = dialog.current
    if (node && !node.open) node.showModal()
  }, [])

  useEffect(() => {
    let cancelled = false
    setRecipe(null)
    setFailed(false)
    void getRecipe(recipeId).then((result) => {
      if (cancelled) return
      if (result.ok) setRecipe(result.data)
      else setFailed(true)
    })
    return () => {
      cancelled = true
    }
  }, [recipeId])

  async function planFor(date: string) {
    setPlanning(true)
    await addMealPlanEntry({ plan_date: date, slot: 'dinner' as MealSlot, recipe_id: recipeId })
    setPlanning(false)
    await onPlanned()
    dialog.current?.close()
  }

  return (
    <dialog
      ref={dialog}
      className="preview"
      aria-labelledby="preview-title"
      // Fires for the close button, for Escape, and for the backdrop click
      // below — one exit rather than three that have to be kept in step.
      onClose={onClose}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself, because
        // the backdrop is a pseudo-element and has no separate target. Anything
        // inside stops here.
        if (event.target === dialog.current) dialog.current?.close()
      }}
    >
      {failed ? (
        <div className="preview-body">
          <p className="alert" role="alert">
            That recipe could not be opened.
          </p>
        </div>
      ) : !recipe ? (
        <div className="preview-body">
          <p className="loading">Loading…</p>
        </div>
      ) : (
        <>
          <header className="preview-head">
            {recipe.image_key && (
              <img
                className="preview-photo"
                src={recipeImageUrl(recipe.id, recipe.image_key)}
                alt=""
              />
            )}
            <div>
              <h2 id="preview-title">{recipe.title}</h2>
              <p className="recipe-meta tabular">
                {[
                  `Serves ${recipe.servings}`,
                  labelledDuration(recipe.prep_minutes, 'prep'),
                  labelledDuration(recipe.cook_minutes, 'cooking'),
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
              {recipe.description && <p className="recipe-description">{recipe.description}</p>}
              {recipe.tags.length > 0 && (
                <p className="recipe-card-tags">
                  {recipe.tags.slice(0, 6).map((tag) => (
                    <span key={tag} className="badge">
                      {tag}
                    </span>
                  ))}
                </p>
              )}
            </div>
          </header>

          <div className="preview-body">
            <section>
              <h3>Ingredients</h3>
              {recipe.ingredients.length === 0 ? (
                <p className="empty">None listed.</p>
              ) : (
                <ul className="ingredient-list">
                  {recipe.ingredients.map((row) => (
                    <li key={row.id}>
                      <span className="ingredient-amount tabular">
                        {formatAmount(
                          row.quantity === null ? null : Number(row.quantity),
                          row.unit,
                          recipe.servings,
                          recipe.servings,
                        )}
                      </span>
                      <span className="ingredient-name">
                        {row.name}
                        {row.note && <span className="muted">, {row.note}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3>Method</h3>
              {recipe.steps.length === 0 ? (
                <p className="empty">None written down.</p>
              ) : (
                <ol className="step-list preview-steps">
                  {recipe.steps.map((step) => (
                    <li key={step.id}>{step.body}</li>
                  ))}
                </ol>
              )}
            </section>
          </div>
        </>
      )}

      <footer className="preview-actions">
        {canPlan && recipe && (
          <>
            <label className="visually-hidden" htmlFor="preview-plan">
              Plan {recipe.title} for a day
            </label>
            <select
              id="preview-plan"
              value=""
              disabled={planning}
              onChange={(event) => {
                const value = event.currentTarget.value
                if (value) void planFor(value)
              }}
            >
              <option value="">Plan for…</option>
              {days.map((day) => (
                <option key={day.toISOString()} value={isoDate(day)}>
                  {day.toLocaleDateString(undefined, { weekday: 'long' })}
                </option>
              ))}
            </select>
          </>
        )}
        <button type="button" className="button" onClick={() => dialog.current?.close()}>
          Close
        </button>
      </footer>
    </dialog>
  )
}
