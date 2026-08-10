// API client.
//
// The base path is relative and must stay that way. The app answers on three
// origins at once — an HTTPS Cloudflare hostname, a tailnet name, and a bare LAN
// IP (SPEC §2.1) — and a hardcoded absolute URL would work on exactly one of
// them. Anything genuinely absolute (links in notifications, export feeds) comes
// from server-side configuration, never from the browser.

const API_BASE = '/api'

// The server sets this cookie deliberately readable by JavaScript: we have to
// echo it into a header on state-changing requests. What makes that safe is that
// a cross-site attacker can cause the cookie to be *sent* but cannot read it.
const CSRF_COOKIE = 'home_ops_csrf'
const CSRF_HEADER = 'X-CSRF-Token'

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

export interface ApiResult<T> {
  status: number
  ok: boolean
  data: T
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match?.[1] ? decodeURIComponent(match[1]) : null
}

export async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; formData?: FormData } = {},
): Promise<ApiResult<T>> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }

  // Never set Content-Type for a FormData body. The browser has to generate it
  // itself so it can append the multipart boundary; supplying one produces a
  // request the server cannot parse.
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  if (UNSAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) headers[CSRF_HEADER] = csrf
  }

  const payload = options.formData ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined)

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    // Sessions are httpOnly cookies. Same-origin only: the reverse proxy puts
    // the API and the app on one origin.
    credentials: 'same-origin',
    ...(payload !== undefined ? { body: payload } : {}),
  })

  // 204 has no body, and readiness answers 503 *with* one — so callers get the
  // status alongside whatever was returned rather than an exception that throws
  // the useful part away.
  const text = await response.text()
  const data = (text ? JSON.parse(text) : null) as T

  return { status: response.status, ok: response.ok, data }
}

// --- shapes -------------------------------------------------------------------

export interface Readiness {
  status: 'ready' | 'not_ready'
  version: string
  database: { ok: boolean; latency_ms: number | null; error: string | null }
  migration: { current: string[]; head: string[]; in_sync: boolean; error: string | null }
}

export interface SetupStatus {
  needs_setup: boolean
  can_setup_here: boolean
  reason: string | null
}

export interface HouseholdMember {
  id: string
  username: string
  display_name: string
  /** A palette key (`clay`, `forest`, …), resolved to a colour by tokens.css. */
  avatar_color: string | null
  role: 'admin' | 'adult' | 'limited' | 'readonly'
  is_active: boolean
  created_at: string
}

export interface CurrentUser extends HouseholdMember {
  permissions: Record<string, string>
}

export interface SettingsDocument {
  values: Record<string, unknown>
}

export interface ApiError {
  detail?: string | { msg?: string }[]
}

export function errorMessage(data: unknown, fallback: string): string {
  const detail = (data as ApiError | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return fallback
}

// --- calls --------------------------------------------------------------------

export const getReadiness = () => request<Readiness>('/health/ready')
export const getSetupStatus = () => request<SetupStatus>('/setup')
export const getMe = () => request<CurrentUser>('/auth/me')

export const claimHousehold = (body: {
  username: string
  display_name: string
  password: string
}) => request<{ user: CurrentUser }>('/setup', { method: 'POST', body })

export const login = (body: { username: string; password: string }) =>
  request<{ user: CurrentUser }>('/auth/login', { method: 'POST', body })

export const logout = () => request<null>('/auth/logout', { method: 'POST' })

export const listUsers = () => request<HouseholdMember[]>('/users')

// --- members, roles and permissions (SPEC §4.2, §4.9) --------------------------

export type Access = 'none' | 'read' | 'write'

export interface PermissionEntry {
  subject_type: 'role' | 'user'
  subject_id: string
  module: string
  access: Access
}

export const createUser = (body: {
  username: string
  display_name: string
  password: string
  role: string
  avatar_color?: string | null
}) => request<HouseholdMember>('/users', { method: 'POST', body })

export const updateUser = (
  id: string,
  body: { display_name?: string; role?: string; is_active?: boolean; avatar_color?: string | null },
) => request<HouseholdMember>(`/users/${id}`, { method: 'PATCH', body })

/** Your own password. Needs the current one, and signs out every other session. */
export const changeOwnPassword = (currentPassword: string, newPassword: string) =>
  request<null>('/auth/password', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword },
  })

