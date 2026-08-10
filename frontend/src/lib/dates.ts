// Date arithmetic for the month grid (SPEC §4.3, "configurable week start").
//
// Pure functions over local `Date`s. The grid is a presentation concern — the
// server has already resolved every occurrence to an instant, so nothing here
// needs to know about recurrence or timezones.
//
// All of it is off-by-one arithmetic around month and week boundaries, which is
// exactly the kind of code that looks right and is wrong for one month a year.

export type WeekStart = 'monday' | 'sunday' | 'saturday'

const WEEK_START_INDEX: Record<WeekStart, number> = {
  sunday: 0,
  monday: 1,
  saturday: 6,
}

export const DAYS_IN_WEEK = 7
/** Six rows always. A fixed height stops the grid jumping between months. */
export const WEEKS_IN_GRID = 6

/**
 * The choices Settings offers, in the order it offers them.
 *
 * Monday first because it is the default and the household's own answer; the
 * other two are the ones people actually ask for. It must stay in step with
 * `week_starts_on`'s `choices` in the settings registry — a fourth option here
 * writes a value the server rejects, and the failure lands on whoever picks it.
 * `test_week_start_vocabulary.py` reads this list and compares the two.
 */
export const WEEK_STARTS: readonly WeekStart[] = ['monday', 'sunday', 'saturday']

export function isWeekStart(value: unknown): value is WeekStart {
  return value === 'monday' || value === 'sunday' || value === 'saturday'
}

/** The day itself, named. `weekdayLabels` abbreviates; a setting should not. */
export function weekStartLabel(weekStartsOn: WeekStart, locale?: string): string {
  // Any known Sunday works as an anchor; 4 January 1970 was one.
  const anchor = new Date(1970, 0, 4)
  return new Intl.DateTimeFormat(locale, { weekday: 'long' }).format(
    addDays(anchor, WEEK_START_INDEX[weekStartsOn]),
  )
}

export function startOfDay(date: Date): Date {
  const copy = new Date(date)
  copy.setHours(0, 0, 0, 0)
  return copy
}

export function addDays(date: Date, days: number): Date {
  const copy = new Date(date)
  copy.setDate(copy.getDate() + days)
  return copy
}

export function addMonths(date: Date, months: number): Date {
  // Anchor to the 1st before shifting: 31 March plus one month is otherwise
  // 31 April, which JavaScript silently turns into 1 May.
  const copy = new Date(date.getFullYear(), date.getMonth() + months, 1)
  return copy
}

/** The first day of the week containing `date`, honouring the household setting. */
export function startOfWeek(date: Date, weekStartsOn: WeekStart): Date {
  const target = WEEK_START_INDEX[weekStartsOn]
  const start = startOfDay(date)
  // +7 before the modulo so a negative difference does not go backwards.
  const shift = (start.getDay() - target + DAYS_IN_WEEK) % DAYS_IN_WEEK
  return addDays(start, -shift)
}

/**
 * The 42 days a month view shows, including the leading and trailing days that
 * belong to the neighbouring months.
 */
export function monthGrid(month: Date, weekStartsOn: WeekStart): Date[] {
  const firstOfMonth = new Date(month.getFullYear(), month.getMonth(), 1)
  const gridStart = startOfWeek(firstOfMonth, weekStartsOn)
  return Array.from({ length: WEEKS_IN_GRID * DAYS_IN_WEEK }, (_, index) =>
    addDays(gridStart, index),
  )
}

/** Weekday labels in the household's order, for the grid header. */
export function weekdayLabels(weekStartsOn: WeekStart, locale?: string): string[] {
  // Any known Sunday works as an anchor; 4 January 1970 was one.
  const anchor = new Date(1970, 0, 4)
  const formatter = new Intl.DateTimeFormat(locale, { weekday: 'short' })
  return Array.from({ length: DAYS_IN_WEEK }, (_, index) =>
    formatter.format(addDays(anchor, WEEK_START_INDEX[weekStartsOn] + index)),
  )
}

export function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

export function isSameMonth(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
}

/**
 * Does an event overlap a given day?
 *
 * Half-open at the end: an event finishing at midnight belongs to the day that
 * just closed, not the one starting. Otherwise every evening event that runs to
 * 00:00 would smear onto the next day in the grid.
 */
export function overlapsDay(start: Date, end: Date, day: Date): boolean {
  const dayStart = startOfDay(day)
  const dayEnd = addDays(dayStart, 1)
  return start < dayEnd && end > dayStart
}

/**
 * `YYYY-MM-DD` in **local** time — a plan date, not an instant.
 *
 * Not `toISOString().slice(0, 10)`, which converts to UTC first: west of
 * Greenwich that reads the previous day for anything before midday, so a meal
 * planned for Tuesday evening is filed under Monday.
 *
 * Lives here because four screens want it — the planner, the shopping list, the
 * recipe preview and the add-to-plan picker — and four copies of a date
 * conversion is four chances for one of them to be the UTC version.
 */
export function isoDate(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** `YYYY-MM-DDTHH:mm` in local time, which is what `datetime-local` expects. */
export function toLocalInput(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

export function formatTime(date: Date, locale?: string): string {
  return new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit' }).format(date)
}

export function formatMonth(date: Date, locale?: string): string {
  return new Intl.DateTimeFormat(locale, { month: 'long', year: 'numeric' }).format(date)
}
