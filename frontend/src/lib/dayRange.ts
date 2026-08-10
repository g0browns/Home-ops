// Picking one day, or a run of them (SPEC §4.3).
//
// Dates here are `YYYY-MM-DD` strings, not `Date`s, and deliberately: a plan day
// has no time and no zone, and lexicographic comparison on this format *is*
// chronological comparison, so the ordering below needs no parsing at all. The
// moment these become `Date`s somebody compares them with `===`, or drifts one
// across midnight by converting to UTC.
//
// The range is **inclusive at both ends**: `{from: '16', to: '20'}` is five days,
// which is what somebody who clicked 16 and then 20 means. Turning that into the
// exclusive end the database wants is the editor's job, once, at save.

export interface DayRange {
  /** `YYYY-MM-DD`, inclusive. */
  readonly from: string
  /** `YYYY-MM-DD`, inclusive. Equal to `from` for a single day. */
  readonly to: string
}

/**
 * Where a click leaves the selection.
 *
 * Two states and no extra flag: a click either collapses the range to one day or
 * extends a single day into a range. So the gesture is *click the first day,
 * click the last day*, and a third click starts again — which is what a picker
 * with no visible mode has to do to stay predictable.
 *
 * Clicking backwards works: a single day of the 20th, then the 16th, gives
 * 16→20 rather than an empty or inverted range.
 */
export function nextRange(current: DayRange, clicked: string): DayRange {
  const single = current.from === current.to
  if (single && clicked !== current.from) {
    return clicked < current.from
      ? { from: clicked, to: current.from }
      : { from: current.from, to: clicked }
  }
  // Already a range, or the same day clicked twice: start over from here.
  return { from: clicked, to: clicked }
}

/** How a day should be drawn. `single` exists so one day is a circle rather than
    a one-day-wide band with two rounded ends fighting each other. */
export type DayState = 'single' | 'start' | 'end' | 'between' | 'none'

export function dayState(day: string, range: DayRange): DayState {
  if (range.from === range.to) return day === range.from ? 'single' : 'none'
  if (day === range.from) return 'start'
  if (day === range.to) return 'end'
  return day > range.from && day < range.to ? 'between' : 'none'
}

/**
 * More than one day, which the editor treats as automatically all-day.
 *
 * The household's rule: a thing that spans days is a thing you do not give a
 * start time to. See the editor for the one exception that keeps working — an
 * overnight event picked as a *single* day whose end time is earlier than its
 * start.
 */
export function spansMultipleDays(range: DayRange): boolean {
  return range.from !== range.to
}

/** How many days the range covers, inclusive. `16→20` is 5, not 4. */
export function dayCount(range: DayRange): number {
  const from = Date.parse(`${range.from}T00:00:00Z`)
  const to = Date.parse(`${range.to}T00:00:00Z`)
  if (Number.isNaN(from) || Number.isNaN(to)) return 0
  // Parsed as UTC on both sides, so no daylight-saving hour can creep into the
  // division and round 5 days down to 4.
  return Math.round((to - from) / 86_400_000) + 1
}

/** `HH:mm` in local time, for a `type="time"` field. */
export function clockOf(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export interface Instants {
  readonly startsAt: string
  readonly endsAt: string
  readonly isAllDay: boolean
}

/**
 * The two instants the API wants, from what the picker collected.
 *
 * Three rules, and each is a decision rather than arithmetic:
 *
 * **A range is all-day, whatever the checkbox said.** The household's rule: a
 * thing spanning days is not a thing with a start time. So the times are simply
 * not consulted, and `isAllDay` comes back true.
 *
 * **An all-day event ends at midnight on the day AFTER the last one.** The end is
 * exclusive — this is the trap `lib/occurrences.ts` documents at length, and
 * getting it wrong here makes every all-day event a day short at one end or a day
 * long at the other.
 *
 * **A single day whose end time is not after its start runs overnight.** That is
 * the only way to say "23:00 to 02:00" now that there is one date field rather
 * than two, and it is what people mean: nobody enters an event that ends before
 * it begins on the same day.
 */
export function instantsFor(
  range: DayRange,
  times: { readonly allDay: boolean; readonly fromTime: string; readonly toTime: string },
): Instants {
  const allDay = times.allDay || spansMultipleDays(range)

  if (allDay) {
    return {
      startsAt: local(range.from, '00:00').toISOString(),
      endsAt: local(addDay(range.to), '00:00').toISOString(),
      isAllDay: true,
    }
  }

  const startsAt = local(range.from, times.fromTime)
  const overnight = times.toTime <= times.fromTime
  const endsAt = local(overnight ? addDay(range.to) : range.to, times.toTime)
  return { startsAt: startsAt.toISOString(), endsAt: endsAt.toISOString(), isAllDay: false }
}

/**
 * A local wall-clock instant from a day and a time.
 *
 * The `Date(y, m, d, h, min)` form and not `new Date('2026-08-16T09:00')`,
 * because only the former is unambiguously *local* across engines — and local is
 * the whole point: 9am means 9am where the household is, which is also why the
 * event carries a `tzid` rather than an offset.
 */
function local(day: string, clock: string): Date {
  const [year, month, date] = day.split('-').map(Number)
  const [hour, minute] = clock.split(':').map(Number)
  return new Date(year ?? 1970, (month ?? 1) - 1, date ?? 1, hour ?? 0, minute ?? 0, 0, 0)
}

/** The next day, as `YYYY-MM-DD`. Via a Date so month ends and leap years are
    the platform's problem rather than ours. */
function addDay(day: string): string {
  const [year, month, date] = day.split('-').map(Number)
  const next = new Date(year ?? 1970, (month ?? 1) - 1, (date ?? 1) + 1)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}`
}

/** "Monday 16 August", or "16 – 20 August" for a range. For the summary line. */
export function rangeLabel(range: DayRange, locale?: string): string {
  const from = new Date(`${range.from}T00:00:00`)
  if (range.from === range.to) {
    return from.toLocaleDateString(locale, {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
    })
  }
  const to = new Date(`${range.to}T00:00:00`)
  // formatRange drops the repeated month and knows where the month goes, which
  // hand-assembly gets wrong the moment the locale is not the one it was written
  // in — the same lesson the meal-plan picker learned.
  return new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'long' }).formatRange(from, to)
}
