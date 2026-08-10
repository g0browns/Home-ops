// Writing a recipe (SPEC §4.6).
//
// Ingredients are structured rows, not free text — quantity, unit, ingredient,
// note — because §4.6's shopping list has to aggregate them later. That makes
// the editor a small table rather than a textarea, which is more work to build
// and much less work to use than typing "200g red lentils" and hoping something
// parses it correctly.

import { useEffect, useState, type FormEvent } from 'react'

import {
  createRecipe,
  deleteRecipeImage,
  listIngredients,
  recipeImageUrl,
  setRecipeImageFromUrl,
  updateRecipe,
  uploadRecipeImage,
  type ImportedRecipe,
  type IngredientSuggestion,
  type Recipe,
  type Visibility,
} from '../api/client'
import { Modal } from '../components/Modal'
import { UNITS, unitLabel } from '../lib/units'

interface DraftIngredient {
  key: string
  name: string
  quantity: string
  unit: string
  note: string
  /** The line the importer read this from, kept on screen so the cook can see
      what it was guessing from rather than having to trust it or re-type it. */
  raw?: string
  /** The importer found nothing measurable here. Flagged, never hidden. */
  needsChecking?: boolean
}

const VISIBILITIES: { value: Visibility; label: string }[] = [
  { value: 'household', label: 'Everyone in the household' },
  { value: 'assignees', label: 'Only me and the people it is for' },
  { value: 'private', label: 'Only me' },
]

let nextKey = 0
const newKey = () => `row-${nextKey++}`

function toDraft(recipe: Recipe | null, imported: ImportedRecipe | null): DraftIngredient[] {
  if (imported) {
    return imported.ingredients.map((row) => ({
      key: newKey(),
      name: row.name,
      quantity: row.quantity === null ? '' : String(Number(row.quantity)),
      unit: row.unit ?? '',
      note: row.note ?? '',
      raw: row.raw,
      needsChecking: !row.confident,
    }))
  }
  if (!recipe || recipe.ingredients.length === 0) {
    return [{ key: newKey(), name: '', quantity: '', unit: '', note: '' }]
  }
  return recipe.ingredients.map((row) => ({
    key: newKey(),
    name: row.name,
    // Trailing zeros come back from a Numeric column; nobody writes "200.0000 g".
    quantity: row.quantity === null ? '' : String(Number(row.quantity)),
    unit: row.unit ?? '',
    note: row.note ?? '',
  }))
}

