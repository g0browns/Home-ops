// Recipes (SPEC §4.6, phase 5a).
//
// Three states in one page: a list of cards, one recipe open to read, and the
// editor. A recipe is a document — you look at it while your hands are covered
// in flour — so reading it is the default and editing is the detour, not the
// other way round.

import { useCallback, useEffect, useState } from 'react'

import {
  deleteRecipe,
  errorMessage,
  getRecipe,
  importRecipe,
  listRecipeTags,
  listRecipes,
  recipeImageUrl,
  type CurrentUser,
  type ImportedRecipe,
  type Recipe,
  type RecipeSummary,
} from '../api/client'
import { formatDuration } from '../lib/duration'
import { MealieImport } from './MealieImport'
import { MealPlanPage } from './MealPlanPage'
import { RecipeEditor } from './RecipeEditor'
import { RecipeView } from './RecipeView'

// Recipes and the plan are two views of one module rather than two nav items:
// §4.6 calls the Kitchen one subsystem, and the planner is only useful beside
// the shelf you drag from. Shopping left in Phase 6 and is its own section now
// (§4.12) — a household buys things that have nothing to do with a recipe.
const KITCHEN_VIEWS = [
  ['recipes', 'Recipes'],
  ['plan', 'Meal plan'],
] as const

type KitchenView = (typeof KITCHEN_VIEWS)[number][0]

type Mode =
  | { readonly kind: 'list' }
  | { readonly kind: 'read'; readonly recipe: Recipe }
  | { readonly kind: 'edit'; readonly recipe: Recipe | null }
  // A draft read off a web page. It is a separate mode from 'edit' because
  // nothing has been saved: cancelling here throws the import away rather than
  // returning to a record.
  | { readonly kind: 'import'; readonly draft: ImportedRecipe }

