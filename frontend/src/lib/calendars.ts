// Calendar colour: the second half of the Rota colour system (SPEC §6).
//
// A member owns a hue and a calendar owns a hue, and they are drawn differently
// on purpose:
//
//   * A member's hue is an *edge* — a 3px bar, a small filled mark. It answers
//     "whose is this".
//   * A calendar's hue is a *fill* — the whole event block. It answers "what
//     kind of thing is this", which on a wall planner is what you scan for
//     first: work, school, the household.
//
// They are separate palettes for a concrete reason and not merely a tidy one.
// An event filled in a member's hue, carrying that same member's initials mark,
// puts the mark on its own colour and it disappears. Two palettes makes that
// impossible rather than merely unlikely.
//
// Same two rules as members.ts: the stored value is a KEY, never a hex, and the
// colour is never the only signal — a block always carries its title.

export const CALENDAR_HUES = [
  'violet',
  'slate',
  'graphite',
  'brick',
  'rust',
  'moss',
] as const

export type CalendarHue = (typeof CALENDAR_HUES)[number]

export function isCalendarHue(value: unknown): value is CalendarHue {
  return typeof value === 'string' && (CALENDAR_HUES as readonly string[]).includes(value)
}

/**
 * The CSS custom property holding this calendar's fill in the current theme.
 *
 * A calendar with no hue set falls back to the muted text colour, which is
 * legible under `--calendar-on-hue` in both themes and reads as "unset" rather
 * than quietly claiming another calendar's colour.
 */
export function calendarHueVar(key: string | null | undefined): string {
  return isCalendarHue(key) ? `var(--calendar-${key})` : 'var(--text-secondary)'
}