/** Somebody else's, for when they have forgotten it. Needs `users` write, and
    refuses your own account — that one needs the current password. */
export const resetPassword = (userId: string, newPassword: string) =>
  request<null>(`/users/${userId}/password`, {
    method: 'POST',
    body: { new_password: newPassword },
  })

/** What deleting a member would destroy. Health is a yes-or-no, never a count:
    "Sam has 42 health records" is health data about Sam (§4.8). */
export interface Belongings {
  tasks: number
  notes: number
  recipes: number
  events: number
  contacts: number
  shopping_lists: number
  planned_meals: number
  has_health_records: boolean
}

export const getBelongings = (userId: string) =>
  request<Belongings>(`/users/${userId}/belongings`)

/** Removes the member **and everything they own** — 19 cascading foreign keys.
    Refused for your own account and for the last active administrator. */
export const deleteUser = (userId: string) =>
  request<null>(`/users/${userId}`, { method: 'DELETE' })

/** Only the deviations from the role defaults are stored, so this is sparse. */
export const listPermissions = () => request<PermissionEntry[]>('/permissions')

export const setPermission = (body: {
  subject_type: 'role' | 'user'
  subject_id: string
  module: string
  access: Access
}) => request<PermissionEntry>('/permissions', { method: 'PUT', body })

export const clearPermission = (params: {
  subject_type: 'role' | 'user'
  subject_id: string
  module: string
}) => request<null>(`/permissions${query(params)}`, { method: 'DELETE' })

export const getMySettings = () => request<SettingsDocument>('/settings/me')

export const getHouseholdSettings = () => request<SettingsDocument>('/settings/household')

export const updateMySetting = (key: string, value: unknown) =>
  request<SettingsDocument>(`/settings/me/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: { value },
  })

export const updateHouseholdSetting = (key: string, value: unknown) =>
  request<SettingsDocument>(`/settings/household/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: { value },
  })

// --- tasks (SPEC §4.4) --------------------------------------------------------

export type TaskStatus = 'open' | 'in_progress' | 'done' | 'archived'
export type TaskPriority = 'none' | 'low' | 'medium' | 'high' | 'urgent'
export type Visibility = 'private' | 'assignees' | 'household'

export interface Task {
  id: string
  title: string
  description: string | null
  category_id: string | null
  priority: TaskPriority
  status: TaskStatus
  due_at: string | null
  due_is_all_day: boolean
  parent_task_id: string | null
  recurrence_rule: string | null
  recurrence_group_id: string | null
  /** A human label like "Weekly", so the UI never parses an RRULE itself. */
  recurrence_label: string | null
  completed_at: string | null
  completed_by_user_id: string | null
  owner_id: string
  visibility: Visibility
  position: number
  created_at: string
  assignee_ids: string[]
}

export interface TaskCategory {
  id: string
  name: string
  color_key: string | null
  position: number
}

export interface Completion {
  completed: Task
  /** The next instance of a recurring series, when one was generated. */
  successor: Task | null
}