export function KitchenPage({ me }: { readonly me: CurrentUser }) {
  const [recipes, setRecipes] = useState<RecipeSummary[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [tag, setTag] = useState('')
  const [view, setView] = useState<KitchenView>('recipes')
  const [mode, setMode] = useState<Mode>({ kind: 'list' })
  const [loading, setLoading] = useState(true)
  const [importUrl, setImportUrl] = useState('')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)

  const canWrite = me.permissions['kitchen'] === 'write'

  const refresh = useCallback(async () => {
    setLoading(true)
    const [found, knownTags] = await Promise.all([
      listRecipes({ search: search.trim() || undefined, tag: tag || undefined }),
      listRecipeTags(),
    ])
    setLoading(false)
    if (found.ok) setRecipes(found.data)
    if (knownTags.ok) setTags(knownTags.data)
  }, [search, tag])

  useEffect(() => {
    // Debounced, so typing does not fire a request per keystroke.
    const timer = setTimeout(() => void refresh(), 150)
    return () => clearTimeout(timer)
    // `view` is in here on purpose even though `refresh` does not use it:
    // coming back from the planner has to re-read the shelf. Without it the
    // list is whatever it was when the page mounted, so a recipe created from
    // a planned meal is simply not there until a reload.
  }, [refresh, view])

  async function open(id: string) {
    const result = await getRecipe(id)
    if (result.ok) setMode({ kind: 'read', recipe: result.data })
  }

  async function reopen(id: string) {
    const result = await getRecipe(id)
    setMode(result.ok ? { kind: 'read', recipe: result.data } : { kind: 'list' })
    await refresh()
  }

  async function runImport(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const url = importUrl.trim()
    if (!url) return

    setImporting(true)
    setImportError(null)
    const result = await importRecipe(url)
    setImporting(false)

    if (result.ok) {
      setImportUrl('')
      setMode({ kind: 'import', draft: result.data })
    } else {
      // The server's message is the useful one: it says whether the address was
      // refused, unreachable, or simply had no recipe on it. Replacing it with
      // "import failed" would send somebody hunting for a typo that is not
      // there.
      setImportError(errorMessage(result.data, 'Could not import that page.'))
    }
  }

  if (view !== 'recipes') {
    // The same page frame as the recipe shelf, so the header does not vanish
    // when you switch views.
    return (
      <div className="page">
        <div className="page-head">
          <h1>Kitchen</h1>
        </div>
        <div className="toolbar kitchen-views">
          <ViewToggle view={view} onChange={setView} />
        </div>
        <MealPlanPage me={me} />
      </div>
    )
  }

  // The editor, wherever it is opened from. Rendered beside a page rather than
  // in place of one, because it is a modal: something has to be behind it.
  const editor =
    mode.kind === 'edit' || mode.kind === 'import' ? (
      <RecipeEditor
        recipe={mode.kind === 'edit' ? mode.recipe : null}
        {...(mode.kind === 'import' ? { imported: mode.draft } : {})}
        onCancel={() =>
          mode.kind === 'edit' && mode.recipe
            ? void reopen(mode.recipe.id)
            : setMode({ kind: 'list' })
        }
        onSaved={(saved) => void reopen(saved.id)}
      />
    ) : null

  // Editing an existing recipe keeps that recipe on screen behind the modal,
  // rather than dropping you back to the shelf you are not looking at.
  const reading =
    mode.kind === 'read' ? mode.recipe : mode.kind === 'edit' ? mode.recipe : null

  if (reading) {
    return (
      <>
        <RecipeView
          recipe={reading}
          canWrite={canWrite}
          onBack={() => {
            setMode({ kind: 'list' })
            void refresh()
          }}
          onEdit={() => setMode({ kind: 'edit', recipe: reading })}
          onDelete={async () => {
            await deleteRecipe(reading.id)
            setMode({ kind: 'list' })
            await refresh()
          }}
        />
        {editor}
      </>
    )
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Kitchen</h1>
        <p className="page-summary">
          <strong>{recipes.length}</strong> {recipes.length === 1 ? 'recipe' : 'recipes'}
        </p>
      </div>

      <div className="toolbar">
        <ViewToggle view={view} onChange={setView} />

        <input
          type="search"
          className="search"
          placeholder="Search recipes…"
          aria-label="Search recipes"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
        />

        <label className="visually-hidden" htmlFor="recipe-tag">
          Filter by tag
        </label>
        <select id="recipe-tag" value={tag} onChange={(event) => setTag(event.currentTarget.value)}>
          <option value="">All tags</option>
          {tags.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        {canWrite && (
          <button type="button" className="button" onClick={() => setMode({ kind: 'edit', recipe: null })}>
            New recipe
          </button>
        )}
      </div>

      {canWrite && (
        <form className="quick-add" onSubmit={runImport}>
          <div className="field" style={{ flex: '1 1 22rem' }}>
            <label htmlFor="import-url">Import from a web page</label>
            <input
              id="import-url"
              type="url"
              inputMode="url"
              placeholder="https://example.com/recipes/dal"
              value={importUrl}
              onChange={(event) => setImportUrl(event.currentTarget.value)}
            />
          </div>
          <button type="submit" className="button" disabled={importing || !importUrl.trim()}>
            {importing ? 'Reading…' : 'Import'}
          </button>
        </form>
      )}

      {importError && (
        <p className="alert" role="alert">
          {importError}
        </p>
      )}

      {canWrite && <MealieImport onImported={() => void refresh()} />}

      {loading && recipes.length === 0 ? (
        <p className="loading">Loading…</p>
      ) : recipes.length === 0 ? (
        <p className="empty">
          {search || tag ? 'Nothing matches that.' : 'No recipes yet.'}
        </p>
      ) : (
        <ul className="recipe-grid">
          {recipes.map((recipe) => (
            <li key={recipe.id}>
              <button type="button" className="recipe-card" onClick={() => void open(recipe.id)}>
                <span className="recipe-thumb">
                  {recipe.image_key ? (
                    <img
                      src={recipeImageUrl(recipe.id, recipe.image_key, true)}
                      alt=""
                      loading="lazy"
                    />
                  ) : (
                    // Not an empty box: a recipe without a picture is normal and
                    // should not look like one that failed to load.
                    <span className="recipe-thumb-empty" aria-hidden="true">
                      ▤
                    </span>
                  )}
                </span>
                <span className="recipe-card-body">
                  <span className="recipe-card-title">{recipe.title}</span>
                  <span className="recipe-card-meta tabular">
                    {[`Serves ${recipe.servings}`, formatDuration(totalTime(recipe))]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                  {recipe.tags.length > 0 && (
                    <span className="recipe-card-tags">
                      {recipe.tags.map((name) => (
                        <span key={name} className="badge">
                          {name}
                        </span>
                      ))}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Over the shelf: a new recipe or an import has no record to sit on
          top of, so this is what is behind it. */}
      {editor}
    </div>
  )
}

function ViewToggle({
  view,
  onChange,
}: {
  readonly view: KitchenView
  readonly onChange: (next: KitchenView) => void
}) {
  return (
    <div className="segmented" role="group" aria-label="Kitchen view">
      {KITCHEN_VIEWS.map(([option, label]) => (
        <button
          key={option}
          type="button"
          aria-pressed={view === option}
          onClick={() => onChange(option)}
        >
          {label}
        </button>
      ))}
    </div>
  )
}


function totalTime(recipe: RecipeSummary): number | null {
  const total = (recipe.prep_minutes ?? 0) + (recipe.cook_minutes ?? 0)
  return total > 0 ? total : null
}
