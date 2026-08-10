// Reading a recipe (SPEC §4.6).
//
// The servings control is the feature here: "changing servings recalculates
// ingredient quantities". It rescales what is drawn and never touches what is
// stored — the recipe as written stays as written, so halving it to cook
// tonight does not quietly rewrite it for everyone.

import { useEffect, useState } from 'react'

import { recipeImageUrl, type Recipe } from '../api/client'
import { labelledDuration } from '../lib/duration'
import { formatAmount } from '../lib/units'
import { AddToPlan } from './AddToPlan'

const PROGRESS_KEY = 'home-ops:cooking-progress'

/** Which steps are struck off, per recipe. Browser-local; see the note in the
    component for why this is not on the server. */
function loadProgress(recipeId: string): ReadonlySet<string> {
  try {
    const raw = window.localStorage.getItem(`${PROGRESS_KEY}:${recipeId}`)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : [])
  } catch {
    // A private-browsing window, a full quota, or something else's data under
    // our key. None of those are a reason to fail to show a recipe.
    return new Set()
  }
}

function saveProgress(recipeId: string, done: ReadonlySet<string>): void {
  try {
    const key = `${PROGRESS_KEY}:${recipeId}`
    if (done.size === 0) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, JSON.stringify([...done]))
  } catch {
    // Nothing here is worth interrupting somebody mid-recipe for.
  }
}