export function RecipeEditor({
  recipe,
  imported = null,
  onCancel,
  onSaved,
}: {
  readonly recipe: Recipe | null
  /** A draft read off a web page. Nothing has been saved yet: SPEC 4.6 requires
      the cook to correct the parse before anything is. */
  readonly imported?: ImportedRecipe | null
  readonly onCancel: () => void
  readonly onSaved: (saved: Recipe) => void
}) {
  const [ingredients, setIngredients] = useState<DraftIngredient[]>(() =>
    toDraft(recipe, imported),
  )
  const [steps, setSteps] = useState<string[]>(() => {
    if (imported && imported.steps.length > 0) return imported.steps
    if (recipe && recipe.steps.length > 0) return recipe.steps.map((s) => s.body)
    return ['']
  })
  const [suggestions, setSuggestions] = useState<IngredientSuggestion[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imageKey, setImageKey] = useState<string | null>(recipe?.image_key ?? null)
  const unchecked = ingredients.filter((row) => row.needsChecking).length

  useEffect(() => {
    // The whole vocabulary, small enough to fetch once and let the browser's
    // native datalist do the filtering.
    void listIngredients().then((result) => {
      if (result.ok) setSuggestions(result.data)
    })
  }, [])

  function patchIngredient(key: string, field: keyof DraftIngredient, value: string) {
    setIngredients((rows) =>
      rows.map((row) => (row.key === key ? { ...row, [field]: value } : row)),
    )
  }

  function addIngredient() {
    setIngredients((rows) => [...rows, { key: newKey(), name: '', quantity: '', unit: '', note: '' }])
  }

  function removeIngredient(key: string) {
    setIngredients((rows) => (rows.length === 1 ? rows : rows.filter((row) => row.key !== key)))
  }

  function moveIngredient(index: number, delta: number) {
    // Buttons rather than drag: the ingredient list is an ordered document and
    // must be reorderable by keyboard, the same rule the notes board follows.
    setIngredients((rows) => {
      const target = index + delta
      if (target < 0 || target >= rows.length) return rows
      const copy = [...rows]
      const [moved] = copy.splice(index, 1)
      if (moved) copy.splice(target, 0, moved)
      return copy
    })
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)

    const body = {
      title: String(data.get('title') ?? '').trim(),
      description: String(data.get('description') ?? ''),
      servings: Number(data.get('servings') ?? 4),
      prep_minutes: numberOrNull(data.get('prep_minutes')),
      cook_minutes: numberOrNull(data.get('cook_minutes')),
      source_url: String(data.get('source_url') ?? '').trim() || null,
      visibility: String(data.get('visibility') ?? 'household'),
      tags: String(data.get('tags') ?? '')
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean),
      // Blank rows are how a form with an empty last line behaves; they are not
      // ingredients and must not reach the server.
      ingredients: ingredients
        .filter((row) => row.name.trim())
        .map((row) => ({
          name: row.name.trim(),
          quantity: row.quantity.trim() === '' ? null : row.quantity.trim(),
          unit: row.unit || null,
          note: row.note.trim() || null,
        })),
      steps: steps.map((step) => step.trim()).filter(Boolean),
    }

    setBusy(true)
    setError(null)
    const result = recipe ? await updateRecipe(recipe.id, body) : await createRecipe(body)

    if (result.ok && imported?.image_url) {
      // Only now. Downloading somebody else's photograph is a second request to
      // a second address, and it should not happen while the cook is still
      // looking at a preview they might discard. A failure here does not fail
      // the save: the recipe is worth keeping without the picture.
      await setRecipeImageFromUrl(result.data.id, imported.image_url)
    }

    setBusy(false)
    if (result.ok) onSaved(result.data)
    else setError('Could not save that recipe. Check the quantities and units.')
  }

  async function onPickImage(file: File | undefined) {
    if (!file || !recipe) return
    setBusy(true)
    setError(null)
    const result = await uploadRecipeImage(recipe.id, file)
    setBusy(false)
    if (result.ok) setImageKey(result.data.image_key)
    else setError('That file was not an image we can use. Try a JPEG, PNG or WebP.')
  }

  const heading = imported ? 'Check this import' : recipe ? 'Edit recipe' : 'New recipe'

  return (
    <Modal title={heading} onClose={onCancel} wide labelledBy="recipe-editor-title">
      <form className="note-editor" onSubmit={save} aria-label={heading}>

      {imported && (
        <p className="notice">
          Read from <strong>{hostOf(imported.source_url)}</strong> via {imported.extracted_by}.
          Nothing is saved yet.
          {unchecked > 0 && (
            <>
              {' '}
              <strong>{unchecked}</strong>{' '}
              {unchecked === 1 ? 'ingredient needs' : 'ingredients need'} a look, marked below.
            </>
          )}
        </p>
      )}

      <div className="field">
        <label htmlFor="recipe-title">Title</label>
        <input
          id="recipe-title"
          name="title"
          defaultValue={imported?.title ?? recipe?.title ?? ''}
          required
          autoFocus
        />
      </div>

      <div className="field">
        <label htmlFor="recipe-description">Description</label>
        <textarea
          id="recipe-description"
          name="description"
          rows={2}
          defaultValue={imported?.description ?? recipe?.description ?? ''}
        />
      </div>

      <div className="editor-row">
        <div className="field">
          <label htmlFor="recipe-servings">Serves</label>
          <input
            id="recipe-servings"
            name="servings"
            type="number"
            min={1}
            max={1000}
            defaultValue={imported?.servings ?? recipe?.servings ?? 4}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="recipe-prep">Prep (minutes)</label>
          <input
            id="recipe-prep"
            name="prep_minutes"
            type="number"
            min={0}
            defaultValue={imported?.prep_minutes ?? recipe?.prep_minutes ?? ''}
          />
        </div>
        <div className="field">
          <label htmlFor="recipe-cook">Cooking (minutes)</label>
          <input
            id="recipe-cook"
            name="cook_minutes"
            type="number"
            min={0}
            defaultValue={imported?.cook_minutes ?? recipe?.cook_minutes ?? ''}
          />
        </div>
      </div>

      {/* --- ingredients ------------------------------------------------- */}

      <fieldset className="field ingredient-editor">
        <legend>Ingredients</legend>
        <datalist id="known-ingredients">
          {suggestions.map((row) => (
            <option key={row.id} value={row.name} />
          ))}
        </datalist>

        <ol className="ingredient-rows">
          {ingredients.map((row, index) => (
            <li key={row.key} data-needs-checking={row.needsChecking ? 'true' : undefined}>
              {row.raw && (
                <span className="ingredient-raw" title="The line as the site published it">
                  {row.raw}
                </span>
              )}
              <input
                className="qty"
                type="number"
                step="any"
                min="0"
                placeholder="Qty"
                aria-label={`Quantity for ingredient ${index + 1}`}
                value={row.quantity}
                onChange={(e) => patchIngredient(row.key, 'quantity', e.currentTarget.value)}
              />
              <select
                aria-label={`Unit for ingredient ${index + 1}`}
                value={row.unit}
                onChange={(e) => patchIngredient(row.key, 'unit', e.currentTarget.value)}
              >
                <option value="">(none)</option>
                {UNITS.map((unit) => (
                  <option key={unit.key} value={unit.key}>
                    {unitLabel(unit)}
                  </option>
                ))}
              </select>
              <input
                className="ingredient-name-input"
                list="known-ingredients"
                placeholder="Ingredient"
                aria-label={`Ingredient ${index + 1}`}
                value={row.name}
                onChange={(e) => patchIngredient(row.key, 'name', e.currentTarget.value)}
              />
              <input
                // Just "Note". A worked example repeated down every row reads
                // as data rather than as a hint, which is actively misleading
                // on the import screen where the job is judging what was parsed.
                placeholder="Note"
                aria-label={`Note for ingredient ${index + 1}`}
                value={row.note}
                onChange={(e) => patchIngredient(row.key, 'note', e.currentTarget.value)}
              />
              <span className="row-actions">
                <button
                  type="button"
                  onClick={() => moveIngredient(index, -1)}
                  disabled={index === 0}
                  aria-label={`Move ingredient ${index + 1} up`}
                >
                  ↑
                </button>
                <button
                  type="button"
                  onClick={() => moveIngredient(index, 1)}
                  disabled={index === ingredients.length - 1}
                  aria-label={`Move ingredient ${index + 1} down`}
                >
                  ↓
                </button>
                <button
                  type="button"
                  onClick={() => removeIngredient(row.key)}
                  aria-label={`Remove ingredient ${index + 1}`}
                >
                  ×
                </button>
              </span>
            </li>
          ))}
        </ol>
        <button type="button" onClick={addIngredient}>
          Add ingredient
        </button>
        <span className="field-hint">
          Leave the quantity blank for things measured to taste.
        </span>
      </fieldset>

      {/* --- method ------------------------------------------------------ */}

      <fieldset className="field ingredient-editor">
        <legend>Method</legend>
        <ol className="step-rows">
          {steps.map((step, index) => (
            <li key={index}>
              <textarea
                rows={2}
                placeholder={`Step ${index + 1}`}
                aria-label={`Step ${index + 1}`}
                value={step}
                onChange={(e) => {
                  const value = e.currentTarget.value
                  setSteps((rows) => rows.map((row, i) => (i === index ? value : row)))
                }}
              />
              <span className="row-actions">
                <button
                  type="button"
                  onClick={() =>
                    setSteps((rows) => {
                      if (index === 0) return rows
                      const copy = [...rows]
                      const [moved] = copy.splice(index, 1)
                      if (moved !== undefined) copy.splice(index - 1, 0, moved)
                      return copy
                    })
                  }
                  disabled={index === 0}
                  aria-label={`Move step ${index + 1} up`}
                >
                  ↑
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setSteps((rows) => (rows.length === 1 ? rows : rows.filter((_, i) => i !== index)))
                  }
                  aria-label={`Remove step ${index + 1}`}
                >
                  ×
                </button>
              </span>
            </li>
          ))}
        </ol>
        <button type="button" onClick={() => setSteps((rows) => [...rows, ''])}>
          Add step
        </button>
      </fieldset>

      {/* --- picture ----------------------------------------------------- */}

      <fieldset className="field ingredient-editor">
        <legend>Picture</legend>
        {recipe ? (
          <div className="image-picker">
            {imageKey ? (
              <img className="recipe-thumb-preview" src={recipeImageUrl(recipe.id, imageKey, true)} alt="" />
            ) : (
              <span className="recipe-thumb-empty" aria-hidden="true">
                ▤
              </span>
            )}
            <div>
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                aria-label="Choose a picture"
                onChange={(e) => void onPickImage(e.currentTarget.files?.[0])}
              />
              <span className="field-hint">
                Uploads are re-encoded, which strips location data out of phone
                photographs.
              </span>
              {imageKey && (
                <button
                  type="button"
                  onClick={async () => {
                    const result = await deleteRecipeImage(recipe.id)
                    if (result.ok) setImageKey(null)
                  }}
                >
                  Remove picture
                </button>
              )}
            </div>
          </div>
        ) : imported?.image_url ? (
          <span className="field-hint">
            The page has a picture. It is downloaded when you save, not before.
          </span>
        ) : (
          <span className="field-hint">Save the recipe first, then add a picture.</span>
        )}
      </fieldset>

      <div className="editor-row">
        <div className="field">
          <label htmlFor="recipe-tags">Tags</label>
          <input
            id="recipe-tags"
            name="tags"
            placeholder="vegetarian, quick"
            defaultValue={(imported?.tags ?? recipe?.tags ?? []).join(', ')}
          />
        </div>
        <div className="field">
          <label htmlFor="recipe-source">Source URL</label>
          <input
            id="recipe-source"
            name="source_url"
            defaultValue={imported?.source_url ?? recipe?.source_url ?? ''}
          />
        </div>
        <div className="field">
          <label htmlFor="recipe-visibility">Who can see this</label>
          <select id="recipe-visibility" name="visibility" defaultValue={recipe?.visibility ?? 'household'}>
            {VISIBILITIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
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
          {busy ? 'Saving…' : recipe ? 'Save recipe' : 'Create recipe'}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      </form>
    </Modal>
  )
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www[.]/, '')
  } catch {
    return url
  }
}

function numberOrNull(value: FormDataEntryValue | null): number | null {
  const text = String(value ?? '').trim()
  return text === '' ? null : Number(text)
}
