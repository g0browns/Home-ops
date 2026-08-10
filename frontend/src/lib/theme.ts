// Theme resolution (SPEC §6 — light and dark both first-class).
//
// Three states, not two: `system` follows the operating system and is the
// default, because most people have already made this decision once and would
// rather not make it again per app.
//
// Pure functions here; the React wiring lives in useTheme.ts. Keeping the
// resolution logic free of hooks is what makes it directly testable.

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']

export const THEME_STORAGE_KEY = 'home-ops.theme'

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === 'string' && (THEME_PREFERENCES as readonly string[]).includes(value)
}

export function resolveTheme(
  preference: ThemePreference,
  systemPrefersDark: boolean,
): ResolvedTheme {
  if (preference === 'system') return systemPrefersDark ? 'dark' : 'light'
  return preference
}

/**
 * Write the preference onto the document root.
 *
 * For an explicit choice we stamp `data-theme`, which the token stylesheet
 * treats as beating the media query in *both* directions — so picking Light on a
 * dark-mode machine actually works. For `system` we remove the attribute
 * entirely and let `prefers-color-scheme` govern, rather than resolving it in JS
 * and pinning the result; pinning would stop the page tracking an OS change
 * that happens while it is open.
 */
export function applyThemePreference(
  preference: ThemePreference,
  root: HTMLElement = document.documentElement,
): void {
  if (preference === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', preference)
}

/** The preference to persist, read back on next load before the API responds. */
export function readStoredPreference(
  storage: Pick<Storage, 'getItem'> | undefined = safeStorage(),
): ThemePreference {
  const raw = storage?.getItem(THEME_STORAGE_KEY)
  return isThemePreference(raw) ? raw : 'system'
}

export function storePreference(
  preference: ThemePreference,
  storage: Pick<Storage, 'setItem'> | undefined = safeStorage(),
): void {
  try {
    storage?.setItem(THEME_STORAGE_KEY, preference)
  } catch {
    // Private browsing and blocked storage are not errors worth surfacing; the
    // server copy is the source of truth and this is only there to stop a flash
    // of the wrong theme on the next load.
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof localStorage === 'undefined' ? undefined : localStorage
  } catch {
    return undefined
  }
}

export function systemPrefersDark(): boolean {
  return (
    typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches
  )
}

export function themeLabel(preference: ThemePreference): string {
  return { system: 'Match system', light: 'Light', dark: 'Night shift' }[preference]
}