export function RecipeView({
  recipe,
  canWrite,
  onBack,
  onEdit,
  onDelete,
}: {
  readonly recipe: Recipe
  readonly canWrite: boolean
  readonly onBack: () => void
  readonly onEdit: () => void
  readonly onDelete: () => void
}) {
  const [servings, setServings] = useState(recipe.servings)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [planning, setPlanning] = useState(false)
  //: What was just planned, in words. The dialog closes on success, so without
  //: this the only feedback is a dialog vanishing — which reads as a cancel.
  const [planned, setPlanned] = useState<string | null>(null)
  const [done, setDone] = useState<ReadonlySet<string>>(() => loadProgress(recipe.id))

  // Kept in the browser, not on the server: how far through the cooking you are
  // is yours, not the household's. Two people cooking the same recipe on
  // different evenings should not strike each other's steps off. It survives a
  // reload, which is the failure that actually happens — a phone locking on the
  // counter and coming back to a list with nothing marked.
  useEffect(() => {
    saveProgress(recipe.id, done)
  }, [recipe.id, done])

  function toggleStep(id: string) {
    setDone((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const doneCount = recipe.steps.filter((step) => done.has(step.id)).length

  const scaled = servings !== recipe.servings
  const total = (recipe.prep_minutes ?? 0) + (recipe.cook_minutes ?? 0)

  return (
    <div className="page">
      <div className="toolbar">
        <button type="button" onClick={onBack} className="link-button">
          ← All recipes
        </button>
        {canWrite && (
          <>
            {/* Ahead of Edit: reading a recipe and deciding to cook it is far
                commoner than reading one and deciding to rewrite it. */}
            <button type="button" className="button" onClick={() => setPlanning(true)}>
              Add to meal plan
            </button>
            <button type="button" onClick={onEdit}>
              Edit
            </button>
            {confirmingDelete ? (
              <>
                <span className="notice">Delete “{recipe.title}” for good?</span>
                <button type="button" onClick={onDelete}>
                  Yes, delete
                </button>
                <button type="button" onClick={() => setConfirmingDelete(false)}>
                  Keep it
                </button>
              </>
            ) : (
              <button type="button" onClick={() => setConfirmingDelete(true)}>
                Delete
              </button>
            )}
          </>
        )}
      </div>

      {planned && (
        <p className="notice" role="status">
          {planned}{' '}
          <button type="button" className="link-button" onClick={() => setPlanned(null)}>
            Dismiss
          </button>
        </p>
      )}

      {planning && (
        <AddToPlan
          recipe={recipe}
          onClose={() => setPlanning(false)}
          onAdded={(entry) => {
            setPlanning(false)
            // The day back in words rather than as the `YYYY-MM-DD` that was
            // sent: the confirmation should read like the choice that was made.
            const day = new Date(`${entry.plan_date}T00:00:00`)
            setPlanned(
              `Added to ${entry.slot} on ${day.toLocaleDateString(undefined, {
                weekday: 'long',
                day: 'numeric',
                month: 'long',
              })}.`,
            )
          }}
        />
      )}

      <article className="recipe">
        <header className="recipe-head">
          <div>
            <h1>{recipe.title}</h1>
            {recipe.description && <p className="recipe-description">{recipe.description}</p>}

            <p className="recipe-meta tabular">
              {[
                labelledDuration(recipe.prep_minutes, 'prep'),
                labelledDuration(recipe.cook_minutes, 'cooking'),
                labelledDuration(total, 'total'),
              ]
                .filter(Boolean)
                .join(' · ')}
            </p>

            {recipe.tags.length > 0 && (
              <p className="recipe-card-tags">
                {recipe.tags.map((name) => (
                  <span key={name} className="badge">
                    {name}
                  </span>
                ))}
              </p>
            )}

            {recipe.source_url && (
              <p className="recipe-source">
                {/* noreferrer as well as noopener: a recipe's source is somebody
                    else's site and does not need to know where the click came
                    from. */}
                <a href={recipe.source_url} target="_blank" rel="noopener noreferrer">
                  Original recipe
                </a>
              </p>
            )}
          </div>

          {recipe.image_key && (
            <img
              className="recipe-photo"
              src={recipeImageUrl(recipe.id, recipe.image_key)}
              alt={recipe.title}
            />
          )}
        </header>

        <div className="recipe-columns">
          <section className="recipe-ingredients">
            <div className="recipe-section-head">
              <h2>Ingredients</h2>
              <div className="servings" role="group" aria-label="Servings">
                <button
                  type="button"
                  onClick={() => setServings((n) => Math.max(1, n - 1))}
                  aria-label="Fewer servings"
                  disabled={servings <= 1}
                >
                  −
                </button>
                <span className="tabular">
                  Serves <strong>{servings}</strong>
                </span>
                <button
                  type="button"
                  onClick={() => setServings((n) => Math.min(100, n + 1))}
                  aria-label="More servings"
                >
                  +
                </button>
              </div>
            </div>

            {scaled && (
              <p className="notice" data-testid="scaled-notice">
                Scaled from {recipe.servings}. The saved recipe is unchanged.
                <button type="button" className="link-button" onClick={() => setServings(recipe.servings)}>
                  Reset
                </button>
              </p>
            )}

            {recipe.ingredients.length === 0 ? (
              <p className="empty">No ingredients listed.</p>
            ) : (
              <ul className="ingredient-list">
                {recipe.ingredients.map((row) => (
                  <li key={row.id}>
                    <span className="ingredient-amount tabular">
                      {formatAmount(
                        row.quantity === null ? null : Number(row.quantity),
                        row.unit,
                        recipe.servings,
                        servings,
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

          <section className="recipe-steps">
            <div className="recipe-section-head">
              <h2>Method</h2>
              {recipe.steps.length > 0 && (
                <p className="step-progress">
                  <span className="tabular">
                    {doneCount} of {recipe.steps.length} done
                  </span>
                  {doneCount > 0 && (
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setDone(new Set())}
                    >
                      Start again
                    </button>
                  )}
                </p>
              )}
            </div>

            {recipe.steps.length === 0 ? (
              <p className="empty">No method written down.</p>
            ) : (
              <ol className="step-list">
                {recipe.steps.map((step) => {
                  const isDone = done.has(step.id)
                  return (
                    <li key={step.id} data-done={isDone}>
                      {/* The whole step is the target. Cooking means wet hands
                          and a phone propped on the counter, which is no place
                          for a 16px checkbox. */}
                      <button
                        type="button"
                        onClick={() => toggleStep(step.id)}
                        aria-pressed={isDone}
                        title={isDone ? 'Bring this step back' : 'Strike this step off'}
                      >
                        <span className="step-text">{step.body}</span>
                        <span className="visually-hidden">
                          {isDone ? ' — done, select to bring it back' : ' — select when done'}
                        </span>
                      </button>
                    </li>
                  )
                })}
              </ol>
            )}
          </section>
        </div>
      </article>
    </div>
  )
}