function query(params: Record<string, string | boolean | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

export const listTasks = (
  filters: { search?: string | undefined; assignee_id?: string | undefined } = {},
) =>
  request<Task[]>(`/tasks${query(filters)}`)

export const createTask = (body: Partial<Task> & { title: string }) =>
  request<Task>('/tasks', { method: 'POST', body })

export const updateTask = (id: string, body: Partial<Task>) =>
  request<Task>(`/tasks/${id}`, { method: 'PATCH', body })

export const completeTask = (id: string) =>
  request<Completion>(`/tasks/${id}/complete`, { method: 'POST' })

export const deleteTask = (id: string) => request<null>(`/tasks/${id}`, { method: 'DELETE' })

export const listCategories = () => request<TaskCategory[]>('/task-categories')

export const createCategory = (body: { name: string; color_key?: string | null }) =>
  request<TaskCategory>('/task-categories', { method: 'POST', body })

export const deleteCategory = (id: string) =>
  request<null>(`/task-categories/${id}`, { method: 'DELETE' })

// --- notes (SPEC §4.5) --------------------------------------------------------

export interface Note {
  id: string
  title: string
  /** Markdown source. Rendered and sanitised in lib/markdown.ts. */
  body: string
  color_key: string | null
  is_pinned: boolean
  owner_id: string
  visibility: Visibility
  created_at: string
  updated_at: string
  /** Manual board order, lowest first. Shared, like pinning. */
  position: number
  tags: string[]
}

export const listNotes = (
  filters: {
    search?: string | undefined
    tag?: string | undefined
    author_id?: string | undefined
  } = {},
) =>
  request<Note[]>(`/notes${query(filters)}`)

export const listNoteTags = () => request<string[]>('/notes/tags')

export const createNote = (body: Partial<Note> & { title: string }) =>
  request<Note>('/notes', { method: 'POST', body })

export const updateNote = (id: string, body: Partial<Note>) =>
  request<Note>(`/notes/${id}`, { method: 'PATCH', body })

export const deleteNote = (id: string) => request<null>(`/notes/${id}`, { method: 'DELETE' })

/** The board's new order, front to back. Shared across the household. */
export const reorderNotes = (noteIds: string[]) =>
  request<null>('/notes/order', { method: 'PUT', body: { note_ids: noteIds } })

// --- calendar (SPEC §4.3) -----------------------------------------------------

export type EditScope = 'this' | 'this_and_following' | 'all'

export interface CalendarSummary {
  id: string
  name: string
  color_key: string | null
  is_default: boolean
  position: number
}

/**
 * One occurrence, already resolved by the server.
 *
 * A recurring event is one database row and many things on a wall planner. The
 * server expands it — so the browser never parses an RRULE or reasons about
 * daylight saving to draw a grid.
 */
export interface Occurrence {
  event_id: string
  calendar_id: string
  title: string
  description: string | null
  location: string | null
  starts_at: string
  ends_at: string
  is_all_day: boolean
  tzid: string
  /** Which occurrence this is. Send it back to edit or delete just this one. */
  original_start: string
  is_recurring: boolean
  is_override: boolean
  recurrence_label: string | null
  owner_id: string
  visibility: Visibility
  assignee_ids: string[]
}

export interface AgendaTask {
  task_id: string
  title: string
  due_at: string
  status: TaskStatus
  priority: TaskPriority
  assignee_ids: string[]
}

export interface Agenda {
  occurrences: Occurrence[]
  tasks: AgendaTask[]
}

export const listCalendars = () => request<CalendarSummary[]>('/calendars')

export const createCalendar = (body: { name: string; color_key?: string | null }) =>
  request<CalendarSummary>('/calendars', { method: 'POST', body })

export const updateCalendar = (
  id: string,
  body: { name?: string; color_key?: string | null; is_default?: boolean },
) => request<CalendarSummary>(`/calendars/${id}`, { method: 'PATCH', body })

/** Takes the calendar's events with it, unlike deleting a task category. */
export const deleteCalendar = (id: string) =>
  request<null>(`/calendars/${id}`, { method: 'DELETE' })

export const getAgenda = (params: {
  start: string
  end: string
  assignee_id?: string | undefined
  search?: string | undefined
  include_tasks?: boolean | undefined
}) => request<Agenda>(`/calendar/agenda${query(params as Record<string, string | boolean | undefined>)}`)

export const createEvent = (body: Record<string, unknown>) =>
  request<Occurrence>('/calendar/events', { method: 'POST', body })

export const updateEvent = (id: string, body: Record<string, unknown>) =>
  request<Occurrence>(`/calendar/events/${id}`, { method: 'PATCH', body })

/** POST, not DELETE: it carries a scope and an occurrence in the body. */
export const deleteEvent = (id: string, body: { scope: EditScope; original_start?: string }) =>
  request<null>(`/calendar/events/${id}/delete`, { method: 'POST', body })

// --- kitchen: recipes (SPEC §4.6, phase 5a) -----------------------------------

export interface RecipeIngredientRow {
  id: string
  ingredient_id: string
  name: string
  aisle: string | null
  /** A decimal string, not a number: the server sends exact values. */
  quantity: string | null
  unit: string | null
  note: string | null
  position: number
}

export interface RecipeStepRow {
  id: string
  position: number
  body: string
}

export interface RecipeSummary {
  id: string
  title: string
  description: string
  servings: number
  prep_minutes: number | null
  cook_minutes: number | null
  /** Changes whenever the picture does, so it doubles as a cache-buster. */
  image_key: string | null
  owner_id: string
  visibility: Visibility
  tags: string[]
}

export interface Recipe extends RecipeSummary {
  source_url: string | null
  ingredients: RecipeIngredientRow[]
  steps: RecipeStepRow[]
}

export interface IngredientSuggestion {
  id: string
  name: string
  aisle: string | null
}

export const listRecipes = (params: {
  search?: string | undefined
  tag?: string | undefined
  author_id?: string | undefined
}) => request<RecipeSummary[]>(`/recipes${query(params as Record<string, string | undefined>)}`)

export const getRecipe = (id: string) => request<Recipe>(`/recipes/${id}`)

export const listRecipeTags = () => request<string[]>('/recipes/tags')

export const listIngredients = (search?: string) =>
  request<IngredientSuggestion[]>(`/ingredients${query({ search })}`)

export const createRecipe = (body: Record<string, unknown>) =>
  request<Recipe>('/recipes', { method: 'POST', body })

export const updateRecipe = (id: string, body: Record<string, unknown>) =>
  request<Recipe>(`/recipes/${id}`, { method: 'PATCH', body })

export const deleteRecipe = (id: string) => request<null>(`/recipes/${id}`, { method: 'DELETE' })

/**
 * The picture, served by the API rather than as a static file.
 *
 * A recipe carries a visibility and this URL goes through the same check the
 * recipe does. The key is in the query string so the browser treats a replaced
 * image as a different resource.
 */
export const recipeImageUrl = (id: string, imageKey: string, thumb = false) =>
  `/api/recipes/${id}/image?v=${imageKey}${thumb ? '&thumb=true' : ''}`

export const uploadRecipeImage = (id: string, file: File) => {
  const form = new FormData()
  form.append('file', file)
  return request<Recipe>(`/recipes/${id}/image`, { method: 'PUT', formData: form })
}

export const deleteRecipeImage = (id: string) =>
  request<Recipe>(`/recipes/${id}/image`, { method: 'DELETE' })

// --- recipe import (SPEC §4.6, phase 5b) --------------------------------------

export interface ImportedIngredient {
  /** The line as the site published it, kept beside the parse so the cook can
      see what it was guessing from. */
  raw: string
  name: string
  quantity: string | null
  unit: string | null
  note: string | null
  /** False when nothing measurable was found — the rows worth checking. */
  confident: boolean
}

/** A draft. Nothing has been saved, and none of it is trusted. */
export interface ImportedRecipe {
  title: string
  description: string
  servings: number | null
  prep_minutes: number | null
  cook_minutes: number | null
  source_url: string
  tags: string[]
  ingredients: ImportedIngredient[]
  steps: string[]
  extracted_by: string
  /** The page's picture. Downloaded on save, not while previewing. */
  image_url: string | null
}

export const importRecipe = (url: string) =>
  request<ImportedRecipe>('/recipes/import', { method: 'POST', body: { url } })

/** Fetched only once the cook has decided to keep the recipe, not while they
    are still looking at a preview they might discard. */
export const setRecipeImageFromUrl = (id: string, url: string) =>
  request<Recipe>(`/recipes/${id}/image/from-url`, { method: 'POST', body: { url } })

export interface MealieImportResult {
  /** True when nothing was written — the preview. */
  preview: boolean
  found: number
  imported: number
  replaced: number
  skipped_existing: number
  /** Files in the archive that were not recipes we recognised. */
  skipped_unreadable: number
  /** Pictures in the archive — a property of the file. */
  found_with_images: number
  /** Pictures actually stored, which differs whenever recipes are skipped. */
  with_images: number
  /** How many titles clash. `conflicts` below is truncated for display, so its
      length is not this number. */
  conflict_count: number
  conflicts: string[]
}

/**
 * Read a Mealie ZIP export.
 *
 * `preview` walks the same code path and writes nothing, so what it reports and
 * what the import does cannot drift apart.
 */
export const importMealie = (
  file: File,
  options: { preview: boolean; onConflict?: 'skip' | 'replace' },
) => {
  const form = new FormData()
  form.append('file', file)
  const params = query({
    preview: options.preview,
    on_conflict: options.onConflict ?? 'skip',
  })
  return request<MealieImportResult>(`/recipes/import/mealie${params}`, {
    method: 'POST',
    formData: form,
  })
}

// --- the meal plan (SPEC §4.6, phase 5c) --------------------------------------

export type MealSlot = 'breakfast' | 'lunch' | 'dinner' | 'side'

export interface MealPlanEntry {
  id: string
  /** A calendar date, `YYYY-MM-DD`. A meal belongs to a day, not to an instant. */
  plan_date: string
  slot: MealSlot
  position: number
  /** Null for a free-text entry, and also when the recipe is one you may not
      see — `hidden_recipe` tells the two apart. */
  recipe_id: string | null
  title: string
  note: string | null
  image_key: string | null
  owner_id: string
  /** Somebody planned a recipe here that you cannot open. The slot is taken;
      what is in it is not disclosed. */
  hidden_recipe: boolean
}

export const listMealPlan = (start: string, end: string) =>
  request<MealPlanEntry[]>(`/meal-plan${query({ start, end })}`)

export const addMealPlanEntry = (body: {
  plan_date: string
  slot: MealSlot
  recipe_id?: string | null
  title?: string | null
  note?: string | null
}) => request<MealPlanEntry>('/meal-plan', { method: 'POST', body })

export const moveMealPlanEntry = (
  id: string,
  body: { plan_date: string; slot: MealSlot; before_id?: string | null },
) => request<MealPlanEntry>(`/meal-plan/${id}`, { method: 'PATCH', body })

export const removeMealPlanEntry = (id: string) =>
  request<null>(`/meal-plan/${id}`, { method: 'DELETE' })

/** §4.6: turn a free-text planned meal into a real recipe, in place. */
export const savePlannedMealAsRecipe = (id: string) =>
  request<Recipe>(`/meal-plan/${id}/save-as-recipe`, { method: 'POST' })

// --- shopping (SPEC §4.12, phase 6) -------------------------------------------

export interface ShoppingItem {
  id: string
  list_id: string
  ingredient_id: string | null
  name: string
  /** Where in the shop it lives — the ingredient's aisle, or the line's own.
      One field, so the client need not know which kind of line it is holding. */
  section: string | null
  /** A decimal string from the server; parsed for display, never for arithmetic. */
  quantity: string | null
  unit: string | null
  note: string | null
  is_generated: boolean
  /** The quantities behind this line could not be added — 200 g and 2 cups of
      the same flour. The list says so rather than inventing a total. */
  is_uncombined: boolean
  /** Somebody typed their own amount over the plan's. It survives a rebuild. */
  quantity_overridden: boolean
  is_checked: boolean
  checked_by_id: string | null
  position: number
}

export interface ShoppingList {
  id: string
  name: string
  visibility: Visibility
  /** Meaningful while visibility is `assignees`; kept regardless, so flipping
      back does not lose who it was shared with. */
  shared_with: string[]
  is_meal_plan_target: boolean
  position: number
  owner_id: string
  items: ShoppingItem[]
}

export interface ShoppingGenerateResult {
  list_id: string
  hidden_meals: number
  text_meals: string[]
  uncombined: number
  /** Wanted, and already on another list because somebody moved it there. Not
      an error — it is the transfer working. */
  kept_on_other_lists: number
}

/**
 * Every list you can see, with its lines, and the ETag that came back.
 *
 * One request rather than one per list: the page draws them all at once, so
 * asking per list would multiply by however many lists somebody made. `lists`
 * is null on a 304, which is the signal to keep what is already on screen.
 *
 * Written against `fetch` rather than `request` because that helper
 * deliberately hides headers, and the whole point here is a header exchange.
 */
export async function listShoppingLists(
  etag: string | null,
): Promise<{ status: number; etag: string | null; lists: ShoppingList[] | null }> {
  const response = await fetch(`${API_BASE}/shopping-lists`, {
    headers: etag
      ? { Accept: 'application/json', 'If-None-Match': etag }
      : { Accept: 'application/json' },
    credentials: 'same-origin',
  })

  if (response.status === 304) {
    return { status: 304, etag: response.headers.get('ETag') ?? etag, lists: null }
  }

  const text = await response.text()
  return {
    status: response.status,
    etag: response.headers.get('ETag'),
    lists: response.ok && text ? (JSON.parse(text) as ShoppingList[]) : null,
  }
}

export const createShoppingList = (body: {
  name: string
  visibility?: Visibility
  shared_with?: string[]
  is_meal_plan_target?: boolean
}) => request<ShoppingList>('/shopping-lists', { method: 'POST', body })

export const updateShoppingList = (
  id: string,
  body: {
    name?: string
    visibility?: Visibility
    shared_with?: string[]
    is_meal_plan_target?: boolean
  },
) => request<ShoppingList>(`/shopping-lists/${id}`, { method: 'PATCH', body })

export const deleteShoppingList = (id: string) =>
  request<null>(`/shopping-lists/${id}`, { method: 'DELETE' })

export const reorderShoppingLists = (order: string[]) =>
  request<null>('/shopping-lists/order', { method: 'PUT', body: { order } })

export const addShoppingItem = (
  listId: string,
  body: {
    ingredient_name?: string | null
    title?: string | null
    quantity?: string | null
    unit?: string | null
    note?: string | null
    section?: string | null
  },
) => request<ShoppingItem>(`/shopping-lists/${listId}/items`, { method: 'POST', body })

/** Tick it, note it, place it in the shop, or send it to another list. */
export const updateShoppingItem = (
  id: string,
  body: {
    is_checked?: boolean
    note?: string | null
    section?: string | null
    list_id?: string
    /** Null hands a generated line back to the meal plan's arithmetic. */
    quantity?: string | null
    unit?: string | null
  },
) => request<ShoppingItem>(`/shopping-lists/items/${id}`, { method: 'PATCH', body })

export const removeShoppingItem = (id: string) =>
  request<null>(`/shopping-lists/items/${id}`, { method: 'DELETE' })

export const clearCheckedShoppingItems = (listId: string) =>
  request<{ removed: number }>(`/shopping-lists/${listId}/clear-checked`, { method: 'POST' })

export const listShoppingSections = () => request<string[]>('/shopping-lists/sections')

export const generateShoppingList = (start: string, end: string, listId?: string) =>
  request<ShoppingGenerateResult>('/shopping-lists/generate', {
    method: 'POST',
    body: { start, end, ...(listId ? { list_id: listId } : {}) },
  })

// --- contacts (SPEC §4.7, phase 7) --------------------------------------------

export interface ContactPhone {
  label: string | null
  number: string
}

export interface ContactEmail {
  label: string | null
  address: string
}

export interface ContactAddress {
  label: string | null
  street: string | null
  locality: string | null
  region: string | null
  postcode: string | null
  country: string | null
}

export interface ContactSummary {
  id: string
  display_name: string
  organisation: string | null
  job_title: string | null
  owner_id: string
  visibility: Visibility
  tags: string[]
  /** Carried on the summary deliberately: a directory that makes you open a
      record to see the number is a directory you stop using. */
  phones: ContactPhone[]
  emails: ContactEmail[]
}

export interface Contact extends ContactSummary {
  given_name: string | null
  family_name: string | null
  website: string | null
  notes: string
  addresses: ContactAddress[]
}

export interface ContactImportResult {
  preview: boolean
  found: number
  imported: number
  replaced: number
  skipped_existing: number
  unreadable: number
  conflict_count: number
  conflicts: string[]
}

export const listContacts = (
  params: { search?: string | undefined; tag?: string | undefined } = {},
) => request<ContactSummary[]>(`/contacts${query(params)}`)

export const listContactTags = () => request<string[]>('/contacts/tags')

export const getContact = (id: string) => request<Contact>(`/contacts/${id}`)

export const createContact = (body: Record<string, unknown>) =>
  request<Contact>('/contacts', { method: 'POST', body })

export const updateContact = (id: string, body: Record<string, unknown>) =>
  request<Contact>(`/contacts/${id}`, { method: 'PATCH', body })

export const deleteContact = (id: string) =>
  request<null>(`/contacts/${id}`, { method: 'DELETE' })

/** A relative URL, like everything else here (§2.1). The browser downloads it
    with the session cookie attached, so there is nothing to build by hand. */
export const contactsExportUrl = (
  params: { search?: string | undefined; tag?: string | undefined } = {},
) => `${API_BASE}/contacts/export${query(params)}`

export function importContacts(
  file: File,
  options: { preview?: boolean; onConflict?: 'skip' | 'replace' } = {},
) {
  const form = new FormData()
  form.append('file', file)
  const params = query({
    preview: String(options.preview ?? true),
    on_conflict: options.onConflict ?? 'skip',
  })
  return request<ContactImportResult>(`/contacts/import${params}`, {
    method: 'POST',
    formData: form,
  })
}

// --- health records (SPEC §4.8, phase 8) ---------------------------------------
//
// `/health-records`, not `/health`: that URL is the application's liveness
// probe, is public, and §8.6 has it verified over the Cloudflare hostname.

export type VitalKind =
  | 'weight'
  | 'blood_pressure'
  | 'heart_rate'
  | 'blood_glucose'
  | 'temperature'
  | 'oxygen_saturation'
  | 'respiratory_rate'
  | 'custom'

export interface VitalReading {
  id: string
  /** Who the reading is *about*. Not necessarily who typed it. */
  subject_id: string
  recorded_by_id: string | null
  kind: VitalKind
  label: string | null
  value: string
  secondary_value: string | null
  unit: string
  measured_at: string
  note: string | null
}

/** Descriptive statistics only. There is deliberately no verdict field here,
    and §4.8 forbids adding one. */
export interface VitalSummary {
  kind: string
  unit: string | null
  count: number
  first_at: string | null
  last_at: string | null
  latest: string | null
  minimum: string | null
  maximum: string | null
  mean: string | null
  change: string | null
}

export interface MedicationDose {
  id: string
  taken_at: string
  amount: string | null
  recorded_by_id: string | null
  note: string | null
}

export interface Medication {
  id: string
  subject_id: string
  recorded_by_id: string | null
  name: string
  dose: string | null
  form: string | null
  schedule: string | null
  is_active: boolean
  started_on: string | null
  stopped_on: string | null
  stock_count: string | null
  refill_at: string | null
  /** Stock at or below the level the member set. Two of their own numbers
      compared — not a judgment about them. */
  needs_refill: boolean
  note: string | null
  doses: MedicationDose[]
}

export interface LabAnalyte {
  id: string
  name: string
  value: string | null
  text_value: string | null
  unit: string | null
  /** The lab's printed range, transcribed. Nothing compares the value to it. */
  reference_low: string | null
  reference_high: string | null
  reference_text: string | null
  position: number
}

export interface LabReport {
  id: string
  subject_id: string
  recorded_by_id: string | null
  title: string
  lab_name: string | null
  collected_on: string
  note: string | null
  analytes: LabAnalyte[]
}

export interface ActivityEntry {
  id: string
  subject_id: string
  recorded_by_id: string | null
  kind: string
  label: string | null
  happened_at: string
  duration_minutes: number | null
  distance_miles: string | null
  calories: number | null
  note: string | null
}

export interface HealthShare {
  viewer_id: string
  granted_at: string
}

export interface HealthVocabulary {
  vital_kinds: string[]
  medication_forms: string[]
  activity_kinds: string[]
}

const HEALTH = '/health-records'

export const getHealthVocabulary = () => request<HealthVocabulary>(`${HEALTH}/vocabulary`)

/** Whose records you can see: yourself, plus anyone who has shared with you. */
export const listHealthSubjects = () => request<string[]>(`${HEALTH}/subjects`)

export const listHealthShares = () => request<HealthShare[]>(`${HEALTH}/shares`)

export const setHealthShares = (viewerIds: string[]) =>
  request<HealthShare[]>(`${HEALTH}/shares`, { method: 'PUT', body: { viewer_ids: viewerIds } })

export const listVitals = (params: { subject_id?: string; kind?: string } = {}) =>
  request<VitalReading[]>(`${HEALTH}/vitals${query(params)}`)

export const getVitalSummary = (subjectId: string, kind: string) =>
  request<VitalSummary>(`${HEALTH}/vitals/summary${query({ subject_id: subjectId, kind })}`)

export const addVital = (body: Record<string, unknown>) =>
  request<VitalReading>(`${HEALTH}/vitals`, { method: 'POST', body })

export const deleteVital = (id: string) =>
  request<null>(`${HEALTH}/vitals/${id}`, { method: 'DELETE' })

export const listMedications = (params: { subject_id?: string; include_stopped?: boolean } = {}) =>
  request<Medication[]>(
    `${HEALTH}/medications${query({
      subject_id: params.subject_id,
      include_stopped: params.include_stopped ? 'true' : undefined,
    })}`,
  )

export const addMedication = (body: Record<string, unknown>) =>
  request<Medication>(`${HEALTH}/medications`, { method: 'POST', body })

export const updateMedication = (id: string, body: Record<string, unknown>) =>
  request<Medication>(`${HEALTH}/medications/${id}`, { method: 'PATCH', body })

export const logDose = (id: string, body: Record<string, unknown> = {}) =>
  request<Medication>(`${HEALTH}/medications/${id}/doses`, { method: 'POST', body })

export const deleteMedication = (id: string) =>
  request<null>(`${HEALTH}/medications/${id}`, { method: 'DELETE' })

export const listLabReports = (params: { subject_id?: string } = {}) =>
  request<LabReport[]>(`${HEALTH}/lab-reports${query(params)}`)

export const addLabReport = (body: Record<string, unknown>) =>
  request<LabReport>(`${HEALTH}/lab-reports`, { method: 'POST', body })

export const deleteLabReport = (id: string) =>
  request<null>(`${HEALTH}/lab-reports/${id}`, { method: 'DELETE' })

export const listActivity = (params: { subject_id?: string } = {}) =>
  request<ActivityEntry[]>(`${HEALTH}/activity${query(params)}`)

export const addActivity = (body: Record<string, unknown>) =>
  request<ActivityEntry>(`${HEALTH}/activity`, { method: 'POST', body })

export const deleteActivity = (id: string) =>
  request<null>(`${HEALTH}/activity/${id}`, { method: 'DELETE' })

/** A relative URL the browser downloads with the session cookie attached. */
export const healthExportUrl = (subjectId: string) =>
  `${API_BASE}${HEALTH}/export${query({ subject_id: subjectId })}`

// --- API tokens (SPEC §4.10, phase 9) ------------------------------------------

export interface TokenScope {
  module: string
  access: Access
}

export interface ApiToken {
  id: string
  name: string
  /** The first few characters, so a list can name which token this is. */
  prefix: string
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
  /** Empty means the token is not narrowed: its owner's permissions are the
      only limit. Naming any module narrows it to those modules alone. */
  scopes: TokenScope[]
}

/** The one response carrying the secret. It is shown once and never again. */
export interface TokenCreated extends ApiToken {
  token: string
}

export const listTokens = () => request<ApiToken[]>('/tokens')

export const createToken = (body: {
  name: string
  scopes?: Record<string, Access>
  expires_at?: string | null
}) => request<TokenCreated>('/tokens', { method: 'POST', body })

export const revokeToken = (id: string) =>
  request<null>(`/tokens/${id}`, { method: 'DELETE' })

/** Removes the rows for tokens already revoked. Nothing live is touched. */
export const clearRevokedTokens = () =>
  request<{ deleted: number }>('/tokens/revoked', { method: 'DELETE' })
